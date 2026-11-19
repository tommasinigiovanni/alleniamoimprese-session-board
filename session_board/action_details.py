"""Display-only action details. Pure stdlib; never execute or evaluate input.

The owner must be able to read what the agent runs: commands, scripts and file
paths are shown as written, including multiline scripts. Only recognized
credentials are masked before display: private keys, well-known token formats,
authentication headers, passwords inside URLs and values assigned to names
such as password, secret, token or api key.
"""
import re

MAX_DETAIL = 24000
MAX_INPUT = 65536
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_.:-]{0,199}\Z')
_SECRET = re.compile(r'token|secret|passw(?:or)?d|passwd|credential|auth|cookie|api.?key|private.?key|access.?key|signature|^key$|^user(?:name)?$', re.I)
_PRIVATE_KEY = re.compile(r'-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)', re.S)
# The last alternative is a Telegram bot token: bot number, colon, key.
_KNOWN_TOKEN = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|\d{6,}:[A-Za-z0-9_-]{30,})')
# The lookbehinds keep both patterns linear on very long words.
_URL_PASSWORD = re.compile(r'''((?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@"'`]+:)[^\s/@"'`]+(@)''')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u206f]')
_SHELL_TOOLS = {'Bash', 'exec_command', 'shell_command', 'shell'}
_FILE_TOOLS = {'Read', 'NotebookRead', 'Edit', 'Write', 'NotebookEdit', 'view_image', 'Grep', 'Glob'}
_PAYLOAD_FLAGS = {'--data', '--data-raw', '--data-binary', '--data-urlencode', '--json', '--form', '--form-string', '--body', '--post-data', '--post-file', '-d', '-F'}
_PRIVATE_FLAGS = {'--header', '--proxy-header', '-H', '-u', '-U', '-p', '-P', '-b'}
_HEADER = re.compile(r'(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*:)[^\r\n"\']*')
_NAMED_SECRET = re.compile(r'''(?i)((?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]*?(?:password|passwd|secret|token|api[_-]?key)[A-Za-z0-9_]*["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s;&|)]+)''')
_LABEL_FLAG = re.compile(r'''(?<![\w-])(--[A-Za-z][A-Za-z0-9_-]*|-[uUpPHbdF])(=|\s+|(?=\S))("(?:\\.|[^"\\])*(?:"|\Z)|'(?:\\.|[^'\\])*(?:'|\Z)|[^\s"'=\-][^\s]*)''')


def _scrub(value):
    """Mask recognized credentials. Everything else stays as written."""
    value = _CONTROL.sub('', value)
    value = _PRIVATE_KEY.sub('[chiave privata omessa]', value)
    value = _HEADER.sub(r'\1 [intestazione omessa]', value)
    value = _KNOWN_TOKEN.sub('[omesso]', value)
    return _URL_PASSWORD.sub(r'\1[omesso]\2', value)


def sanitize_text(value):
    """Return the command or script as written, with credentials masked.

    This is a display projection, not a command for re-execution. Newlines and
    shell syntax are preserved so the owner reads exactly what was run.
    """
    if not isinstance(value, str):
        return ''
    value = _scrub(value[:MAX_INPUT])
    return _NAMED_SECRET.sub(r'\1[omesso]', value).strip()


def sanitize_detail(value):
    clean = sanitize_text(value)
    truncated = isinstance(value, str) and len(value) > MAX_INPUT or len(clean) > MAX_DETAIL
    result = {'detail': clean[:MAX_DETAIL - 1] + '…' if len(clean) > MAX_DETAIL else clean}
    if truncated:
        result['detail_truncated'] = True
    return result


def sanitize_label(value):
    """Redact legacy human descriptions without interpreting them as a shell."""
    if not isinstance(value, str):
        return ''
    if len(value) > MAX_INPUT:
        return '[descrizione troppo lunga omessa]'
    value = _scrub(value)
    value = re.sub(r'''(?i)(\b[A-Za-z_][A-Za-z0-9_.-]*["']?\s*=\s*|\b(?:password|passwd|secret|token|api[_-]?key)["']?\s*:\s*)(?:"[^"]*"|'[^']*'|[^\s;]+)''', r'\1[omesso]', value)
    value = _LABEL_FLAG.sub(lambda match: match[1] + (match[2] or ' ') + '[omesso]'
                            if match[1] in _PRIVATE_FLAGS | _PAYLOAD_FLAGS or _SECRET.search(match[1].lstrip('-'))
                            else match[0], value)
    return value


def _file_detail(arguments):
    """The path of the file the tool touches, as written."""
    value = next((arguments[key] for key in ('file_path', 'notebook_path', 'path')
                  if isinstance(arguments.get(key), str) and arguments[key]), None)
    return value[:4096] if value else None


def describe_details(name, arguments=None, namespace=None):
    arguments = arguments if isinstance(arguments, dict) else {}
    qualified = name if not isinstance(namespace, str) or name.startswith(namespace + '.') else namespace + '.' + name
    tool = qualified if isinstance(qualified, str) and _NAME.fullmatch(qualified) and _scrub(qualified) == qualified else '[strumento omesso]'
    result = {'tool': tool}
    short = name.rsplit('.', 1)[-1]
    if short in _SHELL_TOOLS:
        command = arguments.get('command', arguments.get('cmd'))
        if isinstance(command, str) and command:
            result.update(sanitize_detail(command))
    elif short in _FILE_TOOLS:
        detail = _file_detail(arguments)
        if detail:
            result.update(sanitize_detail(detail))
    elif qualified in ('exec', 'functions.exec'):
        script = arguments.get('code')
        if isinstance(script, str) and script:
            result.update(sanitize_detail(script))
    return result
