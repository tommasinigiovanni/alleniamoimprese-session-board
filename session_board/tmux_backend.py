"""Local tmux adapter: explicit socket, numeric pane IDs and literal input."""
import os
import re
import secrets
import shlex
import subprocess
import threading

import psutil

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_STYLE_ESCAPE = re.compile(
    r'\x1b\[(?P<sgr>[0-9;:]*)m'
    r'|\x1b\]8;[^\x00-\x1f\x7f-\x9f;]*;[^\x00-\x1f\x7f-\x9f]*(?:\x07|\x1b\\)')


class TmuxUnavailable(Exception):
    pass


class PaneMissing(Exception):
    pass


class TmuxBackend:
    def __init__(self, socket_name='default'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', socket_name):
            raise ValueError('Nome socket tmux non valido')
        self.prefix = ['tmux', '-L', socket_name]

    def pane_lock(self, pane):
        if type(pane) is not int or pane < 0:
            raise ValueError('ID pannello non valido')
        key = (tuple(self.prefix), pane)
        with _LOCKS_GUARD:
            return _LOCKS.setdefault(key, threading.RLock())

    def run(self, args):
        env = {k: v for k, v in os.environ.items() if k != 'TMUX'}
        env['LC_ALL'] = 'C'
        try:
            return subprocess.run(self.prefix + args, capture_output=True, text=True,
                                  errors='replace', timeout=3, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TmuxUnavailable() from exc

    def sessions(self):
        fields = ['session_id', 'session_name', 'session_created', 'session_attached',
                  'pane_id', 'window_index', 'pane_index', 'pane_active',
                  'pane_current_command', 'pane_current_path', 'pid', 'pane_pid']
        result = self.run(['list-panes', '-a', '-F', ' '.join('#{q:' + field + '}' for field in fields)])
        if result.returncode:
            if any(reason in result.stderr for reason in ('no server running', 'No such file or directory', 'Connection refused')):
                return []
            raise TmuxUnavailable()
        sessions = {}
        server_started = {}
        for line in result.stdout.splitlines():
            try:
                parts = shlex.split(line)
            except ValueError:
                continue
            if len(parts) != len(fields):
                continue
            sid, name, created, attached, pane, window, index, active, command, cwd, server_pid, pane_pid = parts
            try:
                server_pid, pane_pid = int(server_pid), int(pane_pid)
                if server_pid not in server_started:
                    server_started[server_pid] = psutil.Process(server_pid).create_time()
                # Pane IDs restart at %0 when the server restarts. Bind browser
                # selections to this server incarnation and the actual process.
                identity = f'{server_pid}:{server_started[server_pid]:.6f}:{sid}:{created}:{pane_pid}:{pane}'
                item = {'id': int(pane.removeprefix('%')), 'window': int(window), 'index': int(index),
                        'active': active == '1', 'command': command, 'cwd': cwd, 'identity': identity}
                row = sessions.setdefault(sid, {'id': sid, 'name': name, 'created_at': int(created),
                                               'attached': attached != '0', 'panes': []})
            except ValueError:
                continue
            except psutil.Error as exc:
                raise TmuxUnavailable() from exc
            row['panes'].append(item)
        return sorted(sessions.values(), key=lambda row: row['name'].casefold())

    def target(self, pane, expected_identity):
        if type(pane) is not int or pane < 0:
            raise ValueError('ID pannello non valido')
        if not isinstance(expected_identity, str) or not any(
                p['id'] == pane and p['identity'] == expected_identity
                for row in self.sessions() for p in row['panes']):
            raise PaneMissing()
        return '%' + str(pane)

    def output(self, pane, expected_identity):
        target = '%' + str(pane)
        result = self._guarded(pane, expected_identity, [
            ['capture-pane', '-p', '-J', '-S', '-150', '-t', target],
        ])
        return result.stdout[-64000:]

    def composer_view(self, pane, expected_identity, *, expected_command=None):
        """Capture the visible grid and caret together for composer acks."""
        target = '%' + str(pane)
        result = self._guarded(pane, expected_identity, [
            ['display-message', '-p', '-t', target, '#{cursor_x} #{cursor_y} #{cursor_flag}'],
            ['capture-pane', '-p', '-e', '-t', target],
        ], expected_command=expected_command)
        header, separator, output = result.stdout.partition('\n')
        try:
            x, y, visible = (int(value) for value in header.split())
            if not separator or x < 0 or y < 0 or visible not in (0, 1):
                raise ValueError()
        except ValueError:
            raise PaneMissing() from None
        # Derive both representations from the same grid. Slicing either one
        # would invalidate the caret row or discard an earlier style reset.
        if len(output) > 256000:
            raise PaneMissing()
        plain = _STYLE_ESCAPE.sub('', output)
        if len(plain) > 64000 or any(char in plain for char in ('\x1b', '\x9b', '\x9d')):
            raise PaneMissing()
        return {'output':plain, 'styled_output':output,
                'cursor_x':x, 'cursor_y':y, 'cursor_visible':bool(visible)}

    def send(self, pane, text, expected_identity):
        target = '%' + str(pane)
        with self.pane_lock(pane):
            self._guarded(pane, expected_identity, [
                ['send-keys', '-t', target, '-l', '--', text],
                ['send-keys', '-t', target, 'Enter'],
            ])

    def send_key(self, pane, key, expected_identity, *, expected_command=None):
        """Send one explicit navigation key, preserving the selected process identity."""
        keys = {'ArrowUp': 'Up', 'ArrowDown': 'Down', 'ArrowLeft': 'Left',
                'ArrowRight': 'Right', 'Enter': 'Enter', 'Escape': 'Escape'}
        if not isinstance(key, str) or key not in keys:
            raise ValueError('Tasto non consentito')
        target = '%' + str(pane)
        with self.pane_lock(pane):
            self._guarded(pane, expected_identity, [
                ['send-keys', '-t', target, keys[key]],
            ], expected_command=expected_command)

    def paste(self, pane, text, expected_identity, *, submit=False, expected_command=None):
        """Emit an explicit paste event without touching a desktop clipboard."""
        if not isinstance(text, str) or any((ord(char) < 32 and char != '\n') or ord(char) == 127 for char in text):
            raise ValueError('Il testo incollato contiene caratteri di controllo')
        target = '%' + str(pane)
        commands = []
        if text:
            commands.append(['send-keys', '-t', target, '-l', '--', '\x1b[200~' + text + '\x1b[201~'])
        if submit:
            commands.append(['send-keys', '-t', target, 'Enter'])
        if not commands:
            return
        with self.pane_lock(pane):
            self._guarded(pane, expected_identity, commands, expected_command=expected_command)

    def respawn(self, pane, expected_identity, cwd, command):
        with self.pane_lock(pane):
            return self._guarded(pane, expected_identity, [
                # -c is a tmux format even when argv is shell-quoted.
                ['respawn-pane', '-k', '-c', cwd.replace('#', '##'), '-t', '%' + str(pane), command],
            ])

    def _guarded(self, pane, expected_identity, commands, *, expected_command=None):
        target = self.target(pane, expected_identity)
        server_pid, _, sid, created, pane_pid, pane_id = expected_identity.split(':')
        checks = [f'#{{==:#{{{field}}},{value}}}' for field, value in (
            ('pid', server_pid), ('session_id', sid), ('session_created', created),
            ('pane_pid', pane_pid), ('pane_id', pane_id),
        )]
        if expected_command is not None:
            if not isinstance(expected_command, str) or not re.fullmatch(r'[A-Za-z0-9_.+-]{1,128}', expected_command):
                raise PaneMissing()
            checks.append(f'#{{==:#{{pane_current_command}},{expected_command}}}')
        condition = checks.pop()
        for check in reversed(checks):
            condition = '#{&&:' + check + ',' + condition + '}'
        marker = 'BOARD_PANE_MISSING_' + secrets.token_hex(16)
        # -F evaluates a tmux format, never an external shell. Validation and
        # both send operations share one tmux connection, so a restarted server
        # cannot accept a stale numeric target between Python's check and I/O.
        branch = ' ; '.join(shlex.join(command) for command in commands)
        result = self.run(['if-shell', '-F', '-t', target, condition, branch,
                           shlex.join(['display-message', '-p', marker])])
        if result.returncode or result.stdout.strip() == marker:
            raise PaneMissing()
        return result
