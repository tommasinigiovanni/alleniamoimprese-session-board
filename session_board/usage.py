"""Read-only subscription observations, isolated by profile and cached 5 minutes.

Claude's usage format follows the existing claude-usage collector. Codex uses
account-bound local observations where available, otherwise its official
app-server read methods. No LLM turn, login, refresh request or quota reset is
sent. Raw tokens, provider errors and command output never enter public data.
"""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import queue
import stat
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .claude_accounts import config_dir


CACHE_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 5
RPC_TIMEOUT_SECONDS = 5
USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
MAX_JSON_BYTES = 128 * 1024
MAX_ROLLOUT_BYTES = 128 * 1024
MAX_ROLLOUT_FILES = 12
MAX_DIRECTORY_ENTRIES = 2000


def _object(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _percent(value):
    number = _number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _epoch(value):
    number = _number(value)
    if number is not None:
        return number if number > 0 else None
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return stamp.timestamp() if stamp.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _text(value, limit=120):
    if not isinstance(value, str):
        return None
    return ''.join(c for c in value if c.isprintable())[:limit].strip() or None


def _read_json(path):
    """Bounded regular-file read; reject symlinks and special files."""
    if path.parent.is_symlink():
        raise OSError('Profile unavailable')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise OSError('Profile unavailable')
        raw = source.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('Profile too large')
    return _object(json.loads(raw))


def _http_get_json(url, headers, timeout):
    request = Request(url, headers=headers, method='GET')
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('Response too large')
    return _object(json.loads(raw))


def _empty(message):
    return {'limits': [], 'scoped_limits': [], 'sampled_at': None,
            'stale': True, 'message': message}


def _claude_limits(payload):
    limits, scoped = [], []
    for key, label, minutes in [('five_hour', '5 ore', 300), ('seven_day', '7 giorni', 10080)]:
        window = _object(payload.get(key))
        used = _percent(window.get('utilization'))
        if used is not None:
            limits.append({'id': key, 'label': label, 'used_percent': used,
                           'resets_at': _epoch(window.get('resets_at')), 'window_minutes': minutes})
    for key, label in [('seven_day_sonnet', 'Sonnet'), ('seven_day_opus', 'Opus')]:
        window = _object(payload.get(key))
        used = _percent(window.get('utilization'))
        if used is not None:
            scoped.append({'id': key, 'label': label, 'used_percent': used,
                           'resets_at': _epoch(window.get('resets_at'))})
    for index, item in enumerate(payload.get('limits') if isinstance(payload.get('limits'), list) else []):
        item = _object(item)
        used = _percent(item.get('percent'))
        if item.get('kind') == 'weekly_scoped' and used is not None:
            model = _object(_object(item.get('scope')).get('model'))
            scoped.append({'id': 'scoped_' + str(index),
                           'label': _text(model.get('display_name')) or 'Modello',
                           'used_percent': used, 'resets_at': _epoch(item.get('resets_at'))})
    return limits, scoped


def _window_label(minutes):
    if minutes and minutes % 1440 == 0:
        return f'{int(minutes / 1440)} giorni'
    if minutes and minutes % 60 == 0:
        return f'{int(minutes / 60)} ore'
    return f'{int(minutes)} minuti' if minutes else 'Finestra'


def _codex_limits(payload):
    payload = _object(payload)
    by_id = payload.get('rateLimitsByLimitId') or payload.get('rate_limits_by_limit_id')
    if isinstance(by_id, dict) and by_id:
        buckets = list(by_id.values())
    else:
        buckets = [payload.get('rateLimits') or payload.get('rate_limits') or payload]
    limits = []
    for bucket in buckets:
        bucket = _object(bucket)
        bucket_id = _text(bucket.get('limitId') or bucket.get('limit_id')) or 'codex'
        name = _text(bucket.get('limitName') or bucket.get('limit_name')) or bucket_id
        for index, key in enumerate(('primary', 'secondary')):
            window = _object(bucket.get(key))
            used = _percent(window.get('usedPercent', window.get('used_percent')))
            minutes = _number(window.get('windowDurationMins', window.get('window_minutes')))
            if used is None:
                continue
            if minutes is not None and (minutes <= 0 or minutes > 525600):
                minutes = None
            label = _window_label(minutes)
            limits.append({'id': f'{bucket_id}_{index}',
                           'label': f'{name} · {label}' if len(buckets) > 1 else label,
                           'used_percent': used, 'window_minutes': minutes,
                           'resets_at': _epoch(window.get('resetsAt', window.get('resets_at')))})
    return limits


def _jwt_metadata(value):
    """Display metadata only; decoded claims never authorize board access."""
    if not isinstance(value, str) or len(value) > MAX_JSON_BYTES:
        return {}
    try:
        part = value.split('.')[1]
        return _object(json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))))
    except (ValueError, IndexError, TypeError):
        return {}


