"""Attach a private image to a verified CLI composer before submitting text.

Image bytes never travel through terminal input. The CLI receives a quoted
local path as a bracketed paste and must acknowledge it with a new image chip
in its current composer. A timeout retains the potentially attached file.
"""
from collections import Counter
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import time

import psutil

from .session_switch import _invocation
from .terminal_images import ImageStore
from .tmux_backend import PaneMissing, TmuxUnavailable, _STYLE_ESCAPE

ATTACH_TIMEOUT = 20
SUBMIT_TIMEOUT = 3
_PASTE_SETTLE = .15
_POLL_INTERVAL = .05
_PROMPT = re.compile(r'^\s*[›❯]')
_IMAGE = re.compile(r'\[Image\s*#(\d+)\]')


class DeliveryError(ValueError):
    def __init__(self, message, status_code=409, *, definite_rejection=False):
        super().__init__(message)
        self.status_code = status_code
        self.definite_rejection = definite_rejection


@contextmanager
def _before_write():
    """Classify only work that cannot yet have changed terminal input."""
    try:
        yield
    except PaneMissing:
        raise DeliveryError('Pannello non più disponibile. Riapri il terminale.', 404,
                            definite_rejection=True) from None
    except (TmuxUnavailable, OSError):
        raise DeliveryError('Verifica del terminale non disponibile. Nessun messaggio inviato.', 503,
                            definite_rejection=True) from None
    except ValueError as error:
        raise DeliveryError(str(error), getattr(error, 'status_code', 400),
                            definite_rejection=True) from None


def _snapshot(service, pane, identity):
    """Resolve fresh process objects so PID reuse cannot reuse cached times."""
    _, item = service._pane(pane, identity)
    try:
        parent = psutil.Process(int(identity.split(':')[-2]))
        matches = []
        wrappers = {}
        for process in [parent] + parent.children(recursive=True):
            try:
                argv = process.cmdline()
                claude = _invocation(process, service.accounts.claude_bin)
                codex = next((argv[index + 1:] for index in (0, 1)
                              if index < len(argv) and Path(argv[index]).name == 'codex'), None)
                engine = 'claude' if claude is not None else 'codex' if codex is not None else None
                if engine is None:
                    continue
                if engine == 'codex' and Path(argv[0]).name in {'node', 'nodejs'}:
                    wrappers[process.pid] = {child.pid for child in process.children(recursive=True)}
                args = claude if engine == 'claude' else codex
                # Auxiliary app servers are not terminal composers. Other CLI
                # commands still count as ambiguity when sharing an active pane.
                interactive = not (args and args[0] in {
                    'auth', 'login', 'logout', 'app-server', 'mcp-server', 'exec', 'review',
                }) and not (engine == 'claude' and any(arg in {'-p', '--print'} for arg in args))
                matches.append((process.pid, process.create_time(), engine,
                                tuple(argv), process.cwd(), interactive, process.uids().effective))
            except psutil.NoSuchProcess:
                # A finished helper (including zombies) cannot own the input.
                # Losing the pane process itself must still fail closed.
                if process.pid == parent.pid:
                    raise
        matches = [record for record in matches if not (
            record[0] in wrappers and any(other[0] in wrappers[record[0]]
                                         and other[0] not in wrappers and other[2] == 'codex'
                                         for other in matches))]
        if len(matches) != 1 or not matches[0][-2]:
            raise DeliveryError('L’invio richiede un solo Claude o Codex interattivo nel pannello')
        if matches[0][-1] != os.geteuid():
            raise DeliveryError('Il CLI deve appartenere allo stesso utente della board per leggere l’immagine privata')
        return matches[0], item['command']
    except (psutil.Error, OSError, ValueError) as error:
        if isinstance(error, DeliveryError):
            raise
        raise DeliveryError('Impossibile verificare il processo Claude o Codex del pannello') from None


