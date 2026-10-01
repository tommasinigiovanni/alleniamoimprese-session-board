"""Scalar session-list metadata from an exactly verified Claude conversation."""
from datetime import datetime
from pathlib import Path
import re

from flask import Blueprint, jsonify, request

from .tmux_backend import PaneMissing, TmuxUnavailable
from .transcripts import MAX_TRANSCRIPT, UUID, _events


MAX_CONTEXT_TOKENS = 1_000_000_000
_TOKEN_FIELDS = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
_BINDING = ('engine', 'account', 'cwd', 'conversation_id', 'process_identity',
            'transcript_path', 'codex_home', 'transcript_identity')
_ENGINES = {'claude', 'codex', 'shell', 'ambiguous', 'claude-codex', 'auth', 'unknown'}
_ACCOUNT = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')


def _empty():
    return dict(engine='unknown', account=None, context_tokens=None, sampled_at=None)


def _timestamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    try:
        # Python 3.10 does not accept a literal UTC Z suffix.
        if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None:
            return value
    except ValueError:
        pass
    return None


def _usage(row):
    if row.get('type') != 'assistant' or any(row.get(flag) for flag in (
            'isMeta', 'isSidechain', 'isCompactSummary')):
        return None
    message = row.get('message')
    if not isinstance(message, dict) or message.get('role') not in (None, 'assistant'):
        return None
    usage = message.get('usage')
    if not isinstance(usage, dict) or not any(key in usage for key in _TOKEN_FIELDS):
        return None
    values = [usage.get(key, 0) for key in _TOKEN_FIELDS]
    if any(type(value) is not int or not 0 <= value <= MAX_CONTEXT_TOKENS for value in values):
        return None
    total = sum(values)
    return total if total <= MAX_CONTEXT_TOKENS else None


def _collect(service, details):
    result = _empty()
    engine = details.get('engine')
    if isinstance(engine, str) and engine in _ENGINES:
        result['engine'] = engine
    # Global Codex subscription observations do not identify this process's
    # account; token_count is not yet part of the verified reader contract.
    if engine != 'claude':
        return result
    account = details.get('account')
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        return result
    result['account'] = account
    sid, cwd = details.get('conversation_id'), details.get('cwd')
    if (not isinstance(sid, str) or not UUID.fullmatch(sid)
            or not isinstance(cwd, str) or not Path(cwd).is_absolute()):
        return result
    transcript = (Path(service.accounts.path_for(account)) / 'projects'
                  / cwd.replace('/', '-') / (sid + '.jsonl'))
    try:
        rows, _ = _events(transcript, sid, cwd, MAX_TRANSCRIPT)
    except OSError:
        return result
    for row, _ in rows:
        tokens = _usage(row)
        if tokens is not None:
            result.update(context_tokens=tokens, sampled_at=_timestamp(row.get('timestamp')))
    return result


def create_blueprint(service_factory, auth_ok):
    bp = Blueprint('session_summary', __name__)

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @bp.get('/api/panes/<int:pane>/summary')
    def summary(pane):
        if not auth_ok():
            return jsonify(error='Accesso richiesto'), 401
        identity = request.args.get('identity', '')
        if not identity or len(identity) > 200:
            return jsonify(error='Identità del pannello richiesta'), 400
        try:
            service = service_factory()
            try:
                details = service.inspect_chat(pane, identity)
            except (ValueError, OSError):
                return jsonify(_empty())
            data = _collect(service, details)
            if not auth_ok():
                return jsonify(error='Accesso richiesto'), 401
            try:
                current = service.inspect_chat(pane, identity)
            except (ValueError, OSError):
                return jsonify(error='La sessione è cambiata. Aggiorna l’elenco.'), 409
            if any(current.get(key) != details.get(key) for key in _BINDING):
                return jsonify(error='La sessione è cambiata. Aggiorna l’elenco.'), 409
            return jsonify(data)
        except PaneMissing:
            return jsonify(error='Pannello non più disponibile. Aggiorna l’elenco.'), 404
        except TmuxUnavailable:
            return jsonify(error='tmux non disponibile'), 503
        except (OSError, ValueError):
            return jsonify(_empty())

    return bp
