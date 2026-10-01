"""Read existing Codex conversations using process-owned root rollout files."""
import json
import os
from pathlib import Path
import re
import stat

import psutil

from .transcripts import MAX_TRANSCRIPT, UUID, TranscriptRows
from .chat_actions import codex_activity

MAX_META = 256 * 1024
_ROLLOUT = re.compile(r'rollout-.+-([a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})\.jsonl')
_IDENTITY_KEYS = ('conversation_id', 'cwd', 'transcript_identity')


class CodexConversationChanged(ValueError):
    status_code = 409


class _NotRootConversation(ValueError):
    pass


def _path(value, home):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not _ROLLOUT.fullmatch(path.name):
        raise ValueError('Percorso conversazione non verificabile')
    resolved = path.resolve(strict=True)
    resolved.relative_to(Path(home) / 'sessions')
    return resolved


def _open(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    file = os.fdopen(fd, 'rb')
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        file.close()
        raise ValueError('Transcript non regolare')
    return file


def _metadata(file, path):
    raw = file.readline(MAX_META + 1)
    if len(raw) > MAX_META:
        raise ValueError('Metadati conversazione troppo grandi')
    row = json.loads(raw)
    if not isinstance(row, dict) or row.get('type') != 'session_meta':
        raise ValueError('Metadati conversazione mancanti')
    meta = row.get('payload')
    if not isinstance(meta, dict):
        raise ValueError('Metadati conversazione non validi')
    sid, cwd = meta.get('id'), meta.get('cwd')
    filename = _ROLLOUT.fullmatch(path.name)
    if (not isinstance(sid, str) or not UUID.fullmatch(sid)
            or filename is None or filename.group(1) != sid
            or not isinstance(cwd, str) or len(cwd) > 4096 or '\0' in cwd
            or not Path(cwd).is_absolute()):
        raise ValueError('Identità conversazione non verificabile')
    # One native process can own the root plus several subagent rollouts.
    # Only the root TUI conversation belongs to the pane's composer.
    if (meta.get('source') != 'cli' or meta.get('parent_thread_id')
            or meta.get('agent_path') not in (None, '/root')):
        raise _NotRootConversation('Transcript non appartenente alla conversazione principale')
    info = os.fstat(file.fileno())
    return {'conversation_id': sid, 'cwd': cwd,
            'transcript_identity': f'{info.st_dev}:{info.st_ino}'}


def inspect_codex(service, pane, identity):
    """Resolve a pane to exactly one native Codex process and one root writer.

    No history scan, cwd guessing, latest-file selection, CLI launch, or state
    writes. Ambiguity is reported without returning a transcript path.
    """
    _, item = service._pane(pane, identity)
    result = dict(engine='codex', account=None, cwd=item.get('cwd'),
                  conversation_id=None, status=None,
                  reason='Conversazione Codex non verificata per questo pannello.')
    try:
        parent = psutil.Process(int(item['identity'].split(':')[-2]))
        processes = []
        for process in [parent] + parent.children(recursive=True):
            argv = process.cmdline()
            if (argv and Path(argv[0]).name in {'codex', 'codex.exe'}
                    and Path(process.exe()).name in {'codex', 'codex.exe'}):
                processes.append(process)
        if len(processes) != 1:
            result['reason'] = 'Il pannello non contiene un unico processo Codex riconoscibile.'
            return result
        process = processes[0]
        environment = process.environ()
        process_home = environment.get('HOME')
        configured = environment.get('CODEX_HOME')
        del environment
        home = Path(service.home).resolve()
        if process_home and Path(process_home).resolve() != home:
            result['reason'] = 'Il processo Codex appartiene a una home diversa dalla board.'
            return result
        codex_home = Path(configured) if configured else home / '.codex'
        if not codex_home.is_absolute():
            return result
        codex_home = codex_home.resolve()
        candidates = {}
        for opened in process.open_files():
            if not any(flag in getattr(opened, 'mode', '') for flag in ('w', 'a', '+')):
                continue
            try:
                path = _path(opened.path, codex_home)
            except (OSError, ValueError):
                continue
            try:
                with _open(path) as file:
                    meta = _metadata(file, path)
                candidates[str(path)] = meta
            except _NotRootConversation:
                continue
            except (OSError, ValueError, UnicodeError):
                # A new root may be opening while an old one is still held.
                # An unreadable writer cannot prove the valid one is current.
                return result
        if len(candidates) != 1:
            result['reason'] = ('Più conversazioni Codex principali aperte: associazione ambigua.'
                                if candidates else 'Codex non ha ancora aperto una conversazione principale verificabile.')
            return result
        path, meta = next(iter(candidates.items()))
        result.update(meta, transcript_path=path, codex_home=str(codex_home),
                      process_identity=f'{process.pid}:{process.create_time():.6f}', reason='')
        return result
    except (psutil.Error, OSError, ValueError):
        result['reason'] = 'Impossibile verificare il processo Codex del pannello.'
        return result


def _message(row, *, include_media=False):
    if not isinstance(row, dict) or row.get('type') != 'response_item':
        return None
    message = row.get('payload')
    if not isinstance(message, dict) or message.get('type') != 'message':
        return None
    role = message.get('role')
    if not isinstance(role, str) or role not in {'user', 'assistant'} or message.get('recipient') not in (None, 'all'):
        return None
    if message.get('phase') == 'analysis' or message.get('channel') == 'analysis':
        return None
    content = message.get('content')
    if not isinstance(content, list):
        return None
    metadata = message.get('internal_chat_message_metadata_passthrough')
    kinds = metadata.get('content_item_kinds') if isinstance(metadata, dict) else None
    parts = []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        if role == 'user' and isinstance(kinds, list) and index < len(kinds):
            kind = kinds[index]
            if not isinstance(kind, str) or (not kind.startswith('user.') and kind != 'unknown'):
                continue
        block_type = block.get('type')
        if not isinstance(block_type, str):
            continue
        if block_type in {'input_text', 'output_text', 'text'} and isinstance(block.get('text'), str):
            text = block['text']
            if role == 'user' and text.lstrip().startswith(('# AGENTS.md instructions', '<environment_context>', '<user_instructions>')):
                continue
            parts.append({'type': 'text', 'text': text})
        elif role == 'user' and block_type in {'input_image', 'image'}:
            image = {'type': 'image'}
            if include_media:
                from .chat_media import codex_source
                image['source'] = codex_source(block)
            parts.append(image)
    if not parts:
        return None
    result = {'type': role, 'message': {'content': parts}}
    if isinstance(message.get('id'), str):
        result['uuid'] = message['id']
    if isinstance(row.get('timestamp'), str):
        result['timestamp'] = row['timestamp']
    return result


def read_codex(details, *, include_actions=False, include_media=False):
    """Normalize real messages and safe activity metadata from a bounded tail.

    event_msg mirrors, reasoning, tool output, instructions and agent traffic
    never become chat bubbles. Image bytes are available only to the explicit
    private-media projection; default callers retain text-only normalization.
    """
    try:
        path = _path(details['transcript_path'], details['codex_home'])
        with _open(path) as file:
            meta = _metadata(file, path)
            if any(meta.get(key) != details.get(key) for key in _IDENTITY_KEYS):
                raise ValueError('Identità modificata')
            size = os.fstat(file.fileno()).st_size
            offset = max(0, size - MAX_TRANSCRIPT)
            file.seek(offset)
            if offset:
                file.readline(MAX_TRANSCRIPT)
            offset = file.tell()
            raw = file.read(MAX_TRANSCRIPT)
            file.seek(0)
            if _metadata(file, path) != meta:
                raise ValueError('Metadati modificati')
    except (OSError, KeyError, ValueError, UnicodeError):
        raise CodexConversationChanged('La conversazione Codex è cambiata o non è più disponibile. Riapri la chat.') from None
    rows = TranscriptRows(meta['transcript_identity'], max(size, offset + len(raw)) - 1)
    for line_number, line in enumerate(raw.splitlines(keepends=True), 1):
        position = offset
        offset += len(line)
        if not line.endswith(b'\n'):
            continue
        try:
            decoded = json.loads(line)
            row = _message(decoded, include_media=include_media)
            if row is None and include_actions:
                row = codex_activity(decoded)
        except (ValueError, UnicodeError):
            continue
        if row is not None:
            source = {'line': line_number if size <= MAX_TRANSCRIPT else None,
                      'byte_offset': position, 'role': row['type'],
                      'conversation_id': details['conversation_id']}
            rows.append((row, source))
    return rows, size > MAX_TRANSCRIPT
