"""Safe tool activity labels and chronological groups for both chat readers.

Static categories accompany real tool names and sanitized supported inputs.
Descriptions, patches, tool output and reasoning are never display material.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re
import shlex

from .action_details import describe_details

MAX_ACTIONS = 100
_TOOLS = {
    'Read': ('📖', 'Lettura file'), 'NotebookRead': ('📖', 'Lettura file'),
    'Edit': ('✏️', 'Modifica file'), 'Write': ('✏️', 'Scrittura file'),
    'NotebookEdit': ('✏️', 'Modifica file'), 'apply_patch': ('✏️', 'Modifica file'),
    'Grep': ('🔎', 'Ricerca nel codice'), 'Glob': ('🔎', 'Ricerca file'),
    'Bash': ('🖥️', 'Esecuzione comando'), 'BashOutput': ('🖥️', 'Lettura esito comando'),
    'exec_command': ('🖥️', 'Esecuzione comando'), 'shell_command': ('🖥️', 'Esecuzione comando'),
    'shell': ('🖥️', 'Esecuzione comando'), 'exec': ('🖥️', 'Esecuzione strumenti'),
    'wait': ('⏳', 'Attesa attività'), 'write_stdin': ('🖥️', 'Interazione con il terminale'),
    'Task': ('🤖', 'Attività agente'), 'Agent': ('🤖', 'Attività agente'),
    'WebFetch': ('🌐', 'Lettura pagina web'), 'WebSearch': ('🌐', 'Ricerca web'),
    'TodoWrite': ('☑️', 'Aggiornamento attività'), 'update_plan': ('☑️', 'Aggiornamento piano'),
    'Artifact': ('📤', 'Creazione contenuto'), 'view_image': ('🖼️', 'Lettura immagine'),
    'request_user_input': ('💬', 'Richiesta di informazioni'),
}
_COMMANDS = {
    'git': ('🔀', 'Operazione Git'), 'gh': ('🔀', 'Operazione GitHub'),
    'pytest': ('🧪', 'Esecuzione test'),
    'npm': ('📦', 'Gestione progetto'), 'pnpm': ('📦', 'Gestione progetto'),
    'yarn': ('📦', 'Gestione progetto'), 'pip': ('📦', 'Gestione pacchetti'),
    'systemctl': ('⚙️', 'Gestione servizio'), 'service': ('⚙️', 'Gestione servizio'),
    'journalctl': ('📋', 'Lettura log'), 'docker': ('🐳', 'Operazione container'),
    'curl': ('🌐', 'Richiesta web'), 'wget': ('🌐', 'Richiesta web'),
    'ssh': ('🔐', 'Connessione remota'), 'scp': ('🔐', 'Trasferimento file'),
    'grep': ('🔎', 'Ricerca nel codice'), 'rg': ('🔎', 'Ricerca nel codice'),
    'find': ('🔎', 'Ricerca file'), 'ls': ('📂', 'Elenco file'), 'cat': ('📖', 'Lettura file'),
    'rm': ('🗑️', 'Rimozione file'), 'mv': ('📦', 'Spostamento file'),
    'cp': ('📄', 'Copia file'), 'mkdir': ('📂', 'Creazione cartella'),
    'python': ('▶️', 'Esecuzione Python'), 'python3': ('▶️', 'Esecuzione Python'),
    'node': ('▶️', 'Esecuzione JavaScript'), 'bash': ('🖥️', 'Esecuzione shell'),
    'sh': ('🖥️', 'Esecuzione shell'), 'ruff': ('🧹', 'Controllo codice'),
    'black': ('🧹', 'Formattazione codice'), 'tmux': ('🪟', 'Gestione terminale'),
    'psql': ('🗄️', 'Operazione database'), 'sqlite3': ('🗄️', 'Operazione database'),
}
_SHELL_TOOLS = {'Bash', 'exec_command', 'shell_command', 'shell'}


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 200


def _command(arguments):
    value = arguments.get('command', arguments.get('cmd'))
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        tokens = shlex.split(value)
    except ValueError:
        return None
    while tokens and (re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', tokens[0], re.S)
                      or tokens[0] in ('env', 'sudo', 'command', 'exec')):
        tokens.pop(0)
    if not tokens:
        return None
    verb = PurePosixPath(tokens[0]).name
    if verb in ('python', 'python3') and len(tokens) >= 3 and tokens[1] == '-m' and tokens[2] in _COMMANDS:
        verb, tokens = tokens[2], tokens[2:]
    if verb not in _COMMANDS:
        return None
    return _COMMANDS[verb]


def describe(name, arguments=None, namespace=None):
    """Combine static labels with the supported, redacted input projection."""
    arguments = arguments if isinstance(arguments, dict) else {}
    qualified = f'{namespace}.{name}' if isinstance(namespace, str) else name
    short = name.rsplit('.', 1)[-1]
    icon, label = _TOOLS.get(short, ('🔧', 'Uso strumento'))
    if qualified.startswith('collaboration.'):
        icon, label = '🤖', 'Collaborazione'
    elif qualified.startswith(('web.', 'web__')):
        icon, label = '🌐', 'Ricerca web'
    elif name.startswith(('mcp__qdrant', 'mcp__codegraph', 'mcp__')):
        icon, label = (('🧠', 'Ricerca nella memoria') if name.startswith('mcp__qdrant') else
                       ('🕸️', 'Esplorazione codice') if name.startswith('mcp__codegraph') else
                       ('🔌', 'Uso integrazione'))
    elif short in _SHELL_TOOLS:
        command = _command(arguments)
        if command:
            icon, label = command
    item = dict(icon=icon, label=label, **describe_details(name, arguments, namespace))
    return item


def codex_activity(row):
    """Normalize Codex's tool protocol while discarding all output/input bodies."""
    if not isinstance(row, dict) or row.get('type') != 'response_item':
        return None
    payload = row.get('payload')
    if not isinstance(payload, dict) or not _identifier(payload.get('call_id')):
        return None
    kind = payload.get('type')
    if not isinstance(kind, str):
        return None
    protocols = {'function_call': 'function', 'custom_tool_call': 'custom',
                 'function_call_output': 'function', 'custom_tool_call_output': 'custom'}
    if kind not in protocols:
        return None
    event = dict(protocol=protocols[kind], call_id=payload['call_id'])
    if kind.endswith('_output'):
        event.update(event='finish', status='error' if payload.get('is_error') is True else 'completed')
    else:
        name = payload.get('name')
        if not _identifier(name):
            return None
        raw_arguments = payload.get('arguments')
        arguments = raw_arguments
        if isinstance(arguments, str) and len(arguments) <= 65536:
            try:
                arguments = json.loads(arguments)
            except (ValueError, RecursionError):
                arguments = None
        else:
            arguments = None
        if name in ('exec', 'functions.exec'):
            script = payload.get('input') if kind == 'custom_tool_call' else raw_arguments
            if not isinstance(arguments, dict) and isinstance(script, str):
                arguments = {'code': script}
        event.update(event='start', **describe(name, arguments, payload.get('namespace')))
    return dict(type='activity', activity=event)


