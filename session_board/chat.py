"""Read-only chat projection of process-verified Claude and Codex conversations."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
import re

from flask import Blueprint, Response, jsonify, request

from .chat_actions import Timeline
from . import chat_media
from .tmux_backend import PaneMissing, TmuxUnavailable
from .transcripts import MAX_TRANSCRIPT, UUID, _events, _redact

MAX_MESSAGES = 100
MAX_TEXT = 24000
# CLI-generated context is not a user turn. Strip these wrappers while keeping
# any real question before/after them; never include tool or thinking blocks.
_WRAPPERS = re.compile(
    r'<(system-reminder|local-command-caveat|local-command-stdout|command-name|'
    r'command-message|command-args|task-notification|ide_opened_file|ide_selection|'
    r'bash-input|bash-stdout|bash-stderr)\b[^>]*>.*?(?:</\1\s*>|\Z)', re.S | re.I)
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_PASTED_CONTENT = re.compile(r'^\s*<pasted_content id="([a-zA-Z0-9_-]{1,32})">\n(.*?)\n</pasted_content id="\1">\s*$', re.S)
_PROCESS_IDENTITY = re.compile(r'[1-9][0-9]*:[1-9][0-9]*(?:\.[0-9]+)?')
_BINDING_KEYS = ('engine', 'account', 'cwd', 'conversation_id', 'process_identity',
                 'transcript_path', 'codex_home', 'transcript_identity')


def _unwrapped_paste(value):
    pasted = _PASTED_CONTENT.fullmatch(value)
    return pasted[2] if pasted else value


def _queue_key(timestamp, content):
    """Match original queue provenance, never redacted/display-normalized text."""
    if (not isinstance(timestamp, str) or not 1 <= len(timestamp) <= 64
            or not isinstance(content, (str, list))):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        canonical = json.dumps(content, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError):
        return None
    return timestamp, hashlib.sha256(canonical.encode()).hexdigest()


def _queued_user(row):
    """Claude persists consumed mid-turn human prompts as queued attachments."""
    attachment = row.get('attachment')
    if row.get('type') != 'attachment' or not isinstance(attachment, dict):
        return row
    origin = attachment.get('origin')
    if (attachment.get('type') != 'queued_command' or attachment.get('commandMode') != 'prompt'
            or attachment.get('humanTurn') is not True or not isinstance(origin, dict)
            or origin.get('kind') != 'human' or not isinstance(attachment.get('prompt'), (str, list))):
        return row
    # Keep the persisted row's UUID, timestamp, offset and metadata flags. The
    # rendered wrapper contains CLI context and is never conversation content.
    return dict(row, type='user', message={'role': 'user', 'content': attachment['prompt']})


def _text(row):
    if row.get('type') not in {'user', 'assistant'}:
        return ''
    if any(row.get(flag) for flag in ('isMeta', 'isCompactSummary', 'isSidechain')):
        return ''
    message = row.get('message')
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text' and isinstance(block.get('text'), str):
                parts.append(_unwrapped_paste(block['text']) if row['type'] == 'user' else block['text'])
            elif block.get('type') == 'image' and row['type'] == 'user':
                parts.append('[Immagine allegata]')
        content = '\n\n'.join(parts)
    if not isinstance(content, str):
        return ''
    if row['type'] == 'user':
        content = _WRAPPERS.sub('', content)
        # Claude wraps multiline paste with a generated tag. It is the same
        # human prompt as the composer text, not a second or different turn.
        content = _unwrapped_paste(content)
    return _redact(_CONTROL.sub('', content)).strip()


def _collect(service, details, *, media=None):
    engine, sid, cwd = (details.get(key) for key in ('engine', 'conversation_id', 'cwd'))
    result = dict(available=False, engine=engine, conversation_id=sid,
                  messages=[], timeline=[], truncated=False, status=details.get('status'))
    if engine not in {'claude', 'codex'}:
        result['reason'] = 'La chat richiede una sessione Claude o Codex riconoscibile in questo pannello.'
        return result
    if (not isinstance(sid, str) or not UUID.fullmatch(sid)
            or not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or (engine == 'claude' and not details.get('account'))
            or (engine == 'codex' and not all(details.get(key) for key in (
                'transcript_path', 'codex_home', 'process_identity', 'transcript_identity')))):
        result['reason'] = details.get('reason') if engine == 'codex' else None
        result['reason'] = result['reason'] or (
            'Conversazione Codex non verificata per questo pannello. Apri il terminale.' if engine == 'codex'
            else 'Conversazione non verificata per questo pannello. Apri il terminale.')
        return result
    try:
        if engine == 'codex':
            from .codex_chat import read_codex
            rows, truncated = read_codex(details, include_actions=True, include_media=True)
        else:
            transcript = (Path(service.accounts.path_for(details['account'])) / 'projects'
                          / cwd.replace('/', '-') / (sid + '.jsonl'))
            # Image-bearing user turns often exceed the results reader's small-row
            # default. Parse within the same total tail budget, then discard bytes.
            process_identity = details.get('process_identity')
            follow_cwd_transitions = (isinstance(process_identity, str) and len(process_identity) <= 80
                                      and _PROCESS_IDENTITY.fullmatch(process_identity) is not None)
            rows, truncated = _events(transcript, sid, cwd, MAX_TRANSCRIPT, follow_cwd_transitions)
    except OSError:
        result['reason'] = 'La conversazione non è ancora disponibile. Riprova o apri il terminale.'
        return result
    if isinstance(getattr(rows, 'before_offset', None), int):
        result.update(cursor=rows.before_offset, transcript_identity=rows.transcript_identity)
    media = media if media is not None else chat_media.Catalog(details)
    media.bind(getattr(rows, 'transcript_identity', None))
    messages = {}
    media_messages = {}
    enqueued = {}
    timeline = Timeline(sid, engine)
    for row, source in rows:
        queued = False
        delivery_offset = None
        if engine == 'claude':
            if (row.get('type') == 'queue-operation' and row.get('operation') == 'enqueue'
                    and not any(row.get(flag) for flag in ('isMeta', 'isCompactSummary', 'isSidechain'))):
                key = _queue_key(row.get('timestamp'), row.get('content'))
                if key is not None:
                    # More than one earlier enqueue is ambiguous, even when
                    # display redaction would make their content identical.
                    enqueued[key] = None if key in enqueued else source['byte_offset']
            projected = _queued_user(row)
            queued = projected is not row
            if queued:
                attachment = row['attachment']
                key = _queue_key(attachment.get('timestamp'), attachment.get('prompt'))
                delivery_offset = enqueued.get(key)
            row = projected
        text = _text(row)
        images, media_notice, tool_images = media.extract(row, source)
        if not text and not images and not media_notice:
            timeline.add(row)
            for entry in tool_images:
                media_messages[entry['id']] = entry
                timeline.message(entry)
            continue
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + '…'
            truncated = True
        identifier = row.get('uuid')
        if not isinstance(identifier, str) or not identifier or len(identifier) > 200:
            identifier = str(source['byte_offset'])
        identifier = hashlib.sha256(f'{sid}:{row["type"]}:{identifier}'.encode()).hexdigest()[:24]
        message = dict(id=identifier, role=row['type'], text=text,
                       source_offset=source['byte_offset'])
        if images:
            message['images'] = images
        if media_notice:
            message['media_notice'] = media_notice
        if queued:
            # The consumed attachment remains in transcript order. Only its
            # verified original enqueue can satisfy a terminal send receipt.
            message['delivery_offset'] = delivery_offset
        timestamp = row.get('timestamp')
        if isinstance(timestamp, str) and len(timestamp) <= 64:
            message['timestamp'] = timestamp
        messages[identifier] = message
        timeline.add(row, message)
        for entry in tool_images:
            media_messages[entry['id']] = entry
            timeline.message(entry)
    retained = dict(list(messages.items())[-MAX_MESSAGES:])
    retained_media = dict(list(media_messages.items())[-chat_media.MAX_IMAGES:])
    for entry in [*retained.values(), *retained_media.values()]:
        if 'images' in entry:
            visible = [item for item in entry['images'] if item['id'] in media.records]
            if len(visible) != len(entry['images']):
                entry['media_notice'] = chat_media.LIMIT
            entry['images'] = visible
    items, actions_truncated = timeline.render({**retained, **retained_media})
    result.update(available=True, messages=list(retained.values()), timeline=items,
                  truncated=truncated or len(messages) > MAX_MESSAGES or actions_truncated)
    if media.limited or len(media_messages) > len(retained_media):
        result['media_notice'] = chat_media.LIMIT
    return result


def create_blueprint(service_factory, auth_ok):
    bp = Blueprint('session_chat', __name__)

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @bp.get('/api/panes/<int:pane>/chat/images/<media_id>')
    def image(pane, media_id):
        if not auth_ok():
            return jsonify(error='Accesso richiesto'), 401
        identity, sid = request.args.get('identity', ''), request.args.get('conversation_id', '')
        if not identity or len(identity) > 200 or not UUID.fullmatch(sid):
            return jsonify(error='Identità del pannello e conversazione richieste'), 400
        if not chat_media.MEDIA_ID.fullmatch(media_id):
            return jsonify(error='Immagine non disponibile'), 404
        try:
            service = service_factory()
            inspect = getattr(service, 'inspect_chat', service.inspect)
            details = inspect(pane, identity)
            if details.get('conversation_id') != sid:
                return jsonify(error='La conversazione è cambiata. Riapri la chat.'), 409
            catalog = chat_media.Catalog(details)
            data = _collect(service, details, media=catalog)
            visible = {item['id'] for entry in data['timeline'] for item in entry.get('images', [])}
            if not data['available'] or media_id not in visible or media_id not in catalog.records:
                return jsonify(error='Immagine non disponibile nei messaggi recenti'), 404
            pixels = chat_media.sanitize(catalog.records[media_id])
            if not auth_ok():
                return jsonify(error='Accesso richiesto'), 401
            # Re-read only the same server-derived bounded transcript. The ID
            # includes its inode, original row offset and embedded-byte digest.
            verified = chat_media.Catalog(details)
            _collect(service, details, media=verified)
            current = inspect(pane, identity)
            if any(current.get(key) != details.get(key) for key in _BINDING_KEYS) or media_id not in verified.records:
                return jsonify(error='La conversazione o l’immagine è cambiata. Riapri la chat.'), 409
            if not auth_ok():
                return jsonify(error='Accesso richiesto'), 401
            response = Response(pixels, mimetype='image/png')
            response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
            response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
            return response
        except PaneMissing:
            return jsonify(error='Pannello non più disponibile. Riapri la sessione.'), 404
        except TmuxUnavailable:
            return jsonify(error='tmux non disponibile'), 503
        except ValueError as error:
            return jsonify(error='Immagine non disponibile o non valida.'), getattr(error, 'status_code', 422)
        except OSError:
            return jsonify(error='Immagine non disponibile'), 503

    @bp.get('/api/panes/<int:pane>/chat')
    def chat(pane):
        if not auth_ok():
            return jsonify(error='Accesso richiesto'), 401
        identity = request.args.get('identity', '')
        if not identity or len(identity) > 200:
            return jsonify(error='Identità del pannello richiesta'), 400
        try:
            service = service_factory()
            details = getattr(service, 'inspect_chat', service.inspect)(pane, identity)
            data = _collect(service, details)
            if not auth_ok():
                return jsonify(error='Accesso richiesto'), 401
            current = getattr(service, 'inspect_chat', service.inspect)(pane, identity)
            if any(current.get(key) != details.get(key) for key in _BINDING_KEYS):
                return jsonify(error='La conversazione è cambiata. Riapri la chat.'), 409
            data['status'] = current.get('status')
            return jsonify(data)
        except PaneMissing:
            return jsonify(error='Pannello non più disponibile. Riapri la sessione.'), 404
        except TmuxUnavailable:
            return jsonify(error='tmux non disponibile'), 503
        except ValueError as error:
            return jsonify(error=str(error)), getattr(error, 'status_code', 400)
        except OSError:
            return jsonify(error='Chat non disponibile'), 503

    return bp
