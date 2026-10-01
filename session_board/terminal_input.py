"""Bounded JSON and multipart input shared by both terminal HTTP adapters."""
from dataclasses import dataclass

from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from .transcripts import UUID

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024


class InputError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TerminalInput:
    text: str
    identity: str
    images: tuple = ()
    chat_conversation: str = None

    @property
    def image(self):
        """Compatibility for readers interested in the first attachment."""
        return self.images[0] if self.images else None


def _image_sizes(images):
    total = 0
    for image in images:
        try:
            # Werkzeug's spooled upload streams are seekable. Inspect their
            # real byte lengths without trusting multipart content-length or
            # retaining a second in-memory copy of the batch.
            position = image.stream.tell()
            image.stream.seek(0, 2)
            size = image.stream.tell()
            image.stream.seek(position)
        except (AttributeError, OSError, ValueError):
            raise InputError('Impossibile leggere una delle immagini') from None
        if size > MAX_IMAGE_BYTES:
            raise InputError('Ogni immagine può occupare al massimo 10 MiB', 413)
        total += size
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise InputError('Le immagini possono occupare al massimo 20 MiB complessivi', 413)


def read_input(request):
    multipart = request.mimetype == 'multipart/form-data'
    request.max_content_length = MAX_TOTAL_IMAGE_BYTES + 64 * 1024 if multipart else 16 * 1024
    # The multipart decoder buffers chunks before streaming file content.
    # Keep that buffer bounded without applying the old 16 KiB JSON limit
    # to a complete image. Caption and identity are checked separately below.
    request.max_form_memory_size = 256 * 1024
    request.max_form_parts = MAX_IMAGES + 4
    images = ()
    try:
        if multipart:
            fields, files = request.form, request.files
            if any(len(fields.getlist(key)) != 1 for key in fields):
                raise InputError('I campi del messaggio non possono essere ripetuti')
            images = tuple(files.getlist('image'))
            if set(files) != {'image'} or not 1 <= len(images) <= MAX_IMAGES:
                raise InputError('Allega da 1 a 5 immagini per messaggio')
            _image_sizes(images)
            text, identity = fields.get('text', ''), fields.get('identity')
            conversation = fields.get('chat_conversation') if 'chat_conversation' in fields else None
        else:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise InputError('Invia un oggetto JSON valido oppure un’immagine')
            text, identity = data.get('text'), data.get('identity')
            conversation = data.get('chat_conversation') if 'chat_conversation' in data else None
            if 'chat_conversation' in data and conversation is None:
                raise InputError('Conversazione chat non valida')
    except RequestEntityTooLarge:
        raise InputError('Upload troppo grande: massimo 5 immagini, 10 MiB ciascuna e 20 MiB complessivi' if multipart
                         else 'Messaggio troppo grande', 413) from None
    except BadRequest:
        raise InputError('Impossibile leggere il messaggio o l’immagine') from None
    if not isinstance(identity, str) or not identity or len(identity) > 200:
        raise InputError('Identità del pannello richiesta')
    if conversation is not None and (not isinstance(conversation, str) or not UUID.fullmatch(conversation)):
        raise InputError('Conversazione chat non valida')
    if isinstance(text, str):
        text = text.replace('\r\n', '\n')
    if (not isinstance(text, str) or (not text.strip() and not images)
            or len(text) > 4000 or any((ord(char) < 32 and char != '\n') or ord(char) == 127 for char in text)):
        raise InputError('Inserisci un messaggio senza caratteri di controllo, massimo 4000 caratteri')
    return TerminalInput(text, identity, images, conversation)
