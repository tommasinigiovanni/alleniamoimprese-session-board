"""Answer only a currently verified CLI menu in the selected pane."""
from functools import wraps
import hashlib
import json
import math
import re
import secrets
import threading
import time

from flask import Blueprint, jsonify, request

from .action_details import sanitize_detail, sanitize_label
from .terminal_delivery import _snapshot, DeliveryError
from .tmux_backend import PaneMissing, TmuxUnavailable
from .transcripts import UUID

_QUESTION = re.compile(r'^\s*(?:Do you want to (?:proceed|continue|create [^\r\n?]{1,240}|make this edit to [^\r\n?]{1,240}|allow this connection)|Would you like to (?:run the following command|make the following edits|grant these permissions|send input to the existing terminal))\?\s*$', re.I)
_OPTION = re.compile(r'^\s*([❯›>])?\s*([1-9])[.)]?\s+(.{1,600}?)\s*$')
_FOOTER = re.compile(r'^\s*(?:Esc|Escape|Enter|Press (?:enter|Enter)|↑|↓|Use (?:arrow|the arrow)).{0,180}\s*$', re.I)
_BORDER = re.compile(r'^[\s─━═╭╮╰╯┌┐└┘┬┴┼├┤]+$')
_BASH_HEADER = re.compile(r'^\s*Bash command(?:\s+·\s+from the [^\r\n]{1,120} agent)?\s*$')
_RAIL = re.compile(r'^[ \t]*[│|][ \t]?(.*)$')
_PREVIEW_FALLBACK = 'Anteprima non disponibile. Apri il terminale per verificare l’operazione richiesta.'
_CHOICE_OPTION = re.compile(r'^[ \t]*([❯›>])?[ \t]*([1-9])[.)][ \t]+(?P<text>\S.{0,1199}?)\s*$')
_CHOICE_FOOTER = re.compile(r'^\s*Press enter to confirm or esc to (?:go back|cancel)\s*$', re.I)
_CHOICE_TITLE = re.compile(r'^(?:[^\r\n?]{1,500}\?|Select (?:Model(?: and Effort)?|Reasoning Level for .{1,200}))$', re.I)
_CHOICE_INPUT = re.compile(r'^(?:Other\b|Type (?:something|your)\b|None of the above\b)', re.I)
_FORM_HEADER = re.compile(r'^\s*←\s+(?:[☐☑☒]\s+[^☐☑☒✔→\r\n]{1,80}\s+){1,4}✔\s+Submit\s+→\s*$')
_FORM_TAB = re.compile(r'([☐☑☒])\s+([^☐☑☒✔→\r\n]{1,80}?)(?=\s+[☐☑☒✔]\s+)')
_FORM_FOOTER = re.compile(r'^\s*Enter to select\s*·\s*Tab/Arrow keys to navigate\s*·\s*Esc to cancel\s*$', re.I)
_FORM_OPTION = re.compile(r'^\s*([❯›>])?\s*([1-9])[.)]\s+(\S.{0,1199}?)\s*$')
_FORM_STATUS = re.compile(r'^\s*› Message from @[a-zA-Z0-9_-]{1,80}:.{0,500}$')


def _parse_approval(view, engine=None):
    if not isinstance(view, dict) or not isinstance(view.get('output'), str) or len(view['output']) > 64000:
        return None
    lines = [line.strip('│') if line.startswith('│') and line.endswith('│') else line
             for line in view['output'].splitlines()]
    cursor = view.get('cursor_y', -1)
    if type(cursor) is not int or not 0 <= cursor < len(lines):
        return None
    starts = [index for index, line in enumerate(lines) if _QUESTION.fullmatch(line)]
    if not starts:
        return None
    start = starts[-1]
    choices, selected, preview, footer, last = [], [], [], False, start
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or _BORDER.fullmatch(line):
            continue
        option = _OPTION.fullmatch(line)
        if option:
            number = int(option[2])
            if footer or number != len(choices) + 1:
                return None
            choices.append(dict(id=number, label=option[3]))
            if option[1]: selected.append(number)
        elif choices and _FOOTER.fullmatch(line):
            footer = True
        elif not choices and engine == 'codex' and len(preview) < 15 and line.startswith('  '):
            preview.append(line)
        else:
            return None
        last = index
    if not footer or not 2 <= len(choices) <= 9 or len(selected) != 1 or not start <= cursor <= last:
        return None
    # Include the visible frame in the binding without returning its contents.
    # Ignore only the selected marker, which verified arrow navigation changes.
    normalized = []
    for line in lines:
        option = _OPTION.fullmatch(line)
        normalized.append(f'{option[2]}. {option[3]}' if option else line.rstrip())
    screen = hashlib.sha256('\n'.join(normalized).encode()).hexdigest()
    # Keep the command's beginning even when a heredoc exceeds fifteen rows.
    # A previous approval's header cannot describe the current request.
    boundary = starts[-2] + 1 if len(starts) > 1 else 0
    headers = [index for index in range(boundary, start) if _BASH_HEADER.fullmatch(lines[index])]
    context_lines = preview if engine == 'codex' else lines[headers[-1]:start] if headers else []
    # The visible frame is already bounded. Redact the whole command before
    # sanitize_detail applies its smaller public display limit.
    context = '\n'.join(context_lines).strip()
    return dict(question=lines[start].strip(), context=context, choices=choices,
                selected=selected[0], _screen=screen)


