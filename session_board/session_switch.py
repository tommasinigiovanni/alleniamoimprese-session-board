"""Restart a verified Claude process in its own tmux pane with another profile.

No default account credentials, global registries or launcher scripts are
imported. Paths and account authentication belong to the injected account
service. A successful response means the replacement process was observed;
the CLI can still require a trust prompt or report a resume error afterwards.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import time

import psutil

from .tmux_backend import PaneMissing, TmuxBackend

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_IDLE = {'idle', 'waiting', 'blocked', 'done'}
_VALUE_FLAGS = {'--model', '--effort', '--permission-mode'}
_MULTI_FLAGS = {'--add-dir', '--allowedTools', '--allowed-tools', '--disallowedTools', '--disallowed-tools', '--tools', '--mcp-config'}
_BOOL_FLAGS = {'--dangerously-skip-permissions', '--strict-mcp-config', '--disable-slash-commands'}
_MAX_SEARCH_PATH = 4096
_FORBIDDEN_IN_PATH = set('\n\r\0')


class SwitchError(ValueError):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


def _json(path, maximum=65536):
    try:
        with Path(path).open('r', encoding='utf-8') as handle:
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            return None
        value = json.loads(data)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _same_path(left, right):
    return Path(left).resolve() == Path(right).resolve()


def _invocation(process, cli):
    """Inspect executable positions only; never substring-search an argument."""
    argv = process.cmdline()
    if not argv:
        return None
    for index in (0, 1):
        if index >= len(argv):
            continue
        value = argv[index]
        if (Path(value).name == 'claude' or _same_path(value, cli)):
            return argv[index + 1:]
    return None


def _preserved_args(argv, own_settings):
    out = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        name, separator, value = arg.partition('=')
        if name in _BOOL_FLAGS and not separator:
            out.append(name)
        elif name == '--settings':
            if not separator and index + 1 < len(argv):
                index += 1
                value = argv[index]
            if value != own_settings:
                raise SwitchError('Lo switch non può sostituire le impostazioni --settings personalizzate: integra prima gli hook nella configurazione comune')
        elif name in _VALUE_FLAGS or name in _MULTI_FLAGS:
            if separator:
                out.extend([name, value])
            elif index + 1 < len(argv):
                index += 1
                out.extend([name, argv[index]])
                if name in _MULTI_FLAGS:
                    while index + 1 < len(argv) and not argv[index + 1].startswith('-'):
                        index += 1
                        out.append(argv[index])
        index += 1
    return out


class SwitchService:
    def __init__(self, tmux, accounts, state_dir):
        self.tmux = tmux
        self.accounts = accounts
        self.home = Path(accounts.home).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()

    def _pane(self, pane, identity):
        self.tmux.target(pane, identity)
        for row in self.tmux.sessions():
            for item in row['panes']:
                if item['id'] == pane and item['identity'] == identity:
                    return row, item
        raise PaneMissing()

    def _process(self, pane):
        pid = int(pane['identity'].split(':')[-2])
        try:
            parent = psutil.Process(pid)
            found = []
            codex = False
            authentication = False
            for process in [parent] + parent.children(recursive=True):
                try:
                    argv = process.cmdline()
                    args = _invocation(process, self.accounts.claude_bin)
                except psutil.NoSuchProcess:
                    # Short-lived helpers can finish during discovery. The
                    # pane process and the chosen CLI must still be verifiable.
                    if process.pid == parent.pid:
                        raise
                    continue
                if any(Path(value).name == 'codex' for value in argv[:2]):
                    codex = True
                if args is not None:
                    if args[:1] == ['auth']:
                        authentication = True
                        continue
                    found.append((process, args))
            if codex:
                return None, 'codex' if not found else 'claude-codex'
            if len(found) != 1:
                return None, ('auth' if authentication else 'shell') if not found else 'ambiguous'
            process, argv = found[0]
            return {'pid':process.pid, 'started_at':process.create_time(),
                    'cwd':process.cwd(), 'argv':argv, 'process':process}, 'claude'
        except (psutil.Error, OSError, ValueError):
            raise SwitchError('Impossibile verificare il processo del pannello') from None

    def _account(self, process):
        try:
            environment = process['process'].environ()
        except psutil.Error:
            raise SwitchError('Impossibile verificare l’abbonamento del processo') from None
        # Values of other environment variables are never returned; the ones
        # kept below travel no further than the relaunch of this same process.
        config = environment.get('CLAUDE_CONFIG_DIR')
        process_home = environment.get('HOME')
        # Preserve an existing sandbox marker, never infer or enable one.
        # The CLI uses this marker when an already-authorized root process
        # carries its existing --dangerously-skip-permissions option.
        process['safe_env'] = {'IS_SANDBOX':'1'} if environment.get('IS_SANDBOX') == '1' else {}
        # MCP servers and hooks are spawned by bare name (codegraph, node...):
        # a PATH rebuilt from the board drops the entries the session reached,
        # so keep the one the process was started with when it is usable.
        search_path = environment.get('PATH') or ''
        if 0 < len(search_path) <= _MAX_SEARCH_PATH and not set(search_path) & _FORBIDDEN_IN_PATH:
            process['safe_env']['PATH'] = search_path
        if process_home and not _same_path(process_home, self.home):
            raise SwitchError('Il processo appartiene a una home diversa da quella configurata')
        del environment
        if not config or _same_path(config, self.accounts.path_for('principale')):
            return 'principale'
        path = Path(config).resolve()
        name = path.name
        if path.parent != self.home or not name.startswith('.claude-'):
            raise SwitchError('Profilo Claude del processo non gestito da questa board')
        slug = name[len('.claude-'):]
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', slug):
            raise SwitchError('Profilo Claude del processo non riconosciuto')
        if not _same_path(path, self.accounts.path_for(slug)):
            raise SwitchError('Profilo Claude del processo non riconosciuto')
        return slug

    def _event_path(self, process):
        digest = hashlib.sha256(f'{process["pid"]}:{process["started_at"]:.6f}'.encode()).hexdigest()
        return self.state_dir / 'session-events' / (digest + '.json')

    def _record(self, process, account):
        event = _json(self._event_path(process))
        if (event and event.get('pid') == process['pid']
                and event.get('started_at') == process['started_at']
                and event.get('cwd') == process['cwd']):
            fallback = (event, True)
        else:
            fallback = ({}, False)
        config = Path(self.accounts.path_for(account))
        record = _json(config / 'sessions' / (str(process['pid']) + '.json'))
        if not record or record.get('cwd') != process['cwd']:
            return fallback
        if record.get('pid', process['pid']) != process['pid']:
            return fallback
        try:
            updated = float(record.get('updatedAt', 0))
            if updated > 1e12:
                updated /= 1000
            if updated < process['started_at'] - 2 or updated > time.time() + 5:
                return fallback
        except (ValueError, TypeError):
            return fallback
        if fallback[1]:
            # Keep the hook's process-bound conversation, but don't let its
            # startup idle state hide a more recent native busy transition.
            if updated > float(event.get('updated_at', 0)):
                event['status'] = record.get('status')
            return event, True
        return record, False

    def _transcripts(self, process, account):
        directory = Path(self.accounts.path_for(account)) / 'projects' / process['cwd'].replace('/', '-')
        candidates = {}
        try:
            for path in directory.glob('*.jsonl'):
                if len(candidates) >= 2000:
                    raise SwitchError('Troppi transcript nel progetto: selezione non verificabile')
                if _UUID.fullmatch(path.stem) and path.is_file() and not path.is_symlink():
                    candidates[path.stem] = path
        except OSError:
            raise SwitchError('Impossibile leggere i transcript del progetto') from None
        return candidates

    def _inspect(self, pane, identity):
        row, item = self._pane(pane, identity)
        process, engine = self._process(item)
        result = {'engine':engine, 'account':None, 'conversation_id':None,
                  'cwd':item['cwd'], 'switchable':False, 'reason':'Lo switch richiede un unico processo Claude nel pannello',
                  'candidates':[], 'status':None}
        if not process:
            return result, None
        account = self._account(process)
        record, hooked = self._record(process, account)
        candidates = self._transcripts(process, account)
        status = str(record.get('status') or '').strip().lower()
        current = None
        try:
            # A CLI can read old transcripts to preview/search history. Only
            # a writer is evidence of the conversation currently being saved.
            opened = {Path(entry.path).resolve() for entry in process['process'].open_files()
                      if any(flag in getattr(entry, 'mode', '') for flag in ('w', 'a', '+'))}
        except psutil.Error:
            opened = set()
        active = [sid for sid, path in candidates.items() if path.resolve() in opened]
        if len(active) == 1:
            current = active[0]
        elif hooked and record.get('conversation_id') in candidates:
            current = record['conversation_id']
        elif len(candidates) == 1 and record.get('sessionId') in candidates:
            current = record['sessionId']
        result.update(account=account, cwd=process['cwd'], conversation_id=current,
                      candidates=[{'id':sid, 'updated_at':path.stat().st_mtime} for sid, path in candidates.items()], status=status or None)
        if status not in _IDLE:
            result['reason'] = ('La sessione sta lavorando: attendi che sia ferma' if status in {'busy','working','running','thinking'}
                                else 'Stato del processo non verificabile: attendi un evento dello stato o configura gli hook')
        elif not candidates:
            result['reason'] = 'Nessun transcript riprendibile per questo progetto'
        elif current is None:
            result['reason'] = 'Conversazione ambigua: scegli esplicitamente il transcript da riprendere'
            result['requires_conversation'] = True
        else:
            result.update(switchable=True, reason='')
        process.update(account=account, candidates=candidates, row=row, pane=item,
                       active_conversations=active)
        return result, process

    def inspect(self, pane, identity):
        return self._inspect(pane, identity)[0]

    def inspect_chat(self, pane, identity):
        """Read existing conversations without widening account-switch policy."""
        details, process = self._inspect(pane, identity)
        if details['engine'] == 'codex':
            from .codex_chat import inspect_codex
            return inspect_codex(self, pane, identity)
        if not process:
            return details
        details['process_identity'] = f'{process["pid"]}:{process["started_at"]:.6f}'
        active = process['active_conversations']
        if active:
            details['conversation_id'] = active[0] if len(active) == 1 else None
            return details
        # A single transcript in a directory is not proof of ownership.
        # Keep only a process-bound hook until a native record is verified.
        details['conversation_id'] = None
        previous, hooked = self._record(process, details['account'])
        hook_updated = 0
        try:
            hook_updated = float(previous.get('updated_at', 0)) if hooked else 0
            if (hooked and math.isfinite(hook_updated)
                    and process['started_at'] - 2 <= hook_updated <= time.time() + 5
                    and previous.get('account') == details['account']
                    and isinstance(previous.get('conversation_id'), str)
                    and _UUID.fullmatch(previous['conversation_id'])):
                sid = previous['conversation_id']
                details['conversation_id'] = sid if sid in process['candidates'] else None
            else:
                hook_updated = 0
        except (ValueError, TypeError):
            hook_updated = 0
        config = Path(self.accounts.path_for(details['account']))
        record = _json(config / 'sessions' / (str(process['pid']) + '.json')) or {}
        # Native Claude registries identify the live CLI even while its
        # transcript writer is closed. PID alone is insufficient after reuse:
        # bind the kernel start ticks, machine and PID namespace as well.
        try:
            pid = process['pid']
            started = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            machine = Path('/etc/machine-id').read_text().strip()
            domain = f'linux:{machine}:{os.readlink("/proc/self/ns/pid")}'
            sid = record.get('sessionId')
            updated = float(record.get('updatedAt', 0))
            if updated > 1e12:
                updated /= 1000
            if (record.get('pid') == pid and record.get('procStart') == started
                    and record.get('pidDomain') == domain and machine
                    and record.get('cwd') == process['cwd']
                    and math.isfinite(updated)
                    and process['started_at'] - 2 <= updated <= time.time() + 5
                    and isinstance(sid, str) and _UUID.fullmatch(sid)
                    and process['process'].is_running()):
                if updated > hook_updated:
                    # /clear also updates the native SID. A newer native
                    # registry must invalidate an older hook, even before
                    # the new conversation has written its first turn.
                    details['conversation_id'] = sid if sid in process['candidates'] else None
        except (OSError, ValueError, TypeError, IndexError, psutil.Error):
            pass
        return details

    def active_sessions_for_account(self, slug):
        result = []
        for row in self.tmux.sessions():
            for item in row['panes']:
                process, engine = self._process(item)
                if process and self._account(process) == slug:
                    result.append(row['name'])
                elif engine in {'ambiguous', 'claude-codex'}:
                    raise SwitchError('Impossibile escludere sessioni attive su questo abbonamento')
        return sorted(set(result))

    def record_event(self, pane, identity, status, conversation_id=None, engine='claude'):
        if status not in {'busy','working','idle','waiting','blocked','done'} or engine != 'claude':
            raise SwitchError('Evento della sessione non valido', 400)
        row, item = self._pane(pane, identity)
        process, found = self._process(item)
        if not process or found != 'claude':
            raise SwitchError('Il pannello non contiene un unico processo Claude')
        account = self._account(process)
        if conversation_id is not None and (not isinstance(conversation_id, str) or not _UUID.fullmatch(conversation_id)):
            raise SwitchError('ID conversazione non valido', 400)
        path = self._event_path(process)
        previous = _json(path) or {}
        record = {'pid':process['pid'], 'started_at':process['started_at'], 'cwd':process['cwd'],
                  'account':account, 'status':status, 'updated_at':time.time(),
                  'conversation_id':conversation_id or previous.get('conversation_id')}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.event-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(record, handle)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _command(self, process, target_account, sid, pane):
        binary = shutil.which(str(self.accounts.claude_bin))
        if not binary:
            raise SwitchError('CLI Claude non disponibile', 503)
        config = Path(self.accounts.path_for(target_account))
        hook = shlex.join([sys.executable, '-m', 'session_board.session_switch', '--hook',
                          '--state-dir', str(self.state_dir), '--socket', self.tmux.prefix[-1],
                          '--home', str(self.home), '--cli', binary])
        settings = {'hooks': {event: [{'hooks':[{'type':'command', 'command':hook}]}]
                             for event in ('SessionStart','UserPromptSubmit','Stop','PreToolUse')}}
        own_settings = json.dumps(settings,separators=(',',':'))
        command = [binary, '--resume', sid] + _preserved_args(process['argv'], own_settings)
        command.extend(['--settings',own_settings])
        environment = {
            'HOME':str(self.home), 'PATH':os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            'TERM':'xterm-256color', 'LANG':'C.UTF-8',
            'TMUX_PANE':'%'+str(pane),
            'PYTHONPATH':str(Path(__file__).resolve().parents[1]),
        }
        # The native primary profile reads ~/.claude.json only when the
        # override is absent. Pointing explicitly at ~/.claude changes CLI
        # configuration lookup and drops primary MCP/account metadata.
        if target_account != 'principale':
            environment['CLAUDE_CONFIG_DIR'] = str(config)
        environment.update(process.get('safe_env', {}))
        return shlex.join(['env','-i'] + [f'{key}={value}' for key,value in environment.items()] + command)

    def _share_project(self, process, target_account):
        source = Path(self.accounts.path_for(process['account'])) / 'projects' / process['cwd'].replace('/', '-')
        shared_root = (Path(self.accounts.path_for('principale')) / 'projects').resolve()
        if not source.resolve().is_relative_to(shared_root):
            raise SwitchError('I transcript del profilo di origine non sono condivisi con il principale: importa prima il progetto per conservarlo dopo la rimozione dell’abbonamento')
        target_root = Path(self.accounts.path_for(target_account)) / 'projects'
        target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = target_root / source.name
        if target.exists():
            if target.resolve() != source.resolve():
                raise SwitchError('Il profilo di destinazione contiene un progetto diverso; unifica i transcript prima dello switch')
        else:
            target.symlink_to(source.resolve(), target_is_directory=True)

    def switch(self, pane, identity, target_account, conversation_id=None):
        with self.tmux.pane_lock(pane), self.accounts.mutation_lock:
            before, process = self._inspect(pane, identity)
            if not process or (not before['switchable'] and not before.get('requires_conversation')):
                raise SwitchError(before['reason'])
            if conversation_id is not None and (not isinstance(conversation_id,str) or not _UUID.fullmatch(conversation_id)):
                raise SwitchError('ID conversazione non valido', 400)
            sid = conversation_id or before['conversation_id']
            if sid not in process['candidates']:
                raise SwitchError('Scegli una conversazione disponibile nel progetto del pannello')
            if before['conversation_id'] and sid != before['conversation_id']:
                raise SwitchError('Il transcript scelto non è la conversazione attiva del pannello')
            account = self.accounts.require_account(target_account)
            if account.get('logged_in') is not True or account.get('error'):
                raise SwitchError('Abbonamento non autenticato: completa prima il login')
            command = self._command(process, target_account, sid, pane)
            self._share_project(process, target_account)
            latest, checked = self._inspect(pane, identity)
            if (not checked or latest['status'] not in _IDLE or checked['pid'] != process['pid']
                    or checked['started_at'] != process['started_at'] or latest['cwd'] != before['cwd']
                    or (latest['conversation_id'] and latest['conversation_id'] != sid)
                    or sid not in checked['candidates']):
                raise SwitchError('La sessione è cambiata durante la verifica: riapri il pannello')
            try:
                self.tmux.respawn(pane, identity, process['cwd'], command)
            except PaneMissing:
                raise
            except Exception:
                raise SwitchError('Riavvio non confermato: verifica il terminale prima di riprovare', 503) from None
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                for row in self.tmux.sessions():
                    for item in row['panes']:
                        if item['id'] != pane or item['identity'] == identity:
                            continue
                        replacement, engine = self._process(item)
                        if replacement and self._account(replacement) == target_account:
                            # CLI process visibility precedes startup messages/registry by
                            # a few milliseconds. Poll for the bound registry or hook.
                            record, hooked = self._record(replacement,target_account)
                            if not record or record.get('conversation_id' if hooked else 'sessionId') != sid:
                                continue
                            return {'ok':True, 'account':target_account, 'resumed':True,
                                    'new_identity':item['identity'], 'conversation_id':sid,
                                    'message':'Processo riavviato con resume; verifica nel terminale eventuali richieste del CLI'}
                time.sleep(.05)
            raise SwitchError('Il pannello è stato riavviato, ma Claude non ha confermato l’avvio. Apri il terminale per verificare il recupero', 503)


def _hook_main(args):
    """Local hook: never HTTP, no credentials and no external account login."""
    class LocalAccounts:
        home = Path(args.home)
        claude_bin = args.cli
        def path_for(self, slug):
            return self.home / ('.claude' if slug == 'principale' else '.claude-' + slug)
    data = json.loads(sys.stdin.read(65536))
    pane_text = os.environ.get('TMUX_PANE','')
    if not re.fullmatch(r'%\d+', pane_text):
        return
    pane = int(pane_text[1:])
    backend = TmuxBackend(args.socket)
    service = SwitchService(backend,LocalAccounts(),args.state_dir)
    event = data.get('hook_event_name')
    status = {'SessionStart':'idle', 'UserPromptSubmit':'busy', 'PreToolUse':'busy', 'Stop':'idle'}.get(event)
    if not status:
        return
    for row in backend.sessions():
        for item in row['panes']:
            if item['id'] == pane:
                process, engine = service._process(item)
                ancestors = {(parent.pid, parent.create_time()) for parent in psutil.Process().parents()}
                if not process or (process['pid'],process['started_at']) not in ancestors:
                    # An asynchronous hook belonging to an old CLI must never
                    # label the replacement now occupying the same pane ID.
                    return
                service.record_event(pane,item['identity'],status,data.get('session_id'))
                if event=='SessionStart':
                    account = service.inspect(pane,item['identity'])['account']
                    if account and account!='principale':
                        note = ('Questa sessione usa un abbonamento Claude secondario scelto dalla session board. '
                                'L’account titolare paga il consumo e non cambia interlocutore, autorizzazioni o identità git. '
                                'La directory di configurazione separata è intenzionale; segnala ogni altra incoerenza come sempre.')
                        print(json.dumps({'hookSpecificOutput':{'hookEventName':'SessionStart','additionalContext':note}}))
                return


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--hook',action='store_true')
    for key in ('state-dir','socket','home','cli'):
        parser.add_argument('--'+key,required=True)
    args = parser.parse_args()
    try:
        _hook_main(args)
    except Exception:
        # Hook failures must not break session startup or leak process context.
        pass