def _composer(view, engine=None):
    """Bind the input field to the caret, not to a historical prompt marker.

    Claude draws its own caret with the terminal cursor hidden. Its current
    input is bounded by two horizontal rules; transcript prompts are not.
    """
    lines = view['output'].splitlines()
    cursor = view['cursor_y']
    if not 0 <= cursor < len(lines):
        return None
    if engine == 'claude':
        border = lambda line: re.fullmatch(r'\s*[─━═]{10,}\s*', line)
        # Claude can place a short status label at the right of the upper rule.
        # Keep the lower rule and caret checks: transcript/menu text is not input.
        top_border = lambda line: re.fullmatch(r'\s*[─━═]{10,}(?: [^─━═\r\n]{1,40} [─━═]+)?\s*', line)
        top = next((row for row in range(cursor - 1, -1, -1) if top_border(lines[row])), None)
        bottom = next((row for row in range(cursor + 1, len(lines)) if border(lines[row])), None)
        if top is None or bottom is None:
            return None
        content = lines[top + 1:bottom]
        first = next((row for row in range(top + 1, bottom) if lines[row].strip()), None)
        if first is None or first > cursor or not re.match(r'^\s*❯', lines[first]):
            return None
        return '\n'.join(content)
    if not view['cursor_visible']:
        return None
    for start in range(cursor, -1, -1):
        line = lines[start]
        if _PROMPT.match(line):
            return '\n'.join(lines[start:cursor + 1])
        if not line.strip() or re.fullmatch(r'[\s─━═]+', line):
            break
    return None


def _chips(view, engine=None):
    return Counter(_IMAGE.findall(_composer(view, engine) or ''))


def _draft(value):
    return re.sub(r'\s+', '', _PROMPT.sub('', value, count=1))


def _dim_state(parameters, dim):
    """Interpret only known SGR attributes; colors consume their own operands."""
    fields = parameters.split(';')
    index = 0
    ordinary = {1, 3, 4, 5, 7, 8, 9, 23, 24, 25, 27, 28, 29, 39, 49, 53, 55, 59}
    ordinary.update(range(30, 38)); ordinary.update(range(40, 48))
    ordinary.update(range(90, 98)); ordinary.update(range(100, 108))
    while index < len(fields):
        field = fields[index]
        if ':' in field:
            parts = field.split(':')
            if parts[0] in {'38', '48', '58'}:
                if len(parts) == 3 and parts[1] == '5':
                    colors = parts[2:]
                elif len(parts) == 5 and parts[1] == '2':
                    colors = parts[2:]
                elif len(parts) == 6 and parts[1] == '2' and parts[2] in {'', '0'}:
                    colors = parts[3:]
                else:
                    return None
                if not all(part.isascii() and part.isdigit() and 0 <= int(part) <= 255 for part in colors):
                    return None
            elif not (len(parts) == 2 and parts[0] == '4' and parts[1] in {'0','1','2','3','4','5'}):
                return None
            index += 1
            continue
        code = int(field or '0')
        if code in {38, 48, 58}:
            mode = fields[index+1] if index+1 < len(fields) else None
            count = 1 if mode == '5' else 3 if mode == '2' else 0
            colors = fields[index+2:index+2+count]
            if not count or len(colors) != count or not all(
                    part.isascii() and part.isdigit() and 0 <= int(part) <= 255 for part in colors):
                return None
            index += count + 2
            continue
        if code in {0, 22}:
            dim = False
        elif code == 2:
            dim = True
        elif code not in ordinary:
            return None
        index += 1
    return dim


def _dim_placeholder(view, value):
    styled = view.get('styled_output')
    plain = view['output']
    if (not isinstance(styled, str) or len(styled) > 256000 or len(plain) > 64000
            or '[Pasted text' in value):
        return False
    parts, flags, position, dim = [], [], 0, False
    for match in _STYLE_ESCAPE.finditer(styled):
        segment = styled[position:match.start()]
        if any(char in segment for char in ('\x1b', '\x9b', '\x9d')):
            return False
        parts.append(segment); flags.extend([dim] * len(segment))
        # Complete OSC 8 hyperlinks carry no display characters or SGR state.
        # The same tokenizer removes them when creating view['output'].
        if match.group('sgr') is not None:
            try:
                dim = _dim_state(match.group('sgr'), dim)
            except ValueError:
                return False
            if dim is None:
                return False
        position = match.end()
    segment = styled[position:]
    if any(char in segment for char in ('\x1b', '\x9b', '\x9d')):
        return False
    parts.append(segment); flags.extend([dim] * len(segment))
    if ''.join(parts) != plain:
        return False
    content = value.splitlines()
    first = next((index for index, line in enumerate(content) if line.strip()), None)
    if first is None:
        return False
    content = content[first:]
    marker = _PROMPT.match(content[0])
    if marker is None or view.get('cursor_x') != marker.end() + 1:
        return False
    lines = plain.splitlines(keepends=True)
    cursor = view['cursor_y']
    if [line.rstrip('\r\n') for line in lines[cursor:cursor+len(content)]] != content:
        return False
    offset = sum(len(line) for line in lines[:cursor])
    seen = False
    for row, line in enumerate(content):
        for column, char in enumerate(line):
            if (row == 0 and column < marker.end()) or char.isspace():
                continue
            seen = True
            if not flags[offset+column]:
                return False
        offset += len(lines[cursor+row])
    return seen