def _parse_choice(view):
    """Recognize the Codex single-choice widget, not multi-question/input forms."""
    if (not isinstance(view, dict) or not isinstance(view.get('output'), str)
            or len(view['output']) > 64000 or view.get('cursor_visible') is not False):
        return None
    lines = view['output'].splitlines()
    cursor = view.get('cursor_y', -1)
    if type(cursor) is not int or not 0 <= cursor < len(lines):
        return None
    end = len(lines) - 1
    while end >= 0 and (not lines[end].strip() or _BORDER.fullmatch(lines[end])):
        end -= 1
    if end < 0 or not _CHOICE_FOOTER.fullmatch(lines[end]):
        return None
    starts = [index for index, line in enumerate(lines[:end])
              if (match := _CHOICE_OPTION.fullmatch(line)) and match[2] == '1']
    if not starts:
        return None
    start = starts[-1]
    header_end = start - 1
    while header_end >= 0 and not lines[header_end].strip():
        header_end -= 1
    header_start = header_end
    while header_start > 0 and lines[header_start - 1].strip() and not _BORDER.fullmatch(lines[header_start - 1]):
        header_start -= 1
    header = [line.strip() for line in lines[header_start:header_end + 1]]
    if (not header or len(header) > 6 or sum(map(len, header)) > 2000
            or not header_start <= cursor <= end):
        return None
    # A rejected approval cannot use this fallback, even if its title wraps or
    # a long preview separates it from another question. Completed menus may
    # remain above the live one, so stop at the preceding Codex menu footer.
    boundary = max((index + 1 for index in range(header_start)
                    if _CHOICE_FOOTER.fullmatch(lines[index])), default=0)
    active = [line.strip() for line in lines[boundary:start] if line.strip()]
    if any(_QUESTION.fullmatch(' '.join(active[first:last]))
           for first in range(len(active)) for last in range(first + 1, min(len(active), first + 6) + 1)):
        return None
    titles = [index for index, line in enumerate(header) if _CHOICE_TITLE.fullmatch(line)]
    if len(titles) != 1:
        return None
    title = titles[0]
    if any(re.search(r'Question \d+/\d+|navigate questions|notes \(tab\)|space.*(?:toggle|select)', line, re.I)
           for line in header):
        return None
    choices, selected, rows = [], [], {}
    label_column = 0
    for index in range(start, end):
        line = lines[index]
        if not line.strip():
            continue
        if any(char in line for char in '☐☒☑□■'):
            return None
        option = _CHOICE_OPTION.fullmatch(line)
        if option:
            number = int(option[2])
            if number != len(choices) + 1:
                return None
            pieces = re.split(r'[ \t]{2,}', option['text'], maxsplit=1)
            label, description = pieces[0], pieces[1] if len(pieces) == 2 else ''
            if _CHOICE_INPUT.match(label):
                return None
            choices.append(dict(id=number, label=label, description=description))
            if option[1]:
                selected.append(number)
            label_column = option.start('text')
            rows[index] = f'{number}. {option["text"]}'
        elif (choices and len(line) - len(line.lstrip()) >= label_column
              and not _OPTION.fullmatch(line) and not _FOOTER.fullmatch(line)
              and not line.lstrip().startswith(('›', '❯', '>', '…'))):
            choice = choices[-1]
            key = 'description' if choice['description'] or len(line) - len(line.lstrip()) > label_column else 'label'
            choice[key] = (choice[key] + ' ' + line.strip()).strip()
        else:
            return None
        if len(choices[-1]['label']) > 600 or len(choices[-1]['description']) > 1200:
            return None
    if not 2 <= len(choices) <= 9 or len(selected) != 1:
        return None
    screen = hashlib.sha256('\n'.join(rows.get(index, line.rstrip())
                            for index, line in enumerate(lines)).encode()).hexdigest()
    return dict(kind='choice', question=header[title],
                context='\n'.join(line for index, line in enumerate(header) if index != title),
                choices=choices, selected=selected[0], _screen=screen)


