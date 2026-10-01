"""Opaque references to embedded images in an already verified transcript.

This module never opens image paths, follows URLs or creates an image cache.
Only the caller's bounded transcript records can supply image bytes.
"""
import base64
import binascii
from collections import OrderedDict
import hashlib
import io
import json
import re
import threading

from .terminal_images import ImageError, RasterDecoder
from .transcripts import MAX_TRANSCRIPT

MAX_IMAGES = 20
MAX_ROW_IMAGES = 5
MEDIA_ID = re.compile(r'[a-f0-9]{64}')
MIMES = frozenset(('image/png', 'image/jpeg', 'image/webp', 'image/gif'))
UNSUPPORTED = 'Immagine non disponibile: sono supportate solo immagini incorporate PNG, JPEG, WebP e GIF.'
LIMIT = 'Alcune immagini superano il limite di visualizzazione. Sono mostrate fino a 20 immagini recenti e 5 per messaggio.'
_CALL_ID = re.compile(r'[A-Za-z0-9_.:-]{1,200}')
_DECODE_LOCK = threading.Lock()


def embedded(source):
    return (isinstance(source, dict) and source.get('type') == 'base64'
            and isinstance(source.get('media_type'), str) and source['media_type'] in MIMES
            and isinstance(source.get('data'), str) and 0 < len(source['data']) <= MAX_TRANSCRIPT
            and source['data'].isascii())


def codex_source(block):
    """Only Codex's established user input_image data URI shape is supported."""
    value = block.get('image_url')
    if not isinstance(value, str):
        return None
    header, separator, data = value.partition(',')
    if not separator:
        return None
    for mime in MIMES:
        if header == f'data:{mime};base64':
            return dict(type='base64', media_type=mime, data=data)
    return None


class Catalog:
    def __init__(self, details):
        self.scope = {key: details.get(key) for key in (
            'engine', 'account', 'cwd', 'conversation_id', 'process_identity',
            'transcript_path', 'codex_home')}
        self.records = OrderedDict()
        self.limited = False
        self.transcript_identity = None

    def bind(self, identity):
        self.transcript_identity = identity

    def _image(self, row, location, source, block, alt):
        value = block.get('source')
        if not embedded(value) or not self.transcript_identity:
            return None
        digest = hashlib.sha256(value['data'].encode()).hexdigest()
        identity = [self.scope, self.transcript_identity, source['byte_offset'], row.get('uuid'),
                    row.get('type'), location, value['media_type'], digest]
        identifier = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.records[identifier] = value
        if len(self.records) > MAX_IMAGES:
            self.records.popitem(last=False)
            self.limited = True
        return dict(id=identifier, alt=alt)

    def extract(self, row, source):
        """Return direct descriptors and tool media without copying tool text."""
        if row.get('type') not in ('user', 'assistant') or any(row.get(flag) for flag in (
                'isMeta', 'isSidechain', 'isCompactSummary')):
            return [], None, []
        message = row.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            return [], None, []
        direct, tools, notice = [], [], None
        count = 0
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            groups = []
            if block.get('type') == 'image':
                groups = [(block, [index], direct, 'Immagine allegata' if row['type'] == 'user' else 'Immagine di risposta')]
            elif row['type'] == 'user' and block.get('type') == 'tool_result':
                call_id = block.get('tool_use_id')
                nested = block.get('content')
                if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id) or not isinstance(nested, list):
                    continue
                images = []
                entry = dict(id=hashlib.sha256(json.dumps([self.scope, self.transcript_identity,
                             source['byte_offset'], index, 'tool-media'], sort_keys=True).encode()).hexdigest(),
                             role='media', label='Immagine dello strumento', images=images)
                groups = [(item, [index, child], images, 'Immagine dello strumento')
                          for child, item in enumerate(nested) if isinstance(item, dict) and item.get('type') == 'image']
                if groups:
                    tools.append(entry)
            for image, location, destination, alt in groups:
                count += 1
                descriptor = self._image(row, location, source, image, alt) if count <= MAX_ROW_IMAGES else None
                if descriptor:
                    destination.append(descriptor)
                elif count > MAX_ROW_IMAGES:
                    self.limited = True
                    if destination is direct:
                        notice = LIMIT
                    else:
                        entry['media_notice'] = LIMIT
                elif destination is direct:
                    notice = UNSUPPORTED
                else:
                    entry['media_notice'] = UNSUPPORTED
        return direct, notice, tools


def sanitize(source):
    if not embedded(source):
        raise ImageError('Immagine non disponibile o non valida.', 422)
    try:
        data = base64.b64decode(source['data'], validate=True)
    except (ValueError, binascii.Error):
        raise ImageError('Immagine non disponibile o non valida.', 422) from None
    # Bound concurrent in-process raster allocations; uploads keep their own
    # directory lock. No decoded pixels or image bytes survive this request.
    if not _DECODE_LOCK.acquire(timeout=10):
        raise ImageError('Un’immagine è in elaborazione. Riprova.', 503)
    try:
        return RasterDecoder()._decode(io.BytesIO(data))
    except ImageError as error:
        raise ImageError('Immagine non disponibile o non valida.', 413 if error.status_code == 413 else 422) from None
    finally:
        _DECODE_LOCK.release()
