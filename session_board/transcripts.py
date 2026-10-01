"""Bounded transcript reads and display redaction shared by board features."""
import json
import os
import re
import stat
from urllib.parse import parse_qsl, unquote, urlsplit


MAX_TRANSCRIPT = 2 * 1024 * 1024
UUID = re.compile(r'^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$')
SENSITIVE = re.compile(r'(?:secret|credential|password|token|private[-_]?key|^id_(?:rsa|ed25519)|^config[.])', re.I)


class TranscriptRows(list):
    """Parsed rows plus the file boundary read, including unparsed trailing bytes."""
    def __init__(self, identity, before_offset):
        super().__init__()
        self.transcript_identity = identity
        self.before_offset = before_offset


def _url(value):
    try:
        if any(ord(c) < 33 for c in value) or len(value) > 2048:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
            return None
        query = unquote(parsed.query)
        if SENSITIVE.search(query) or re.search(r'(?:^|&)(?:key|sig|auth|code)=', query, re.I):
            return None
        names = [name for name, _ in parse_qsl(query, keep_blank_values=True, max_num_fields=100)]
        if any(re.search(r'(?:signature|token|credential)s?$', name, re.I) for name in names):
            return None
        # OAuth clients use fragment parameters; ordinary document anchors
        # contain no assignments and remain useful links.
        if '=' in unquote(parsed.fragment):
            return None
        return value
    except ValueError:
        return None


def _redact(text):
    text = re.sub(r'https?://[^\s<>"`\])]+', lambda match: match.group(0) if _url(match.group(0)) else '[link riservato omesso]', text)
    text = re.sub(r'-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)', '[chiave privata omessa]', text, flags=re.S)
    text = re.sub(r'''(?im)(\b(?:api[_-]?key|access[_-]?token|password|secret|authorization)\b["']?\s*[=:]\s*)[^\r\n]+''', r'\1[omesso]', text)
    return re.sub(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b', '[omesso]', text)


def _events(path, sid, cwd, max_row=512 * 1024, follow_cwd_transitions=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError('Transcript non regolare')
        offset = max(0, info.st_size - MAX_TRANSCRIPT)
        file.seek(offset)
        if offset:
            file.readline(MAX_TRANSCRIPT)
        offset = file.tell()
        raw = file.read(MAX_TRANSCRIPT)
    rows = TranscriptRows(f'{info.st_dev}:{info.st_ino}', max(info.st_size, offset + len(raw)) - 1)
    for line, data in enumerate(raw.splitlines(keepends=True), 1):
        position = offset
        offset += len(data)
        if len(data) > max_row:
            continue
        try:
            row = json.loads(data)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict) or row.get('sessionId') != sid:
            continue
        main = not any(row.get(flag) for flag in ('isSidechain', 'isMeta', 'isCompactSummary'))
        identifier = row.get('uuid')
        valid_id = isinstance(identifier, str) and UUID.fullmatch(identifier) is not None
        row_cwd = row.get('cwd', cwd)
        if row_cwd != cwd:
            # Claude can change its operational cwd while retaining the same
            # process-owned transcript. The chat caller verifies that exact
            # account/file/SID against the live process before and after read.
            # parentUuid is not an independent authority and its anchor may
            # be outside the bounded tail. Legacy readers remain cwd-strict.
            if not (follow_cwd_transitions and main and valid_id
                    and isinstance(row_cwd, str) and os.path.isabs(row_cwd)):
                continue
        source = {'line': line if info.st_size <= MAX_TRANSCRIPT else None, 'byte_offset': position,
                  'role': row.get('type', 'unknown'), 'conversation_id': sid}
        rows.append((row, source))
    return rows, info.st_size > MAX_TRANSCRIPT
