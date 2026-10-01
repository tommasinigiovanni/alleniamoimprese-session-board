#!/usr/bin/env python3
"""MCP health observations, collected outside HTTP request threads.

The CLI probes fresh connections, not the MCP connections of active sessions.
Only explicit CLI connection failures mean ``Failed``. Missing output, CLI
errors and timeouts mean ``Unknown``. Never expose CLI output or exceptions:
commands, URLs and server diagnostics may contain credentials.

``get_health()`` retains the legacy list of name/status objects, with optional
``reason``, ``checked_at`` (Unix seconds) and ``stale`` fields. First reads
return Unknown immediately. A single daemon worker refreshes each process's
cache at most once per 30 seconds, including failures. Importing does no I/O.
"""
import os
import re
import shutil
import signal
import subprocess
import threading
import time

CORE_SERVERS = tuple(dict.fromkeys(
    name.strip() for name in os.environ.get(
        'MCP_SERVERS', 'qdrant-memory,session-manager,project-state,playwright'
    ).split(',') if name.strip()
))
CLAUDE_BIN = shutil.which('claude') or os.path.expanduser('~/.local/bin/claude')
CLAUDE_HOME = os.path.expanduser('~')
_TIMEOUT = 25
_CACHE_TTL = 30
_cache = {'ts': 0.0, 'data': None}
_lock = threading.Lock()
_worker = None
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_STATUS = re.compile(
    r' - (?:[✔✓✘✗×!⏸⊘?⚠]\ufe0f?\s*)?'
    r'(Connected\b|Failed to connect\b|Connection error\b|Needs authentication\b|'
    r'Pending approval\b|Disabled\b|Rejected\b|not configured\b)',
    re.IGNORECASE,
)
_STATUS_NAMES = {
    'connected': 'Connected',
    'failed to connect': 'Failed',
    'connection error': 'Failed',
    'needs authentication': 'Needs authentication',
    'pending approval': 'Pending approval',
    'disabled': 'Disabled',
    'rejected': 'Disabled',
    'not configured': 'Not configured',
}


def parse_mcp_list(output_text):
    """Parse only status markers, never incidental words in command/error text."""
    result = {}
    for raw in (output_text or '').splitlines():
        line = _ANSI.sub('', raw).strip()
        if line.startswith('Checking MCP server'):
            continue
        parts = re.split(r':\s+', line, maxsplit=1)
        if len(parts) != 2:
            continue
        name, tail = parts
        if not name.strip() or ' - ' not in tail:
            continue
        match = _STATUS.search(tail)
        result[name.strip()] = (
            _STATUS_NAMES[match.group(1).lower()] if match else 'Unknown'
        )
    return result


def _run_claude_list():
    """Probe with the configured profile/environment and a bounded lifetime.

    Each invocation gets a private process group, so timeout cleanup cannot
    terminate active Claude sessions and also stops its own stdio children.
    """
    env = os.environ.copy()
    env['HOME'] = os.path.expanduser(env.get('CLAUDE_HOME') or CLAUDE_HOME)
    # systemd commonly omits user-installed MCP executables such as codegraph.
    paths = (env.get('PATH') or os.defpath).split(os.pathsep)
    local_bin = os.path.join(env['HOME'], '.local', 'bin')
    if local_bin not in paths:
        paths.append(local_bin)
    env['PATH'] = os.pathsep.join(paths)
    command = [env.get('CLAUDE_BIN') or CLAUDE_BIN, 'mcp', 'list']
    cwd = os.path.expanduser(env.get('MCP_PROJECT_DIR') or env['HOME'])
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=env, cwd=cwd, start_new_session=True,
    ) as proc:
        try:
            output, _ = proc.communicate(timeout=_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # A server that detached from this group may still own a pipe.
            # Do not wait on its EOF after the CLI itself has been reaped.
            proc.wait(timeout=1)
            raise
        if proc.returncode:
            # Deliberately omit stdout/stderr from the exception object.
            raise subprocess.CalledProcessError(proc.returncode, command)
    return output or ''


def _unknown(reason, checked_at=None):
    return [dict(name=name, status='Unknown', reason=reason,
                 checked_at=checked_at, stale=False) for name in CORE_SERVERS]


def _refresh_health():
    try:
        output = _run_claude_list()
        parsed = parse_mcp_list(output)
        no_config = 'no mcp servers configured' in output.lower()
        checked_at = time.time()
        health = []
        for name in CORE_SERVERS:
            status = parsed.get(name, 'Not configured' if no_config else 'Unknown')
            health.append(dict(
                name=name, status=status,
                reason='status_unreported' if status == 'Unknown' else 'cli_reported',
                checked_at=checked_at, stale=False,
            ))
    except subprocess.TimeoutExpired:
        health = _unknown('cli_timeout', time.time())
    except FileNotFoundError:
        health = _unknown('cli_or_project_unavailable', time.time())
    except subprocess.CalledProcessError:
        health = _unknown('cli_error', time.time())
    except Exception:
        health = _unknown('probe_error', time.time())
    with _lock:
        _cache['data'] = health
        _cache['ts'] = time.monotonic()


def get_health():
    """Return the latest observation immediately; schedule one refresh if due.

    Cache expiry uses a monotonic clock from completion of the last attempt.
    Expired observations returned during refresh explicitly carry stale=True.
    CLI/process exceptions replace previous green states with Unknown, and
    are cached too. Returned dictionaries are copies, never shared cache data.
    """
    global _worker
    if not CORE_SERVERS:
        return []
    with _lock:
        stale = (_cache['data'] is None or
                 time.monotonic() - _cache['ts'] >= _CACHE_TTL)
        health = _cache['data'] or _unknown('check_in_progress')
        result = [dict(row, stale=stale and _cache['data'] is not None)
                  for row in health]
        if stale and (_worker is None or not _worker.is_alive()):
            _worker = threading.Thread(target=_refresh_health,
                                       name='mcp-health', daemon=True)
            try:
                _worker.start()
            except RuntimeError:
                _worker = None
                result = _unknown('probe_start_error', time.time())
                _cache.update(data=result, ts=time.monotonic())
                result = [dict(row) for row in result]
        return result