def _claude_events(row):
    if row.get('type') not in ('assistant', 'user') or any(
            row.get(flag) for flag in ('isMeta', 'isCompactSummary', 'isSidechain')):
        return
    message = row.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if row['type'] == 'assistant' and block.get('type') == 'tool_use':
            if _identifier(block.get('id')) and _identifier(block.get('name')):
                yield dict(protocol='claude', call_id=block['id'], event='start',
                           **describe(block['name'], block.get('input')))
        elif row['type'] == 'user' and block.get('type') == 'tool_result' and _identifier(block.get('tool_use_id')):
            yield dict(protocol='claude', call_id=block['tool_use_id'], event='finish',
                       status='error' if block.get('is_error') is True else 'completed')


class Timeline:
    def __init__(self, sid, engine):
        self.sid, self.engine = sid, engine
        self.items, self.actions, self.messages = [], {}, set()

    def message(self, message):
        identifier = message['id']
        if identifier not in self.messages:
            self.messages.add(identifier)
            self.items.append(identifier)

    def add(self, row, message=None):
        content = row.get('message', {}).get('content') if isinstance(row.get('message'), dict) else None
        if message and self.engine == 'claude' and isinstance(content, list):
            # One existing bubble can contain multiple text blocks. Place it at
            # its first visible block, keeping tool calls on either side ordered.
            for block in content:
                if (isinstance(block, dict) and (block.get('type') == 'text'
                        and isinstance(block.get('text'), str) and block['text'].strip()
                        or row.get('type') == 'user' and block.get('type') == 'image')):
                    self.message(message)
                self.row(dict(row, message={'content': [block]}))
            self.message(message)
        else:
            if message:
                self.message(message)
            self.row(row)

    def row(self, row):
        events = ([row['activity']] if self.engine == 'codex' and row.get('type') == 'activity'
                  else _claude_events(row) if self.engine == 'claude' else ())
        for event in events:
            identifier = hashlib.sha256(
                f'{self.sid}:action:{event["protocol"]}:{event["call_id"]}'.encode()).hexdigest()[:24]
            if event['event'] == 'finish':
                if identifier in self.actions:
                    self.actions[identifier]['status'] = event['status']
                continue
            if identifier in self.actions:
                continue
            action = dict(id=identifier, icon=event['icon'], label=event['label'], status='started')
            for key in ('tool', 'detail', 'detail_truncated'):
                if key in event:
                    action[key] = event[key]
            self.actions[identifier] = action
            if not self.items or isinstance(self.items[-1], str):
                self.items.append(dict(id='actions-' + identifier, role='actions', actions=[]))
            self.items[-1]['actions'].append(action)

    def render(self, messages):
        keep = set(list(self.actions)[-MAX_ACTIONS:])
        timeline = []
        for item in self.items:
            if isinstance(item, str):
                if item in messages:
                    timeline.append(messages[item])
            else:
                actions = [action for action in item['actions'] if action['id'] in keep]
                if actions:
                    timeline.append(dict(item, actions=actions))
        return timeline, len(self.actions) > MAX_ACTIONS
