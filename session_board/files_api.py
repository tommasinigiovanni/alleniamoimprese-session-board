"""File browser: list, download, upload and create folders below BOARD_FILES_ROOT."""
import errno
import os
import shutil
import stat
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_file, session

MAX_ENTRIES = 5000
CHUNK = 1024 * 1024


class FilesError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status_code = status


def _root():
    return Path(current_app.config['FILES_ROOT']).expanduser().resolve()


def _relative(path):
    root = _root()
    return '' if path == root else str(path.relative_to(root))


def _resolve(value):
    if not isinstance(value, str) or '\0' in value or len(value) > 4096:
        raise FilesError('Percorso non valido')
    root = _root()
    path = (root / value.lstrip('/')).resolve()
    if path != root and root not in path.parents:
        raise FilesError('Percorso fuori dalla cartella consentita', 403)
    return path


def _name(value):
    if (not isinstance(value, str) or not value or value in {'.', '..'} or '/' in value or '\0' in value
            or len(value.encode()) > 255):
        raise FilesError('Nome non valido')
    return value


def _directory(value):
    path = _resolve(value)
    if not path.is_dir():
        raise FilesError('Cartella non trovata', 404)
    return path


def _writable():
    if not current_app.config['ALLOW_SEND']:
        raise FilesError('Questa istanza è in sola lettura', 403)


def _entry(item):
    try:
        info = item.stat(follow_symlinks=True)
        kind = 'dir' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'other'
        size, mtime = info.st_size, info.st_mtime
    except OSError:
        kind, size, mtime = 'broken', None, None
    return dict(name=item.name, type=kind, link=item.is_symlink(),
                size=size if kind == 'file' else None, mtime=mtime)


def create_blueprint():
    bp = Blueprint('files', __name__)

    @bp.errorhandler(FilesError)
    def files_error(error):
        return jsonify(error=str(error)), error.status_code

    @bp.errorhandler(PermissionError)
    def permission_error(_error):
        return jsonify(error='Permesso negato'), 403

    @bp.get('/files')
    def page():
        return render_template('files.html', allow_write=current_app.config['ALLOW_SEND'],
                               csrf=session.get('csrf', ''))

    @bp.get('/api/files/list')
    def listing():
        path = _directory(request.args.get('path', ''))
        entries = []
        with os.scandir(path) as items:
            for item in items:
                if len(entries) >= MAX_ENTRIES:
                    break
                entries.append(_entry(item))
        entries.sort(key=lambda e: (e['type'] != 'dir', e['name'].casefold()))
        root = _root()
        usage = shutil.disk_usage(path)
        return jsonify(path=_relative(path), absolute=str(path), root=str(root),
                       parent=None if path == root else _relative(path.parent),
                       entries=entries, truncated=len(entries) >= MAX_ENTRIES,
                       writable=current_app.config['ALLOW_SEND'] and os.access(path, os.W_OK | os.X_OK),
                       allow_write=current_app.config['ALLOW_SEND'], free_bytes=usage.free,
                       max_upload_bytes=current_app.config['FILES_MAX_BYTES'])

    @bp.get('/api/files/download')
    def download():
        path = _resolve(request.args.get('path', ''))
        if not path.is_file():
            raise FilesError('File non trovato', 404)
        if not os.access(path, os.R_OK):
            raise PermissionError
        return send_file(path, as_attachment=True, download_name=path.name, max_age=0)

    @bp.post('/api/files/mkdir')
    def mkdir():
        _writable()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise FilesError('Invia un oggetto JSON valido')
        parent = _directory(body.get('dir', ''))
        target = parent / _name(body.get('name'))
        try:
            target.mkdir(mode=0o755)
        except FileExistsError:
            raise FilesError('Esiste già un elemento con questo nome', 409) from None
        return jsonify(ok=True, path=_relative(target))

    @bp.put('/api/files/upload')
    def upload():
        _writable()
        limit = current_app.config['FILES_MAX_BYTES']
        length = request.content_length
        if length is None:
            raise FilesError('Dimensione del file mancante', 411)
        if length > limit:
            raise FilesError(f'Il file supera il limite di {limit // (1024 * 1024)} MiB', 413)
        parent = _directory(request.args.get('dir', ''))
        name = _name(request.args.get('name'))
        overwrite = request.args.get('overwrite') == '1'
        target = parent / name
        if target.is_dir():
            raise FilesError('Esiste una cartella con questo nome', 409)
        if target.exists() and not overwrite:
            raise FilesError('Esiste già un file con questo nome', 409)
        if shutil.disk_usage(parent).free < length + 64 * 1024 * 1024:
            raise FilesError('Spazio su disco insufficiente', 507)

        request.max_content_length = limit
        descriptor, temporary = tempfile.mkstemp(dir=parent, prefix='.' + name[:60] + '.', suffix='.upload')
        try:
            received = 0
            with os.fdopen(descriptor, 'wb') as output:
                os.fchmod(output.fileno(), 0o644)
                while True:
                    chunk = request.stream.read(min(CHUNK, length - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > length:
                        raise FilesError('Dimensione del file non coerente')
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != length:
                raise FilesError('Caricamento interrotto: riprova', 400)
            if overwrite:
                os.replace(temporary, target)
            else:
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    raise FilesError('Esiste già un file con questo nome', 409) from None
                except OSError as error:
                    if error.errno not in {errno.EPERM, errno.ENOTSUP, errno.EXDEV}:
                        raise
                    if target.exists():
                        raise FilesError('Esiste già un file con questo nome', 409) from None
                    os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return jsonify(ok=True, path=_relative(target), size=received)

    return bp
