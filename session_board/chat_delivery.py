"""Opt-in chat receipts fence transcript history before one terminal write."""
from pathlib import Path
import stat

from . import chat
from .terminal_delivery import DeliveryError, _before_write
from .tmux_backend import PaneMissing, TmuxUnavailable
from .transcripts import UUID

_SCOPE = ('engine', 'account', 'cwd', 'conversation_id', 'process_identity',
          'transcript_path', 'codex_home', 'transcript_identity')


def _same_scope(before, current):
    return all(before.get(key) == current.get(key) for key in _SCOPE)


def _file_state(service, details):
    if details['engine'] == 'codex':
        path = Path(details['transcript_path'])
    else:
        path = (Path(service.accounts.path_for(details['account'])) / 'projects'
                / details['cwd'].replace('/', '-') / (details['conversation_id'] + '.jsonl'))
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Transcript non regolare')
    return f'{info.st_dev}:{info.st_ino}', info.st_size


def deliver(service, pane, value, write):
    """Write exactly once; post-write uncertainty must never become a retry."""
    with service.tmux.pane_lock(pane):
        with _before_write():
            if not isinstance(value.chat_conversation, str) or not UUID.fullmatch(value.chat_conversation):
                raise DeliveryError('Conversazione chat non valida', 400)
            inspect = getattr(service, 'inspect_chat', None) or service.inspect
            details = inspect(pane, value.identity)
            if (details.get('conversation_id') != value.chat_conversation
                    or details.get('engine') not in {'claude', 'codex'}
                    or not details.get('process_identity')):
                raise DeliveryError('La conversazione è cambiata. Riapri la chat.')
            data = chat._collect(service, details)
            if (data.get('available') is not True or not isinstance(data.get('cursor'), int)
                    or data['cursor'] < -1 or not data.get('transcript_identity')):
                raise DeliveryError('Conversazione non verificabile. Aggiorna la chat prima di inviare.')
            current = inspect(pane, value.identity)
            if not _same_scope(details, current):
                raise DeliveryError('La conversazione è cambiata. Riapri la chat.')
            file_identity, size = _file_state(service, current)
            if file_identity != data['transcript_identity'] or size <= data['cursor']:
                raise DeliveryError('La conversazione è cambiata. Riapri la chat.')
            content = [{'type': 'text', 'text': value.text}]
            images = getattr(value, 'images', None)
            if images is None:
                image = getattr(value, 'image', None)
                images = (image,) if image is not None else ()
            content.extend({'type': 'image'} for _ in images)
            receipt = dict(conversation_id=value.chat_conversation, engine=details['engine'],
                           identity=value.identity, transcript_identity=data['transcript_identity'],
                           # Include any bytes appended while the final process
                           # preflight ran, even if they were not in the projection.
                           before_offset=size - 1,
                           baseline=[message['id'] for message in data['messages'] if message['role'] == 'user'],
                           text=chat._text({'type': 'user', 'message': {'content': content}}),
                           image_count=len(images))
        # Preserve the existing transport's exact error classification.
        write()
        try:
            current = inspect(pane, value.identity)
            file_identity, size = _file_state(service, current)
            if (not _same_scope(details, current) or file_identity != receipt['transcript_identity']
                    or size <= receipt['before_offset']):
                raise ValueError('Changed conversation')
        except (PaneMissing, TmuxUnavailable, ValueError, OSError, KeyError):
            raise DeliveryError('Messaggio inviato al terminale, ma conversazione non più verificabile. '
                                'Controlla il terminale prima di riprovare.', 409,
                                definite_rejection=False) from None
        return receipt