def _empty_composer(view, engine):
    value = _composer(view, engine)
    if value is None or _IMAGE.search(value):
        return False
    if not _draft(value):
        return True
    # The native Claude placeholder is dim only while its actual value is
    # empty. A supplied style frame is authoritative, including for old hint
    # phrases typed as real text with Home. Never fall back after a mismatch.
    if engine == 'claude' and 'styled_output' in view:
        return _dim_placeholder(view, value)
    # Empty inputs may show CLI hints. A literal draft with those words has
    # its caret at the end; require the caret on the hint's first column.
    hints = ({'Ask Codex to do anything', 'Ask a follow-up question'} if engine == 'codex' else {
        'Press up to edit queued messages',
        'Press up to edit queued messages, Enter to send them immediately',
    } if engine == 'claude' else set())
    line = value.splitlines()[0]
    marker = _PROMPT.match(line)
    return (_PROMPT.sub('', value, count=1).strip() in hints
            and marker is not None and view.get('cursor_x') == marker.end() + 1
            and len(value.splitlines()) == 1
            and view['output'].splitlines()[view['cursor_y']] == line)


def _submit(service, pane, identity, text, original):
    """Wait for paste rendering, send Return once and require an empty input.

    Return can precede the CLI's asynchronous paste handling. Never repeat
    Return: an uncertain submission stays uncertain.
    """
    backend = service.tmux
    engine = original[0][2]

    def read():
        if _snapshot(service, pane, identity) != original:
            raise DeliveryError('Il processo del pannello è cambiato')
        view = backend.composer_view(pane, identity, expected_command=original[1])
        return _composer(view, engine), view

    before, initial = read()
    if before is None:
        raise DeliveryError('Il campo messaggio non è più attivo')
    expected = ('' if _empty_composer(initial, engine) else _draft(before)) + re.sub(r'\s+', '', text)
    pasted = re.compile(r'\[Pasted text #\d+(?: \+\d+ lines)?\]')
    previous_markers = Counter(pasted.findall(before))
    if text:
        backend.paste(pane, text, identity, expected_command=original[1])
    deadline = time.monotonic() + SUBMIT_TIMEOUT
    stable_since, previous = time.monotonic(), None
    while True:
        current, view = read()
        # Codex may put the caret on an empty trailing line after a multiline
        # paste. Cross empty lines only for this exact, already-sent draft.
        if current is None and engine == 'codex' and view['cursor_visible']:
            lines = view['output'].splitlines()
            cursor = view['cursor_y']
            for row in range(min(cursor, len(lines) - 1), -1, -1):
                if _PROMPT.match(lines[row]):
                    candidate = '\n'.join(lines[row:cursor + 1])
                    if _draft(candidate) == expected:
                        current = candidate
                    break
        now = time.monotonic()
        matches = current is not None and (_draft(current) == expected or (
            engine == 'claude' and Counter(pasted.findall(current)) - previous_markers))
        if current != previous or not matches:
            previous, stable_since = current, now
        if matches and now - stable_since >= _PASTE_SETTLE:
            break
        if now >= deadline:
            raise DeliveryError('Incolla non confermato: Invio non premuto. Verifica il terminale prima di riprovare.')
        time.sleep(_POLL_INTERVAL)
    backend.send_key(pane, 'Enter', identity, expected_command=original[1])
    deadline = time.monotonic() + SUBMIT_TIMEOUT
    while True:
        _, view = read()
        if _empty_composer(view, engine):
            return
        if time.monotonic() >= deadline:
            raise DeliveryError('Invio non confermato: verifica il terminale prima di riprovare.')
        time.sleep(_POLL_INTERVAL)