def _parse_claude_form(view):
    """Recognize the live tabbed AskUserQuestion picker and its fixed options."""
    if (not isinstance(view, dict) or not isinstance(view.get('output'), str)
            or len(view['output']) > 64000 or view.get('cursor_visible') is not False):
        return None
    lines = view['output'].splitlines()
    cursor = view.get('cursor_y', -1)
    if type(cursor) is not int or not 0 <= cursor < len(lines):
        return None
    headers = [index for index, line in enumerate(lines) if _FORM_HEADER.fullmatch(line)]
    if not headers:
        return None
    start = headers[-1]
    tabs = _FORM_TAB.findall(lines[start])
    if not 1 <= len(tabs) <= 4:
        return None
    footers = [index for index in range(start + 1, len(lines)) if _FORM_FOOTER.fullmatch(lines[index])]
    if len(footers) != 1:
        return None
    end = footers[0]
    if not start <= cursor <= end or end - start > 48:
        return None
    trailing = [line for line in lines[end + 1:] if line.strip()]
    if len(trailing) > 1 or trailing and not _FORM_STATUS.fullmatch(trailing[0]):
        return None
    first = next((index for index in range(start + 1, end)
                  if (match := _FORM_OPTION.fullmatch(lines[index])) and match[2] == '1'), None)
    if first is None:
        return None
    title = [line.strip().removeprefix('│').strip() for line in lines[start + 1:first]
             if line.strip() and not _BORDER.fullmatch(line)]
    question = ' '.join(title)
    if (not 1 <= len(title) <= 4 or not any(char in question for char in '?:')
            or len(question) > 500):
        return None
    choices, selected, normalized = [], [], {}
    special = False
    for index in range(first, end):
        line = lines[index]
        if not line.strip() or _BORDER.fullmatch(line):
            continue
        option = _FORM_OPTION.fullmatch(line)
        if option:
            number, label = int(option[2]), option[3].strip()
            if number != len(choices) + 1 or len(label) > 600:
                return None
            if option[1]:
                selected.append(number)
            if _CHOICE_INPUT.match(label) or label == 'Chat about this':
                special = True
            elif special:
                return None
            choices.append(dict(id=number, label=label, description=''))
            normalized[index] = f'{number}. {label}'
        elif choices and not special and len(line) - len(line.lstrip()) >= 3:
            value = (choices[-1]['description'] + ' ' + line.strip()).strip()
            if len(value) > 1800:
                return None
            choices[-1]['description'] = value
        else:
            return None
    fixed = [item for item in choices if not (_CHOICE_INPUT.match(item['label']) or item['label'] == 'Chat about this')]
    if not 2 <= len(fixed) <= 9 or len(selected) != 1:
        return None
    frame = '\n'.join(normalized.get(index, lines[index].rstrip()) for index in range(start, end + 1))
    screen = hashlib.sha256(frame.encode()).hexdigest()
    context = ' · '.join(f'{name.strip()} {"completata" if marker in "☑☒" else "da scegliere"}'
                         for marker, name in tabs)
    return dict(kind='choice', question=question, context=context,
                choices=fixed, selected=selected[0], _screen=screen, _form=True)


