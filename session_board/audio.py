"""Bounded local dictation. Audio becomes a draft, never terminal input."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import tempfile
import threading

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

MAX_SECONDS = 120
MAX_BYTES = 15 * 1024 * 1024
MAX_TEXT = 16000
# Resolve only when dictation runs: optional audio must not prevent a board
# from starting when its filesystem has no writable temporary directory.
_LOCK_PATH = None
_THREAD_SLOT = threading.Lock()
_FAILURE = 'Trascrizione locale non disponibile. Riprova o scrivi il messaggio.'
_ERRORS = {
    'invalid_audio': ('Audio non valido. Registra nuovamente il messaggio.', 400),
    'too_long': ('La registrazione supera il limite di 120 secondi.', 413),
    'no_speech': ('Non è stato rilevato parlato. Prova a registrare di nuovo.', 422),
    'text_too_long': ('Trascrizione troppo lunga. Registra un audio più breve.', 413),
}


class AudioError(ValueError):
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


def _configuration(settings):
    if settings.get('enabled') is not True:
        raise AudioError('La dettatura locale non è abilitata.')
    python, model = settings.get('python'), settings.get('model')
    language = settings.get('language', 'it')
    if (not isinstance(python, str) or not Path(python).is_absolute()
            or not Path(python).is_file() or not os.access(python, os.X_OK)
            or not isinstance(model, str) or not Path(model).is_absolute()
            or not Path(model).is_dir()
            or not all((Path(model) / name).is_file() for name in ('model.bin', 'config.json', 'tokenizer.json'))
            or not any((Path(model) / name).is_file() for name in ('vocabulary.txt', 'vocabulary.json'))
            or not isinstance(language, str) or not re.fullmatch(r'auto|[a-z]{2,3}', language)):
        raise AudioError('Configura il runtime e il modello locale per usare la dettatura.')
    return dict(python=python, model=model, language=language)


@contextmanager
def _slot():
    if not _THREAD_SLOT.acquire(blocking=False):
        raise AudioError('Una trascrizione è già in corso. Riprova tra poco.', 429)
    fd = None
    try:
        try:
            lock_path = _LOCK_PATH or (Path(tempfile.gettempdir()) / f'session-board-dictation-{os.getuid()}.lock')
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise AudioError(_FAILURE)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AudioError('Una trascrizione è già in corso. Riprova tra poco.', 429) from None
        except OSError:
            raise AudioError(_FAILURE) from None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        _THREAD_SLOT.release()


def _prepare_worker(settings):
    from .audio_runtime import worker
    worker.prepare(settings)


def _run_worker(settings, path):
    from .audio_runtime import worker, WorkerFailure
    try:
        payload = worker.request(settings, path)
    except WorkerFailure as error:
        if error.code == 'timeout':
            raise AudioError('La trascrizione ha superato il tempo disponibile. Registra un audio più breve.', 504) from None
        raise AudioError(_FAILURE) from None
    if not isinstance(payload, dict):
        raise AudioError(_FAILURE)
    if 'error' in payload:
        message, status = _ERRORS.get(payload.get('error'), (_FAILURE, 503))
        raise AudioError(message, status)
    text = payload.get('text')
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT
            or any(ord(char) < 32 and char != '\n' or ord(char) == 127 for char in text)):
        raise AudioError(_FAILURE)
    return text.strip()


def create_blueprint(auth_ok, allow_transcribe, settings):
    bp = Blueprint('local_audio', __name__)

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def access():
        if not auth_ok():
            raise AudioError('Accesso richiesto', 401)
        if not allow_transcribe():
            raise AudioError('La dettatura non è disponibile in sola lettura.', 403)

    @bp.get('/api/audio/status')
    def status():
        if not auth_ok():
            return jsonify(error='Accesso richiesto'), 401
        try:
            access()
            _configuration(settings)
            result = dict(available=True, reason='')
        except AudioError as error:
            result = dict(available=False, reason=str(error))
        except (OSError, ValueError):
            result = dict(available=False, reason=_FAILURE)
        return jsonify(**result, max_seconds=MAX_SECONDS, max_bytes=MAX_BYTES)

    @bp.post('/api/audio/transcribe')
    def transcribe():
        try:
            access()
            config = _configuration(settings)
            with _slot():
                # Both permission and the shared slot precede multipart parsing.
                request.max_content_length = MAX_BYTES + 64 * 1024
                request.max_form_memory_size = 256 * 1024
                request.max_form_parts = 1
                if request.mimetype != 'multipart/form-data':
                    raise AudioError('Allega una registrazione audio.', 400)
                files = request.files
                if request.form or set(files) != {'audio'} or len(files.getlist('audio')) != 1:
                    raise AudioError('Allega una sola registrazione audio.', 400)
                with tempfile.TemporaryDirectory(prefix='session-board-audio-') as directory:
                    path = Path(directory) / 'recording.bin'
                    size = 0
                    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
                        while True:
                            chunk = files['audio'].stream.read(min(65536, MAX_BYTES + 1 - size))
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise AudioError('La registrazione supera il limite di 15 MiB.', 413)
                            output.write(chunk)
                    if not size:
                        raise AudioError('La registrazione è vuota. Prova di nuovo.', 400)
                    text = _run_worker(config, path)
                    access()
                    return jsonify(text=text)
        except AudioError as error:
            return jsonify(error=str(error)), error.status_code
        except RequestEntityTooLarge:
            return jsonify(error='Allega un solo audio, massimo 15 MiB.'), 413
        except BadRequest:
            return jsonify(error='Impossibile leggere la registrazione audio.'), 400
        except Exception:
            # Decoder/model exceptions may contain file paths or input text.
            return jsonify(error=_FAILURE), 503

    @bp.post('/api/audio/prepare')
    def prepare():
        try:
            access()
            _prepare_worker(_configuration(settings))
            return jsonify(ok=True), 202
        except AudioError as error:
            return jsonify(error=str(error)), error.status_code
        except Exception:
            return jsonify(error=_FAILURE), 503

    return bp