def send_multiline(service, pane, identity, text):
    """Paste into a verified Claude/Codex input, never execute separate lines.

    tmux 3.4 cannot expose bracket_paste_flag. Use the same process and
    active-composer checks as native image delivery on this version too.
    """
    if (not isinstance(text, str) or not text.strip() or len(text) > 4000
            or any((ord(char) < 32 and char != '\n') or ord(char) == 127 for char in text)):
        raise DeliveryError('Messaggio non valido: massimo 4000 caratteri', 400, definite_rejection=True)
    backend = service.tmux
    with backend.pane_lock(pane):
        with _before_write():
            original = _snapshot(service, pane, identity)
            view = backend.composer_view(pane, identity, expected_command=original[1])
            if _composer(view, original[0][2]) is None:
                raise DeliveryError('Per inviare più righe apri il campo messaggio di Claude o Codex')
            if _snapshot(service, pane, identity) != original:
                raise DeliveryError('Il processo del pannello è cambiato: riapri il terminale')
        _submit(service, pane, identity, text, original)


def send_image(service, pane, identity, text, upload):
    return send_images(service, pane, identity, text, (upload,))


def send_images(service, pane, identity, text, uploads):
    from .terminal_input import MAX_IMAGES
    if not isinstance(uploads, (list, tuple)) or not 1 <= len(uploads) <= MAX_IMAGES:
        raise DeliveryError('Allega da 1 a 5 immagini per messaggio', 400, definite_rejection=True)
    if (not isinstance(text, str) or len(text) > 4000
            or any((ord(char) < 32 and char != '\n') or ord(char) == 127 for char in text)):
        raise DeliveryError('Inserisci un messaggio senza caratteri di controllo, massimo 4000 caratteri', 400, definite_rejection=True)
    backend = service.tmux
    with backend.pane_lock(pane):
        paths, attempted = [], set()
        store = ImageStore(service.state_dir)
        try:
            with _before_write():
                original = _snapshot(service, pane, identity)
                engine = original[0][2]
                view = backend.composer_view(pane, identity, expected_command=original[1])
                if _composer(view, engine) is None:
                    raise DeliveryError('Il campo messaggio di Claude o Codex non è attivo: chiudi eventuali finestre nel terminale e riprova')
                before = _chips(view, engine)
                # Validate the whole batch before the first terminal write.
                for upload in uploads:
                    paths.append(store.save(upload))
                if any(ord(char) < 32 or ord(char) == 127 for path in paths for char in str(path)):
                    raise DeliveryError('La directory immagini contiene caratteri non supportati', 503)
                if _snapshot(service, pane, identity) != original:
                    raise DeliveryError('Il processo del pannello è cambiato: riapri il terminale')
            deadline = time.monotonic() + ATTACH_TIMEOUT
            for path in paths:
                if _snapshot(service, pane, identity) != original:
                    raise DeliveryError('Il processo del pannello è cambiato')
                attempted.add(path)  # A tmux timeout can follow an accepted write.
                # Claude accepts a quote pair, not concatenated shlex fragments.
                quoted = (json.dumps(str(path), ensure_ascii=False) if engine == 'claude'
                          else shlex.quote(str(path)))
                backend.paste(pane, quoted, identity, expected_command=original[1])
                while True:
                    if _snapshot(service, pane, identity) != original:
                        raise DeliveryError('Il processo del pannello è cambiato')
                    current = _chips(backend.composer_view(pane, identity, expected_command=original[1]), engine)
                    if before - current:
                        raise DeliveryError('Gli allegati nel campo messaggio sono cambiati')
                    if current - before:
                        before = current
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DeliveryError('Il CLI non ha confermato tutti gli allegati')
                    time.sleep(min(_POLL_INTERVAL, remaining))
            _submit(service, pane, identity, text, original)
        except Exception:
            # Retain only files that might have reached the native composer.
            for path in paths:
                if path not in attempted:
                    store.discard(path)
            if attempted:
                raise DeliveryError('Invio non confermato: verifica il terminale prima di riprovare. Gli allegati possibili sono stati conservati.') from None
            raise