def _parse_claude_review(view):
    """Expose the final submit confirmation only after every tab is answered."""
    if (not isinstance(view, dict) or not isinstance(view.get('output'), str)
            or len(view['output']) > 64000 or view.get('cursor_visible') is not False):
        return None
    lines = view['output'].splitlines()
    cursor = view.get('cursor_y', -1)
    if type(cursor) is not int or not 0 <= cursor < len(lines):
        return None
    headers = [index for index, line in enumerate(lines) if _FORM_HEADER.fullmatch(line)]
    if not headers:
        return None
    start = headers[-1]
    tabs = _FORM_TAB.findall(lines[start])
    if not tabs or any(marker not in '☑☒' for marker, _ in tabs):
        return None
    titles = [index for index in range(start + 1, min(len(lines), start + 6))
              if lines[index].strip() == 'Review your answers']
    if len(titles) != 1:
        return None
    title = titles[0]
    ready = [index for index in range(title + 1, min(len(lines), title + 33))
             if lines[index].strip() == 'Ready to submit your answers?']
    if len(ready) != 1 or any('You have not answered all questions' in line
                              for line in lines[title:ready[0]]):
        return None
    first = ready[0] + 1
    while first < len(lines) and not lines[first].strip():
        first += 1
    if first + 1 >= len(lines):
        return None
    options = [_FORM_OPTION.fullmatch(lines[index]) for index in (first, first + 1)]
    if (any(option is None for option in options)
            or [(int(option[2]), option[3].strip()) for option in options]
               != [(1, 'Submit answers'), (2, 'Cancel')]
            or len([option for option in options if option[1]]) != 1
            or not start <= cursor <= first + 1):
        return None
    trailing = [line for line in lines[first + 2:] if line.strip()]
    if len(trailing) > 1 or trailing and not _FORM_STATUS.fullmatch(trailing[0]):
        return None
    normalized = {first: '1. Submit answers', first + 1: '2. Cancel'}
    frame = '\n'.join(normalized.get(index, lines[index].rstrip())
                      for index in range(start, first + 2))
    screen = hashlib.sha256(frame.encode()).hexdigest()
    summary = '\n'.join(line.strip() for line in lines[title + 1:ready[0]] if line.strip())
    return dict(kind='choice', question='Confermi le risposte del questionario?',
                context=summary, choices=[dict(id=1, label='Invia risposte', description=''),
                                          dict(id=2, label='Annulla', description='')],
                selected=int(next(option[2] for option in options if option[1])), _screen=screen,
                _review=True)


def parse_question(view, engine=None):
    approval = _parse_approval(view, engine)
    if approval:
        return approval
    if engine == 'codex':
        return _parse_choice(view)
    return (_parse_claude_form(view) or _parse_claude_review(view)) if engine == 'claude' else None


def _command_context(lines):
    headers = [index for index, line in enumerate(lines) if _BASH_HEADER.fullmatch(line)]
    if headers:
        index = headers[-1] + 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index == len(lines):
            return None
        if _RAIL.fullmatch(lines[index]):
            command = []
            while index < len(lines):
                rail = _RAIL.fullmatch(lines[index])
                if not rail:
                    break
                command.append(rail[1])
                index += 1
            return '\n'.join(command).strip()
        # Older Claude layouts use one indented command without a rail. Do
        # not append the prose description or permission rule underneath it.
        return (lines[index].strip() if lines[headers[-1]].strip() == 'Bash command'
                and index == headers[-1] + 1 and lines[index].startswith(('  ', '\t')) else None)
    commands = [line.lstrip()[2:] for line in lines if line.lstrip().startswith('$ ')]
    return commands[0] if len(commands) == 1 else None


def _command_detail(command):
    if not command:
        return _PREVIEW_FALLBACK
    # Recover only this literal executable/operation/flag prefix. The shell
    # payload stays omitted; no interpolation, token execution or evaluation.
    if (re.match(r'^git[ \t]+commit[ \t]+-m[ \t]+', command)
            and any(marker in command for marker in ('\n', '\r', '<<', '$(', '`'))):
        return 'git commit -m [script omesso]'
    return sanitize_detail(command)['detail'] or _PREVIEW_FALLBACK


def _public(question):
    if question.get('kind') == 'choice':
        return dict(question=sanitize_label(question['question'])[:500],
                    context=sanitize_label(question['context'])[:2000],
                    choices=[dict(id=item['id'], label=sanitize_label(item['label'])[:600],
                                  description=sanitize_label(item['description'])[:1200])
                             for item in question['choices']], selected=question['selected'],
                    review=question.get('_review') is True)
    # Show only the current command preview, never preceding tool output/diffs.
    command = _command_context(question['context'].splitlines())
    return dict(question=sanitize_label(question['question'])[:500],
                context=_command_detail(command),
                choices=[dict(id=item['id'], label=sanitize_label(item['label'])[:600])
                         for item in question['choices']], selected=question['selected'])


