"""Private, bounded raster uploads shared by the full and portable terminals.

Only server-created PNG files recorded with their inode and change timestamps
may be removed. A directory lock covers the index, expiry and quota across
threads, ImageStore instances and processes. No client filenames are retained.
"""
from contextlib import contextmanager
import fcntl
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from PIL import Image, ImageOps, UnidentifiedImageError


class ImageError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class _BoundedOutput(io.BytesIO):
    def __init__(self, maximum):
        super().__init__()
        self.maximum = maximum

    def write(self, value):
        if self.tell() + len(value) > self.maximum:
            raise ImageError('L’immagine convertita supera il limite di spazio consentito.', 413)
        return super().write(value)


class RasterDecoder:
    """Decode bounded raster bytes into a fresh PNG, without filesystem access."""
    MAX_INPUT_BYTES = 10 * 1024 * 1024
    MAX_OUTPUT_BYTES = 32 * 1024 * 1024
    MAX_PIXELS = 25_000_000
    MAX_FRAMES = 256
    def _decode(self, upload):
        stream = getattr(upload, 'stream', upload)
        data = bytearray()
        try:
            while len(data) <= self.MAX_INPUT_BYTES:
                chunk = stream.read(min(65536, self.MAX_INPUT_BYTES + 1 - len(data)))
                if not isinstance(chunk, bytes):
                    raise ImageError('Il file immagine non è leggibile.')
                if not chunk:
                    break
                data.extend(chunk)
        except (OSError, AttributeError, TypeError, ValueError):
            raise ImageError('Il file immagine non è leggibile.') from None
        if len(data) > self.MAX_INPUT_BYTES:
            raise ImageError('L’immagine supera il limite di 10 MiB.', 413)
        if not data:
            raise ImageError('Scegli un’immagine PNG, JPEG, WebP o GIF valida.')
        self._preflight_webp(data)
        try:
            with Image.open(io.BytesIO(data), formats=('PNG', 'JPEG', 'WEBP', 'GIF')) as probe:
                self._check_pixels(probe.size)
                # Some decoders tolerate a missing final marker. Treat it as
                # a partial upload instead of silently repairing the source.
                if ((probe.format == 'JPEG' and not data.endswith(b'\xff\xd9'))
                        or (probe.format == 'GIF' and not data.endswith(b';'))):
                    raise ImageError('L’immagine è incompleta o danneggiata.')
                probe.verify()
            with Image.open(io.BytesIO(data), formats=('PNG', 'JPEG', 'WEBP', 'GIF')) as source:
                source.load()
                ImageOps.exif_transpose(source, in_place=True)
                mode = 'RGBA' if 'A' in source.getbands() or 'transparency' in source.info else 'RGB'
                # A fresh image object carries pixels only, so neither EXIF,
                # ICC profiles nor text chunks survive PNG serialization.
                clean = Image.new(mode, source.size)
                clean.paste(source.convert(mode))
                pixels = source.width * source.height
                frames = 1
                while True:
                    try:
                        source.seek(frames)
                    except EOFError:
                        break
                    frames += 1
                    pixels += source.width * source.height
                    if frames > self.MAX_FRAMES or pixels > self.MAX_PIXELS:
                        raise ImageError('L’animazione supera il limite di decodifica consentito.', 413)
                    source.load()
                output = _BoundedOutput(self.MAX_OUTPUT_BYTES)
                clean.save(output, format='PNG')
                clean.close()
                return output.getvalue()
        except ImageError:
            raise
        except Image.DecompressionBombError:
            raise ImageError('L’immagine supera il limite di 25 milioni di pixel.', 413) from None
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
            raise ImageError('Scegli un’immagine PNG, JPEG, WebP o GIF valida e completa.') from None

    def _check_pixels(self, size):
        if min(size) <= 0 or size[0] * size[1] > self.MAX_PIXELS:
            raise ImageError('L’immagine supera il limite di 25 milioni di pixel.', 413)

    def _preflight_webp(self, data):
        """Check dimensions before Pillow constructs libwebp's AnimDecoder.

        Container fields: developers.google.com/speed/webp/docs/riff_container
        VP8 dimensions: RFC 6386 section 9.1; VP8L: lossless bitstream header.
        This bounds allocations; Pillow still validates and decodes the data.
        """
        if data[:4] != b'RIFF' or data[8:12] != b'WEBP':
            return
        invalid = 'Il contenitore WebP è incompleto o non valido.'
        if len(data) < 20 or int.from_bytes(data[4:8], 'little') + 8 != len(data):
            raise ImageError(invalid)

        def chunks(blob, start, end):
            view = memoryview(blob)
            while start < end:
                if end - start < 8:
                    raise ImageError(invalid)
                kind = bytes(view[start:start + 4])
                size = int.from_bytes(view[start + 4:start + 8], 'little')
                payload_start = start + 8
                following = payload_start + size + (size & 1)
                if following > end or (size & 1 and view[following - 1] != 0):
                    raise ImageError(invalid)
                yield kind, view[payload_start:payload_start + size]
                start = following

        def bitstream_size(kind, payload):
            if kind == b'VP8 ':
                if len(payload) < 10 or payload[0] & 1 or payload[3:6] != b'\x9d\x01\x2a':
                    raise ImageError(invalid)
                size = (int.from_bytes(payload[6:8], 'little') & 0x3fff,
                        int.from_bytes(payload[8:10], 'little') & 0x3fff)
            else:
                if len(payload) < 5 or payload[0] != 0x2f:
                    raise ImageError(invalid)
                bits = int.from_bytes(payload[1:5], 'little')
                if bits >> 29:
                    raise ImageError(invalid)
                size = ((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1)
            self._check_pixels(size)
            return size

        canvas, still, frames = None, 0, 0
        for position, (kind, payload) in enumerate(chunks(data, 12, len(data))):
            if kind == b'VP8X':
                if position != 0 or canvas is not None or len(payload) != 10:
                    raise ImageError(invalid)
                canvas = (int.from_bytes(payload[4:7], 'little') + 1,
                          int.from_bytes(payload[7:10], 'little') + 1)
                self._check_pixels(canvas)
            elif kind in {b'VP8 ', b'VP8L'}:
                size = bitstream_size(kind, payload)
                still += 1
                if still > 1 or frames or (canvas is not None and size != canvas):
                    raise ImageError(invalid)
            elif kind == b'ANMF':
                if canvas is None or len(payload) < 16 or still:
                    raise ImageError(invalid)
                frame = (int.from_bytes(payload[6:9], 'little') + 1,
                         int.from_bytes(payload[9:12], 'little') + 1)
                self._check_pixels(frame)
                x = int.from_bytes(payload[:3], 'little') * 2
                y = int.from_bytes(payload[3:6], 'little') * 2
                if x + frame[0] > canvas[0] or y + frame[1] > canvas[1]:
                    raise ImageError(invalid)
                frames += 1
                if frames > self.MAX_FRAMES or canvas[0] * canvas[1] * frames > self.MAX_PIXELS:
                    raise ImageError('L’animazione supera il limite di decodifica consentito.', 413)
                # ANMF holds another RIFF chunk sequence. Validate every
                # declared bitstream size, not only the enclosing canvas.
                encoded_frames = 0
                for subkind, subpayload in chunks(payload, 16, len(payload)):
                    if subkind in {b'VP8 ', b'VP8L'}:
                        if bitstream_size(subkind, subpayload) != frame:
                            raise ImageError(invalid)
                        encoded_frames += 1
                    elif subkind in {b'VP8X', b'ANMF'}:
                        raise ImageError(invalid)
                if encoded_frames != 1:
                    raise ImageError(invalid)
        if not (still or frames):
            raise ImageError(invalid)

class ImageStore(RasterDecoder):
    MAX_TOTAL_BYTES = 256 * 1024 * 1024
    RETENTION_SECONDS = 7 * 86400
    _MAX_INDEX_BYTES = 4 * 1024 * 1024
    _NAME = re.compile(r'image-[0-9a-f]{32}\.png')
    _INDEX = '.images.json'

    def __init__(self, state_dir, *, clock=time.time):
        self.directory = Path(state_dir).expanduser().absolute() / 'terminal-images'
        self.clock = clock
        self._created = {}

    def _open_directory(self):
        """Walk with dirfds: even parent symlinks cannot redirect a write."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open(self.directory.anchor, flags)
        try:
            for part in self.directory.parts[1:]:
                if part in {'.', '..'}:
                    raise ImageError('Directory delle immagini non utilizzabile.', 503)
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = child
            if os.fstat(current).st_uid != os.geteuid():
                raise ImageError('Directory delle immagini non utilizzabile.', 503)
            os.fchmod(current, 0o700)
            return current
        except BaseException:
            os.close(current)
            raise

    @contextmanager
    def _locked(self):
        directory_fd = lock_fd = None
        try:
            directory_fd = self._open_directory()
            lock_fd = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=directory_fd)
            self._check_private_file(os.fstat(lock_fd))
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield directory_fd
        except OSError:
            raise ImageError('Archivio immagini non disponibile. Riprova più tardi.', 503) from None
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    @staticmethod
    def _check_private_file(info):
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ImageError('Archivio immagini non utilizzabile.', 503)

    @staticmethod
    def _identity(info):
        return [info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size]

    def _read_index(self, directory_fd):
        try:
            fd = os.open(self._INDEX, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=directory_fd)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, 'rb') as handle:
            self._check_private_file(os.fstat(handle.fileno()))
            raw = handle.read(self._MAX_INDEX_BYTES + 1)
        try:
            data = json.loads(raw)
            if len(raw) > self._MAX_INDEX_BYTES or not isinstance(data, dict) or data.get('version') != 1:
                raise ValueError()
            records = data['files']
            if not isinstance(records, dict):
                raise ValueError()
            for name, record in records.items():
                if (not self._NAME.fullmatch(name) or not isinstance(record, dict)
                        or type(record.get('created')) not in {int, float}
                        or not math.isfinite(record['created'])
                        or not isinstance(record.get('identity'), list)
                        or len(record['identity']) != 5
                        or any(type(value) is not int for value in record['identity'])):
                    raise ValueError()
            return records
        except (ValueError, KeyError, TypeError):
            raise ImageError('Registro delle immagini non leggibile. Nessun file è stato rimosso.', 503) from None

    def _write_index(self, directory_fd, records):
        data = json.dumps({'version':1, 'files':records}, separators=(',', ':'), allow_nan=False).encode()
        if len(data) > self._MAX_INDEX_BYTES:
            raise ImageError('Archivio immagini pieno. Attendi la scadenza dei file precedenti.', 413)
        temporary = '.index-' + secrets.token_hex(16) + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory_fd)
        identity = None
        replaced = False
        try:
            with os.fdopen(fd, 'wb') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                identity = self._identity(os.fstat(handle.fileno()))
            os.replace(temporary, self._INDEX, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            replaced = True
        finally:
            try:
                if not replaced and identity is not None and self._matches(directory_fd, temporary, identity):
                    os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass

    def _matches(self, directory_fd, name, identity):
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and info.st_nlink == 1 and self._identity(info) == identity)
        except FileNotFoundError:
            return False

    def _expire(self, directory_fd, records):
        cutoff = self.clock() - self.RETENTION_SECONDS
        for name, record in list(records.items()):
            matches = self._matches(directory_fd, name, record['identity'])
            if not matches or record['created'] <= cutoff:
                if matches:
                    os.unlink(name, dir_fd=directory_fd)
                del records[name]
                self._created.pop(name, None)

    def save(self, upload):
        """Validate a FileStorage/binary stream and return its private PNG Path."""
        with self._locked() as directory_fd:
            # Decoding is serialized too: concurrent uploads must not multiply
            # the memory cost of the maximum-size uncompressed image.
            data = self._decode(upload)
            records = self._read_index(directory_fd)
            self._expire(directory_fd, records)
            total = 0
            for name in os.listdir(directory_fd):
                if name in {self._INDEX, '.lock'}:
                    continue
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            if total + len(data) > self.MAX_TOTAL_BYTES:
                self._write_index(directory_fd, records)
                raise ImageError('Archivio immagini pieno: limite complessivo di 256 MiB.', 413)
            name = 'image-' + secrets.token_hex(16) + '.png'
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory_fd)
            identity = None
            try:
                with os.fdopen(fd, 'wb') as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                    identity = self._identity(os.fstat(handle.fileno()))
                records[name] = {'created':self.clock(), 'identity':identity}
                self._write_index(directory_fd, records)
            except BaseException:
                # A failed index write must not authorize deleting a file
                # replaced or modified since the upload was written. Earlier
                # failures have no completed receipt, so preserve the file.
                try:
                    if identity is not None and self._matches(directory_fd, name, identity):
                        os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
                raise
            self._created[name] = identity
            return self.directory / name

    def discard(self, path):
        """Remove only an unchanged upload returned by this ImageStore instance."""
        path = Path(path)
        if path.parent != self.directory or path.name not in self._created:
            return False
        with self._locked() as directory_fd:
            identity = self._created.pop(path.name, None)
            records = self._read_index(directory_fd)
            record = records.get(path.name)
            if not record or record['identity'] != identity or not self._matches(directory_fd, path.name, identity):
                return False
            os.unlink(path.name, dir_fd=directory_fd)
            records.pop(path.name)
            self._write_index(directory_fd, records)
            return True