def _codex_account_rpc(home, binary):
    """Bounded stdio RPC; wait for initialize and request two read operations."""
    env = os.environ.copy()
    env['HOME'] = str(home)
    env['CODEX_HOME'] = str(home / '.codex')
    # An inherited API key can select a different principal from this profile.
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_ACCESS_TOKEN'):
        env.pop(key, None)
    try:
        process = subprocess.Popen(
            [binary, 'app-server', '--listen', 'stdio://'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(home), env=env, start_new_session=True,
        )
    except OSError:
        return {}
    messages = queue.Queue(maxsize=32)
    def read_stdout():
        try:
            for _ in range(512):
                line = process.stdout.readline(MAX_JSON_BYTES + 1)
                if not line or len(line) > MAX_JSON_BYTES:
                    break
                try:
                    messages.put_nowait(line)
                except queue.Full:
                    break
        except (OSError, ValueError):
            pass
    reader = threading.Thread(target=read_stdout, daemon=True, name='subscription-codex')
    reader.start()
    result = {}
    def send(message):
        process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()
    try:
        send({'id': 0, 'method': 'initialize', 'params': {'clientInfo': {
            'name': 'session_board', 'title': 'Session Board', 'version': '1.0.0'}}})
        initialized = False
        deadline = time.monotonic() + RPC_TIMEOUT_SECONDS
        while time.monotonic() < deadline and len(result) < 2:
            try:
                line = messages.get(timeout=max(0.001, min(0.1, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None or not reader.is_alive():
                    break
                continue
            try:
                message = _object(json.loads(line))
            except (ValueError, TypeError):
                continue
            message_id = message.get('id')
            if message_id == 0 and not initialized:
                if 'result' not in message or 'error' in message:
                    break
                initialized = True
                send({'method': 'initialized', 'params': {}})
                send({'method': 'account/read', 'id': 1, 'params': {'refreshToken': False}})
                send({'method': 'account/rateLimits/read', 'id': 2})
            elif initialized and type(message_id) is int and message_id in (1, 2):
                result[message_id] = message
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            process.stdin.close()
        except (AttributeError, OSError, ValueError):
            pass
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.stdout.close()
        except (AttributeError, OSError, ValueError):
            pass
    return result


class UsageService:
    def __init__(self, home, codex_bin=None):
        self.home = Path(home).expanduser().resolve()
        self.codex_bin = codex_bin
        self._guard = threading.Lock()
        self._locks = {}
        self._cache = {}
        self._codex_worker = None
        self._codex_fingerprint = None

    def _cached(self, key, fingerprint, build):
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            previous = self._cache.get(key)
            if previous and previous[0] == fingerprint and time.monotonic() - previous[1] < CACHE_SECONDS:
                return copy.deepcopy(previous[2])
            old = previous[2] if previous and previous[0] == fingerprint else None
            data = build(old)
            self._cache[key] = (fingerprint, time.monotonic(), data)
            return copy.deepcopy(data)

    def _codex_async(self, fingerprint, build, placeholder):
        """Never hold an HTTP request while the CLI checks its account."""
        with self._guard:
            self._codex_fingerprint = fingerprint
            cached = self._cache.get('codex')
            previous = cached[2] if cached and cached[0] == fingerprint else None
            fresh = previous is not None and time.monotonic() - cached[1] < CACHE_SECONDS
            if not fresh and self._codex_worker is None:
                def refresh():
                    try:
                        data = build(previous)
                    except Exception:
                        data = dict(previous or placeholder, usage_stale=True,
                                    message='Limiti Codex non aggiornabili; nuovo controllo tra cinque minuti.')
                    with self._guard:
                        if self._codex_fingerprint == fingerprint:
                            self._cache['codex'] = (fingerprint, time.monotonic(), data)
                        self._codex_worker = None
                self._codex_worker = threading.Thread(target=refresh, name='codex-usage', daemon=True)
                try:
                    self._codex_worker.start()
                except RuntimeError:
                    self._codex_worker = None
                    previous = dict(previous or placeholder, usage_stale=True,
                                    message='Controllo Codex non avviabile; nuovo tentativo tra cinque minuti.')
                    self._cache['codex'] = (fingerprint, time.monotonic(), previous)
            result = copy.deepcopy(previous or placeholder)
            result['refreshing'] = not fresh and self._codex_worker is not None
            if not fresh:
                result['usage_stale'] = True
            return result

    def account_usage(self, slug):
        directory = Path(config_dir(slug, self.home))
        token, expires = None, None
        try:
            credentials = _object(_read_json(directory / '.credentials.json').get('claudeAiOauth'))
            token = credentials.get('accessToken')
            expires = _number(credentials.get('expiresAt'))
        except (OSError, ValueError):
            pass
        if not isinstance(token, str) or not token or (expires is not None and expires / 1000 <= time.time()):
            self._cache.pop('claude:' + slug, None)
            return _empty('Accesso da completare o rinnovare per leggere i limiti.')
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        def build(previous):
            message = None
            try:
                payload = _object(_http_get_json(USAGE_URL, {
                    'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20',
                }, timeout=HTTP_TIMEOUT_SECONDS))
                limits, scoped = _claude_limits(payload)
                return {'limits': limits, 'scoped_limits': scoped, 'sampled_at': time.time(),
                        'stale': not bool(limits or scoped),
                        'message': None if limits or scoped else 'Il provider non ha comunicato finestre di utilizzo.'}
            except HTTPError as error:
                if error.code in (401, 403):
                    message = 'Accesso da rinnovare per leggere i limiti.'
                elif error.code == 429:
                    message = 'Il provider ha limitato i controlli; nuovo tentativo tra cinque minuti.'
            except (OSError, ValueError, TypeError):
                pass
            message = message or 'Limiti non aggiornabili; nuovo tentativo tra cinque minuti.'
            return dict(previous, stale=True, message=message) if previous else _empty(message)
        return self._cached('claude:' + slug, fingerprint, build)

    def _local_codex_sample(self, account_id):
        if not account_id:
            return None
        root = self.home / '.codex' / 'sessions'
        if root.is_symlink() or root.parent.is_symlink():
            return None
        candidates = []
        now = datetime.now(timezone.utc)
        # Fixed recent date directories bound metadata work as well as bytes read.
        for days in range(7):
            day = root / (now - timedelta(days=days)).strftime('%Y/%m/%d')
            if any(p.is_symlink() for p in (day, day.parent, day.parent.parent)):
                continue
            try:
                with os.scandir(day) as entries:
                    for entry in itertools.islice(entries, MAX_DIRECTORY_ENTRIES):
                        if entry.name.startswith('rollout-') and entry.name.endswith('.jsonl') and entry.is_file(follow_symlinks=False):
                            candidates.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
            except OSError:
                continue
        best = None
        for _, path in sorted(candidates, reverse=True)[:MAX_ROLLOUT_FILES]:
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'rb') as source:
                    size = os.fstat(source.fileno()).st_size
                    header = source.readline(MAX_ROLLOUT_BYTES)
                    meta = _object(json.loads(header))
                    if meta.get('type') != 'session_meta' or _object(meta.get('payload')).get('account_id') != account_id:
                        continue
                    offset = max(0, size - MAX_ROLLOUT_BYTES)
                    source.seek(offset)
                    lines = source.read(MAX_ROLLOUT_BYTES).splitlines()
                    if offset:
                        lines = lines[1:]
                for raw in reversed(lines):
                    try:
                        event = _object(json.loads(raw))
                    except (ValueError, TypeError):
                        continue
                    payload = _object(event.get('payload'))
                    stamp = _epoch(event.get('timestamp'))
                    if event.get('type') != 'event_msg' or payload.get('type') != 'token_count' or stamp is None or stamp > time.time() + 60:
                        continue
                    # An explicit account on an event overrides session metadata.
                    if payload.get('account_id', account_id) != account_id:
                        continue
                    limits = _codex_limits(payload.get('rate_limits'))
                    if limits and (best is None or stamp > best['sampled_at']):
                        best = {'limits': limits, 'sampled_at': stamp}
            except (OSError, ValueError, TypeError):
                continue
        return best

    def codex_status(self):
        try:
            auth = _read_json(self.home / '.codex' / 'auth.json')
        except (OSError, ValueError):
            auth = {}
        fingerprint = hashlib.sha256(json.dumps(auth, sort_keys=True).encode()).hexdigest()
        tokens = {} if auth.get('auth_mode') == 'apikey' else _object(auth.get('tokens'))
        claims = _jwt_metadata(tokens.get('id_token'))
        account_claims = _object(claims.get('https://api.openai.com/auth'))
        configured = bool(tokens.get('access_token') or auth.get('OPENAI_API_KEY'))
        base = {'id': 'codex', 'status': 'configured' if configured else 'unavailable',
                'plan': (_text(account_claims.get('chatgpt_plan_type')) or '').replace('_', ' ').title() or None,
                'account': _text(claims.get('email')), 'limits': [], 'scoped_limits': [],
                'sampled_at': None, 'usage_stale': True, 'source': 'local_metadata',
                'message': 'Credenziali locali presenti; login non verificato.' if configured else 'Codex non disponibile o accesso da completare.'}
        def build(previous):
            data = copy.deepcopy(base)
            local = self._local_codex_sample(tokens.get('account_id') or account_claims.get('chatgpt_account_id'))
            if local:
                data.update(local, source='local_session', usage_stale=time.time() - local['sampled_at'] > CACHE_SECONDS,
                            message='Ultimo campione della sessione locale; aggiornato quando Codex lavora.')
                if not data['usage_stale']:
                    return data
            if self.codex_bin:
                rpc = _codex_account_rpc(self.home, self.codex_bin)
                account_response = _object(rpc.get(1))
                account_result = _object(account_response.get('result'))
                account = account_result.get('account')
                if isinstance(account, dict):
                    data.update(status='active', account=_text(account.get('email')),
                                plan=(_text(account.get('planType')) or '').replace('_', ' ').title() or None,
                                limits=[], sampled_at=None, usage_stale=True,
                                source='codex_app_server')
                    # The CLI may select another workspace or a keyring account.
                    # Old file-bound limits cannot be attached to that identity.
                    local = None
                elif 'account' in account_result:
                    data.update(status='signed_out', plan=None, account=None, limits=[], sampled_at=None,
                                usage_stale=True, message='Accesso Codex da completare.')
                    return data
                rate_result = _object(_object(rpc.get(2)).get('result'))
                limits = _codex_limits(rate_result)
                if isinstance(account, dict) and limits:
                    data.update(limits=limits, sampled_at=time.time(), usage_stale=False,
                                source='codex_app_server', message=None)
                elif not local:
                    data['message'] = 'Limiti Codex non disponibili; nuovo controllo tra cinque minuti.'
            return data
        result = (self._codex_async(fingerprint, build, base) if self.codex_bin
                  else self._cached('codex', fingerprint, build))
        if result['sampled_at'] is not None and time.time() - result['sampled_at'] > CACHE_SECONDS:
            result['usage_stale'] = True
        return result