def _fingerprint(pane, identity, process, question, *, selection=True):
    value = question if selection else {key: value for key, value in question.items() if key != 'selected'}
    return hashlib.sha256(json.dumps([pane, identity, process, value], sort_keys=True).encode()).hexdigest()


class Questions:
    def __init__(self, service, auth_ok=lambda: True, allow_answer=lambda: True):
        self.service, self.auth_ok, self.allow_answer = service, auth_ok, allow_answer
        self.tokens, self.blocked = {}, {}
        self.lock = threading.RLock()

    @staticmethod
    def identity(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 200:
            raise ValueError('Identità del pannello richiesta')
        return value

    def access(self, *, write=False):
        if not self.auth_ok():
            raise DeliveryError('Accesso richiesto', 401)
        if write and not self.allow_answer():
            raise DeliveryError('Invio disabilitato su questa istanza', 403)

    def current(self, service, pane, identity):
        self.access()
        process = _snapshot(service, pane, identity)
        def conversation():
            inspect = getattr(service, 'inspect_chat', None)
            if inspect is None:
                return None  # Compatibility for process-only legacy adapters.
            details = inspect(pane, identity)
            sid = details.get('conversation_id')
            if (not isinstance(sid, str) or not UUID.fullmatch(sid)
                    or details.get('engine') != process[0][2]):
                raise DeliveryError('Conversazione non verificata. Apri il terminale per rispondere.')
            return details['engine'], sid, details.get('process_identity')
        scope = conversation()
        view = service.tmux.composer_view(pane, identity, expected_command=process[1])
        value = parse_question(view, process[0][2])
        if _snapshot(service, pane, identity) != process or conversation() != scope:
            raise DeliveryError('Il processo è cambiato. Aggiorna la domanda.')
        self.access()
        return (*process, scope), value, view

    def read(self, pane, identity):
        identity = self.identity(identity)
        service = self.service()
        with service.tmux.pane_lock(pane):
            process, question, view = self.current(service, pane, identity)
            key = (pane, identity)
            with self.lock:
                now = time.monotonic()
                self.tokens = {token: item for token, item in self.tokens.items() if item['expires'] > now}
                if not question:
                    self.blocked.pop(key, None)
                    self.tokens = {token: item for token, item in self.tokens.items() if item['key'] != key}
                    lines = view.get('output', '').splitlines()
                    waiting = (any(_OPTION.fullmatch(line) for line in lines[-16:])
                               and any(_FOOTER.fullmatch(line) for line in lines[-4:]))
                    if not waiting:
                        waiting = (any(_FORM_HEADER.fullmatch(line) for line in lines[-40:])
                                   and any(line.strip() == 'Review your answers' for line in lines[-40:]))
                    return dict(available=False, waiting=waiting, allow_answer=False,
                                reason='Apri il terminale per leggere e rispondere a questa richiesta.')
                challenge = _fingerprint(pane, identity, process, question)
                base = _fingerprint(pane, identity, process, question, selection=False)
                blocked = self.blocked.get(key) == base
                if not blocked: self.blocked.pop(key, None)
                self.tokens = {token: item for token, item in self.tokens.items()
                               if item['key'] != key or item['challenge'] == challenge}
                token, captured = next(((token, item) for token, item in self.tokens.items()
                                        if item['key'] == key and item['challenge'] == challenge), (None, None))
                allowed = self.allow_answer() and not blocked
                if allowed and token is None:
                    token = secrets.token_urlsafe(24)
                    captured = dict(key=key, process=process, question=question, challenge=challenge,
                                    base=base, expires=now + 60)
                    if len(self.tokens) >= 1000: self.tokens.pop(next(iter(self.tokens)))
                    self.tokens[token] = captured
                result = dict(available=True, waiting=True, engine=process[0][2], kind=question.get('kind', 'approval'),
                              challenge=challenge, token=token if allowed else None,
                              expires_in=max(0, math.ceil(captured['expires'] - now)) if captured and allowed else 0,
                              allow_answer=allowed, **_public(question))
                if blocked:
                    result['reason'] = 'Risposta già tentata. Attendi la nuova richiesta o verifica il terminale.'
                elif not allowed:
                    result['reason'] = 'Istanza in sola lettura: rispondi dal terminale.'
                elif question.get('_form'):
                    result['reason'] = 'Scegli una risposta. Per una risposta libera o per parlarne, apri Terminale.'
                return result

    def answer(self, pane, body):
        self.access(write=True)
        if not isinstance(body, dict): raise ValueError('Risposta non valida')
        identity = self.identity(body.get('identity'))
        choice, token, challenge = body.get('choice'), body.get('token'), body.get('challenge')
        if (type(choice) is not int or not 1 <= choice <= 9 or not isinstance(token, str) or not 1 <= len(token) <= 100
                or challenge is not None and (not isinstance(challenge, str) or len(challenge) > 100)):
            raise ValueError('Scelta non valida')
        with self.lock:
            captured = self.tokens.pop(token, None)
        if (not captured or captured['expires'] <= time.monotonic() or captured['key'] != (pane, identity)
                or challenge is not None and challenge != captured['challenge']):
            raise DeliveryError('La domanda è scaduta o è cambiata. Aggiornala prima di rispondere.')
        expected = captured['question']
        if choice not in [item['id'] for item in expected['choices']]: raise ValueError('Scelta non disponibile')
        service = self.service()
        with service.tmux.pane_lock(pane):
            process, current, _ = self.current(service, pane, identity)
            if process != captured['process'] or current != expected:
                raise DeliveryError('La domanda è cambiata. Aggiornala prima di rispondere.')
            with self.lock:
                if self.blocked.get(captured['key']) == captured['base']:
                    raise DeliveryError('La risposta è già stata tentata. Verifica il terminale.')
                self.blocked[captured['key']] = captured['base']
                if len(self.blocked) > 1000: self.blocked.pop(next(iter(self.blocked)))
                self.tokens = {token: item for token, item in self.tokens.items() if item['key'] != captured['key']}
            selected = expected['selected']
            try:
                while selected != choice:
                    self.access(write=True)
                    step = 1 if choice > selected else -1
                    service.tmux.send_key(pane, 'ArrowDown' if step > 0 else 'ArrowUp', identity, expected_command=process[1])
                    selected += step
                    deadline = time.monotonic() + 1
                    while True:
                        process, current, _ = self.current(service, pane, identity)
                        target = {**expected, 'selected': selected}
                        if process != captured['process'] or (current and {**current, 'selected': expected['selected']} != expected):
                            raise DeliveryError('La domanda è cambiata durante la scelta. Verifica il terminale.')
                        if current == target: break
                        if time.monotonic() >= deadline:
                            raise DeliveryError('Scelta non confermata. Verifica il terminale prima di riprovare.')
                        time.sleep(.025)
                process, current, _ = self.current(service, pane, identity)
                if process != captured['process'] or current != {**expected, 'selected': choice}:
                    raise DeliveryError('La domanda è cambiata. Verifica il terminale.')
                self.access(write=True)
                service.tmux.send_key(pane, 'Enter', identity, expected_command=process[1])
            except (PaneMissing, TmuxUnavailable):
                raise DeliveryError('Invio non confermato. Verifica il terminale prima di riprovare.') from None
        return dict(ok=True, delivery='delivered', message='Risposta consegnata al terminale.')


def create_blueprint(auth_ok, service, allow_answer=lambda: True):
    questions = Questions(service, auth_ok, allow_answer)
    bp = Blueprint('terminal_questions', __name__)

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def boundary(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                questions.access()
                return jsonify(function(*args, **kwargs))
            except PaneMissing: return jsonify(error='Pannello cambiato. Riapri il terminale.'), 404
            except (TmuxUnavailable, OSError): return jsonify(error='Terminale non disponibile. Verifica la connessione.'), 503
            except ValueError as error: return jsonify(error=str(error)), getattr(error, 'status_code', 400)
        return guarded

    @bp.get('/api/panes/<int:pane>/question')
    @boundary
    def question(pane): return questions.read(pane, request.args.get('identity'))

    @bp.post('/api/panes/<int:pane>/question/answer')
    @boundary
    def answer(pane):
        request.max_content_length = 2048
        return questions.answer(pane, request.get_json(silent=True))

    return bp
