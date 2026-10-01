"""Portable session board. No imports, paths or state from the vm3 deployment."""
import hashlib
import hmac
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash

from .store import Store
from .auth_flow import install_auth


def create_app(config=None):
    app = Flask(__name__)
    password_hash = os.environ.get('BOARD_PASSWORD_HASH', '')
    hash_file = os.environ.get('BOARD_PASSWORD_HASH_FILE')
    if hash_file:
        password_hash = Path(hash_file).expanduser().read_text().strip()
    app.config.update(
        PASSWORD_HASH=password_hash, USERNAME=os.environ.get('BOARD_USERNAME', 'admin'),
        SECRET_KEY=os.environ.get('BOARD_SECRET_KEY') or secrets.token_hex(32),
        STATE_DIR=os.environ.get('BOARD_STATE_DIR', str(Path.home() / '.local/state/session-board')),
        MFA_STATE_DIR=os.environ.get('BOARD_MFA_STATE_DIR'),
        TMUX_SOCKET=os.environ.get('BOARD_TMUX_SOCKET', 'default'),
        # Invio acceso di serie (scelta di Giovanni, 1 ottobre 2026): si spegne con BOARD_ALLOW_SEND=0.
        ALLOW_SEND=os.environ.get('BOARD_ALLOW_SEND', '1') != '0',
        AUDIO_ENABLED=os.environ.get('BOARD_AUDIO_ENABLED') == '1',
        AUDIO_PYTHON=os.environ.get('BOARD_AUDIO_PYTHON', sys.executable),
        WHISPER_MODEL=os.environ.get('BOARD_WHISPER_MODEL', ''),
        WHISPER_LANGUAGE=os.environ.get('BOARD_WHISPER_LANGUAGE', 'it'),
        MCP_ENABLED=os.environ.get('BOARD_MCP_ENABLED') == '1',
        INGEST_TOKEN=os.environ.get('BOARD_INGEST_TOKEN', ''),
        ACCOUNTS_ENABLED=os.environ.get('BOARD_ACCOUNTS_ENABLED', '1') == '1',
        ACCOUNTS_HOME=os.environ.get('BOARD_ACCOUNTS_HOME', str(Path.home())),
        CLAUDE_BIN=os.environ.get('BOARD_CLAUDE_BIN') or shutil.which('claude') or 'claude',
        CODEX_BIN=os.environ.get('BOARD_CODEX_BIN') or shutil.which('codex'),
        FILES_ROOT=os.environ.get('BOARD_FILES_ROOT', str(Path.home())),
        FILES_MAX_BYTES=int(os.environ.get('BOARD_FILES_MAX_MB', '2048')) * 1024 * 1024,
        SESSION_COOKIE_NAME='portable_board', SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get('BOARD_COOKIE_SECURE') == '1',
        SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_REFRESH_EACH_REQUEST=False, MAX_CONTENT_LENGTH=16 * 1024,
        MAX_FORM_MEMORY_SIZE=16 * 1024,
    )
    app.config.update(config or {})
    if not re.fullmatch(r'scrypt:32768:8:1\$[A-Za-z0-9]{8,64}\$[0-9a-f]{128}', app.config['PASSWORD_HASH'] or ''):
        raise ValueError('BOARD_PASSWORD_HASH deve essere un hash scrypt generato da deploy/set-password.py')
    store = Store(app.config['STATE_DIR'])
    service_lock = threading.Lock()

    def account_services():
        if app.config.get('ACCOUNT_SERVICES') is not None:
            return app.config['ACCOUNT_SERVICES']
        with service_lock:
            if 'account_services' not in app.extensions:
                from .accounts import AccountService
                from .session_switch import SwitchService
                from .tmux_backend import TmuxBackend
                from .usage import UsageService
                usage = UsageService(app.config['ACCOUNTS_HOME'], app.config['CODEX_BIN'])
                accounts = AccountService(app.config['ACCOUNTS_HOME'], app.config['CLAUDE_BIN'],
                                          usage_loader=usage.account_usage)
                switch = SwitchService(TmuxBackend(app.config['TMUX_SOCKET']), accounts,
                                       app.config['STATE_DIR'])
                accounts.set_active_account_checker(switch.active_sessions_for_account)
                app.extensions['account_services'] = dict(accounts=accounts, switch=switch, usage=usage)
            return app.extensions['account_services']

    from .subscriptions_api import create_blueprint
    app.register_blueprint(create_blueprint(account_services))

    def epoch():
        return hashlib.sha256(app.config['PASSWORD_HASH'].encode()).hexdigest()

    def same_origin():
        origin = request.headers.get('Origin') or request.headers.get('Referer')
        if not origin:
            return True
        try:
            incoming = urlsplit(origin)
            expected = urlsplit(request.host_url)
            if incoming.username is not None or incoming.password is not None:
                return False
            return (incoming.scheme, incoming.hostname, incoming.port or (443 if incoming.scheme == 'https' else 80)) == (
                expected.scheme, expected.hostname, expected.port or (443 if expected.scheme == 'https' else 80))
        except ValueError:
            return False

    auth = install_auth(app, username=app.config['USERNAME'],
                        password_ok=lambda value: check_password_hash(app.config['PASSWORD_HASH'], value),
                        credential_epoch=epoch, origin_allowed=same_origin,
                        state_dir=app.config['MFA_STATE_DIR'] or str(Path(app.config['STATE_DIR']) / 'auth'))

    from .chat import create_blueprint as create_chat_blueprint
    app.register_blueprint(create_chat_blueprint(lambda: account_services()['switch'], auth.authenticated))
    from .session_summary import create_blueprint as create_summary_blueprint
    app.register_blueprint(create_summary_blueprint(lambda: account_services()['switch'], auth.authenticated))
    from .questions import create_blueprint as create_questions_blueprint
    app.register_blueprint(create_questions_blueprint(auth.authenticated, lambda: account_services()['switch'],
                                                     allow_answer=lambda: app.config['ALLOW_SEND']))
    from .files_api import create_blueprint as create_files_blueprint
    app.register_blueprint(create_files_blueprint())
    from .audio import create_blueprint as create_audio_blueprint
    app.register_blueprint(create_audio_blueprint(auth.authenticated, lambda: app.config['ALLOW_SEND'],
        dict(enabled=app.config['AUDIO_ENABLED'], python=app.config['AUDIO_PYTHON'],
             model=app.config['WHISPER_MODEL'], language=app.config['WHISPER_LANGUAGE'])))

    @app.before_request
    def guard():
        if request.endpoint in {'static', 'health', 'ingest'} or (
                request.endpoint and request.endpoint.startswith('board_auth.')):
            return None
        if not auth.authenticated():
            if session.get('authenticated'):
                session.clear()
            return (jsonify(error='Accesso richiesto'), 401) if request.path.startswith('/api/') else redirect('/login')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            supplied = request.headers.get('X-CSRF-Token', '')
            if not same_origin() or not supplied or not hmac.compare_digest(supplied.encode(), session.get('csrf', '').encode()):
                return jsonify(error='Richiesta non valida'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        # Preserve same-origin form Origin on HTTP localhost (SSH tunnels).
        # no-referrer makes Chromium send Origin: null for this POST.
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/healthz')
    def health():
        return jsonify(ok=True)

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/')
    def board():
        return render_template('board.html', host=socket.gethostname(), allow_send=app.config['ALLOW_SEND'],
                               accounts_enabled=app.config['ACCOUNTS_ENABLED'])

    def backend():
        from .tmux_backend import TmuxBackend
        return TmuxBackend(app.config['TMUX_SOCKET'])

    @app.get('/api/sessions')
    def sessions():
        from .tmux_backend import TmuxUnavailable
        try:
            rows = backend().sessions()
        except TmuxUnavailable:
            return jsonify(error='tmux non disponibile'), 503
        statuses = store.latest()
        now = time.time()
        for row in rows:
            reported = statuses.get(row['name'])
            # A same-name replacement must not inherit the previous session's state.
            if reported and reported['updated_at'] >= row['created_at']:
                row['report'] = reported
                row['stale'] = now - reported['updated_at'] > 600
            else:
                row['report'] = None
                row['stale'] = False
        return jsonify(sessions=rows, updated_at=now, allow_send=app.config['ALLOW_SEND'])

    @app.get('/api/metrics')
    def metrics():
        import psutil
        disk = psutil.disk_usage(app.config['STATE_DIR'])
        memory = psutil.virtual_memory()
        return jsonify(host=socket.gethostname(), cpu_percent=psutil.cpu_percent(),
                       memory_percent=memory.percent, disk_percent=disk.percent,
                       uptime_seconds=int(time.time() - psutil.boot_time()))

    @app.get('/api/panes/<int:pane>/output')
    def output(pane):
        from .tmux_backend import PaneMissing, TmuxUnavailable
        identity = request.args.get('identity', '')
        if not identity or len(identity) > 200:
            return jsonify(error='Identità del pannello richiesta'), 400
        try:
            return jsonify(output=backend().output(pane, identity))
        except PaneMissing:
            return jsonify(error='Pannello non più disponibile'), 404
        except TmuxUnavailable:
            return jsonify(error='tmux non disponibile'), 503

    @app.post('/api/panes/<int:pane>/send')
    def send(pane):
        from .tmux_backend import PaneMissing, TmuxUnavailable
        from .terminal_input import read_input
        if not app.config['ALLOW_SEND']:
            return jsonify(error='Invio disabilitato su questa istanza'), 403
        try:
            value = read_input(request)
            def write():
                if value.images:
                    from .terminal_delivery import send_images
                    send_images(account_services()['switch'],pane,value.identity,value.text,value.images)
                elif '\n' in value.text or value.chat_conversation is not None:
                    from .terminal_delivery import send_multiline
                    send_multiline(account_services()['switch'],pane,value.identity,value.text)
                else:
                    backend().send(pane, value.text, value.identity)
            if value.chat_conversation is not None:
                from .chat_delivery import deliver
                return jsonify(ok=True, chat_delivery=deliver(account_services()['switch'],pane,value,write))
            write()
        except ValueError as error:
            payload = {'error': str(error)}
            if hasattr(error, 'definite_rejection'):
                payload['definite_rejection'] = error.definite_rejection
            return jsonify(payload), getattr(error, 'status_code', 400)
        except PaneMissing:
            return jsonify(error='Pannello non più disponibile'), 404
        except TmuxUnavailable:
            return jsonify(error='Invio non riuscito; verifica il pannello prima di riprovare'), 503
        return jsonify(ok=True)

    @app.post('/api/events')
    def ingest():
        token = app.config['INGEST_TOKEN']
        supplied = request.headers.get('Authorization', '')
        if not token or not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
            return jsonify(error='Token richiesto'), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error='JSON non valido'), 400
        name, status, detail = payload.get('session'), payload.get('status'), payload.get('detail', '')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name)
                or not isinstance(status, str) or status not in {'working', 'waiting', 'blocked', 'idle', 'done'}
                or not isinstance(detail, str) or len(detail) > 1000):
            return jsonify(error='Stato non valido'), 400
        store.record(name, status, detail)
        return jsonify(ok=True)

    @app.get('/api/mcp-health')
    def mcp():
        if not app.config['MCP_ENABLED']:
            return jsonify(enabled=False, servers=[])
        from .mcp_health import get_health
        return jsonify(enabled=True, servers=get_health())

    return app
