"""Display-only action details. Pure stdlib; never execute or evaluate input.

Supported detail fields are shell commands, file paths, and literal tool calls
inside functions.exec. Other arguments, scripts and patches are not displayed.
"""
import json
import re
import shlex
from urllib.parse import urlsplit

MAX_DETAIL = 4000
MAX_INPUT = 65536
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_.:-]{0,199}\Z')
_SECRET = re.compile(r'token|secret|passw(?:or)?d|passwd|credential|auth|cookie|api.?key|private.?key|access.?key|signature|^key$|^user(?:name)?$', re.I)
_PRIVATE_KEY = re.compile(r'-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)', re.S)
_KNOWN_TOKEN = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b')
_URL = re.compile(r'''[A-Za-z][A-Za-z0-9+.-]*://[^\s<>"'`]+''')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u206f]')
_SHELL_TOOLS = {'Bash', 'exec_command', 'shell_command', 'shell'}
_FILE_TOOLS = {'Read', 'NotebookRead', 'Edit', 'Write', 'NotebookEdit', 'view_image', 'Grep', 'Glob'}
_PAYLOAD_FLAGS = {'--data', '--data-raw', '--data-binary', '--data-urlencode', '--json', '--form', '--form-string', '--body', '--post-data', '--post-file', '-d', '-F'}
_PRIVATE_FLAGS = {'--header', '--proxy-header', '-H', '-u', '-U', '-p', '-P', '-b'}
_RUNTIME = re.compile(r'(?:python|ruby|perl|php)(?:\d+(?:\.\d+)*)?|node(?:js)?|(?:ba|da|z|fi|k)?sh|pwsh|powershell', re.I)
_SAFE_MODULES = {'pytest', 'unittest', 'pip', 'compileall', 'py_compile', 'http.server'}
_NETWORK_FLAGS = {'--fail', '--silent', '--show-error', '--location', '--head', '--verbose',
                  '--insecure', '--compressed', '--globoff', '--http1.1', '--http2', '--include', '--help', '--version'}
_HEADER = re.compile(r'(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*:)[^\r\n]*')
_LABEL_FLAG = re.compile(r'''(?<![\w-])(--[A-Za-z][A-Za-z0-9_-]*|-[uUpPHbdF])(=|\s+|(?=\S))("(?:\\.|[^"\\])*(?:"|\Z)|'(?:\\.|[^'\\])*(?:'|\Z)|[^\s"'=\-][^\s]*)''')
_UNSUPPORTED_SHELL = '[dettaglio omesso: sintassi shell non supportata]'
_SCRIPT_TOOLS = {'printf', 'echo', 'eval', 'jq', 'sed', 'awk'}
_OMISSION_MARKERS = frozenset({
    '[omesso]', '[URL riservato omesso]', '[chiave privata omessa]',
    '[intestazione omessa]', '[script omesso]', '[argomenti riservati omessi]',
    '[opzioni omesse]', '[contenuto omesso]', '[commento omesso]',
    '[contenuto troppo lungo omesso]', '[descrizione troppo lunga omessa]',
    '[script troppo lungo omesso]', '[script non interpretabile omesso]',
    '[strumento omesso]', _UNSUPPORTED_SHELL,
})
_OMISSION_TOKEN = re.compile('(?:' + '|'.join(re.escape(marker) for marker in _OMISSION_MARKERS)
                            + r')(?=\s|$)')


def _url(match):
    value = match.group(0)
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return '[URL riservato omesso]'
    except ValueError:
        return '[URL riservato omesso]'
    return value


def _scrub(value, *, urls=True, headers=True):
    value = _CONTROL.sub('', value)
    value = _PRIVATE_KEY.sub('[chiave privata omessa]', value)
    if headers:
        value = _HEADER.sub(r'\1 [intestazione omessa]', value)
    value = _KNOWN_TOKEN.sub('[omesso]', value)
    return _URL.sub(_url, value) if urls else value


def _shell_tokens(value):
    """Token spans preserve the original formatting of supported commands."""
    result, index = [], 0
    while index < len(value):
        if value[index].isspace():
            index += 1
            continue
        # A second display filter must not split our own multiword markers.
        # Only exact, complete static markers qualify; later tokens still pass
        # through the ordinary credential and script checks.
        marker = _OMISSION_TOKEN.match(value, index)
        if marker:
            result.append((index, marker.end(), marker.group()))
            index = marker.end()
            continue
        start, quote = index, None
        while index < len(value):
            char = value[index]
            if char == '\\' and quote != "'":
                index += 2
                continue
            if quote:
                if char == quote:
                    quote = None
            elif char in ('"', "'"):
                quote = char
            elif char in ';&|<>()':
                return None
            elif char.isspace():
                break
            index += 1
        raw = value[start:index]
        try:
            decoded = shlex.split(raw)
        except ValueError:
            return None
        if len(decoded) != 1:
            return None
        result.append((start, index, decoded[0]))
    return result


def _prefix(value):
    # Used only when the full payload cannot be safely parsed. Never show args.
    match = re.match(r'\s*([A-Za-z0-9_./-]{1,160})(?:\s|[;&|<>()]|$)', value)
    return match.group(1) if match and not (_SECRET.search(match.group(1)) or _KNOWN_TOKEN.search(match.group(1))) else 'Comando'


def sanitize_text(value):
    """Redact supported textual details before any display-length truncation.

This is not a command for re-execution: omitted arguments are explicit. Input
outside the bounded parser, embedded programs and payloads fail closed.
"""
    if not isinstance(value, str):
        return ''
    if len(value) > MAX_INPUT:
        return _prefix(value) + ' [contenuto troppo lungo omesso]'
    value = _scrub(value, urls=False, headers=False)
    if not value.strip():
        return ''
    # Here-documents, substitutions and multiline programs can contain secrets
    # unrelated to parameter names. Keep only the outer executable.
    if any(marker in value for marker in ('\n', '\r', '<<', '$(', '`')):
        return _prefix(value) + ' [script omesso]'
    tokens = _shell_tokens(value)
    if tokens is None:
        prefix = _prefix(value)
        base = prefix.rsplit('/', 1)[-1]
        reason = '[script omesso]' if base in _SCRIPT_TOOLS or _RUNTIME.fullmatch(base) else _UNSUPPORTED_SHELL
        return prefix + ' ' + reason
    edits, hide_next, network = [], False, False
    for index, (start, end, decoded) in enumerate(tokens):
        if hide_next:
            edits.append((start, end, '[omesso]'))
            hide_next = False
            continue
        if decoded in _OMISSION_MARKERS:
            continue
        base = decoded.rsplit('/', 1)[-1]
        if _RUNTIME.fullmatch(base.removesuffix('.exe')):
            remaining = [token[2] for token in tokens[index + 1:]]
            supported = (not remaining or remaining in (['--version'], ['-V'], ['--help'], ['-h'])
                         or len(remaining) >= 2 and remaining[0] == '-m' and remaining[1] in _SAFE_MODULES)
            if not supported:
                edits.append((end, len(value), ' [script omesso]'))
                break
        if base in {'openssl', 'redis-cli', 'psql', 'mysql', 'sshpass'}:
            edits.append((end, len(value), ' [argomenti riservati omessi]'))
            break
        if base in {'curl', 'wget'}:
            network = True
        if base in _SCRIPT_TOOLS:
            edits.append((end, len(value), ' [script omesso]'))
            break
        if decoded.startswith('#'):
            edits.append((start, len(value), '[commento omesso]'))
            break
        # Git's branch/patch flags do not carry the cookie/password values
        # associated with the same short flags in network clients. Match the
        # actual executable and operation, never an occurrence in an argument.
        if decoded in {'-b', '-p'} and tokens[0][2].rsplit('/', 1)[-1] == 'git':
            operation = [token[2] for token in tokens[1:min(index, 3)]]
            if ((decoded == '-b' and (operation[:2] == ['worktree', 'add'] or operation[:1] == ['checkout']))
                    or decoded == '-p' and operation[:1] in (['diff'], ['show'], ['log'])):
                continue
        option, separator, _ = decoded.partition('=')
        inline_script = (re.match(r'^-[ce]', decoded) is not None
                         or option.lower() in {'--eval', '--call', '--command', '--script', '--split-string'}
                         or decoded == '-S' and any(token[2] == 'env' for token in tokens[:index]))
        git_config = (decoded == '-c' and any(token[2].rsplit('/', 1)[-1] == 'git' for token in tokens[:index])
                      and index + 1 < len(tokens) and re.match(r'^[A-Za-z_][A-Za-z0-9_.-]*=', tokens[index + 1][2]))
        if inline_script and not git_config:
            edits.append((start, len(value), '[script omesso]'))
            break
        if (network and decoded.startswith('-') and decoded not in _NETWORK_FLAGS
                and not re.fullmatch(r'-[fsSLIvki]+', decoded)):
            if decoded.startswith('--'):
                edits.append((start, end, option + ('=[omesso]' if separator else '')))
                hide_next = not separator
            elif len(decoded) == 2:
                edits.append((start, end, decoded))
                hide_next = True
            else:
                edits.append((start, end, '[opzioni omesse]'))
                hide_next = True
            continue
        if len(decoded) > 2 and decoded[:2] in {'-u', '-U', '-p', '-P', '-H', '-b', '-d', '-F'} and not decoded.startswith('--'):
            edits.append((start, end, decoded[:2] + '[omesso]'))
            continue
        if decoded.startswith('-') and (option in _PAYLOAD_FLAGS | _PRIVATE_FLAGS or _SECRET.search(option.lstrip('-'))):
            if separator:
                edits.append((start, end, option + '=[omesso]'))
            else:
                edits.append((start, end, option))
                hide_next = True
            continue
        assignment = re.match(r'([A-Za-z_][A-Za-z0-9_.-]*)=', decoded)
        if assignment:
            edits.append((start, end, assignment.group(1) + '=[omesso]'))
        elif re.match(r'(?:authorization|cookie|set-cookie|x-api-key)\s*:', decoded, re.I):
            edits.append((start, len(value), '[intestazione omessa]'))
            break
        elif decoded.startswith(('{', '[')):
            edits.append((start, end, '[contenuto omesso]'))
    for start, end, replacement in reversed(edits):
        value = value[:start] + replacement + value[end:]
    # Also cover legacy prose descriptions rendered by consumers of this helper.
    value = re.sub(r'''(?i)(\b(?:password|passwd|secret|token|api[_-]?key)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s;]+)''', r'\1[omesso]', value)
    return _URL.sub(_url, value).strip()


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
    value = next((arguments[key] for key in ('file_path', 'notebook_path', 'path')
                  if isinstance(arguments.get(key), str) and arguments[key]), None)
    if value is None:
        return None
    parts = value.replace('\\', '/').split('/')
    if (len(value) > 4096 or _scrub(value) != value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char in value for char in (':', '?', '#', '='))
            or any(_SECRET.search(part) or part.lower().startswith(('.env', 'id_rsa', 'id_ed25519', 'config.')) for part in parts)
            or any(part.lower().endswith(('.pem', '.key', '.p12', '.pfx')) for part in parts)
            or not parts[-1] or not re.fullmatch(r'[\w./ -]+', value)):
        return 'File riservato'
    return value


def _js_tokens(script):
    """Small static lexer: comments/strings cannot masquerade as tool calls."""
    tokens, index = [], 0
    while index < len(script):
        char = script[index]
        if char.isspace():
            index += 1
        elif script.startswith('//', index):
            stop = script.find('\n', index + 2)
            index = len(script) if stop == -1 else stop + 1
        elif script.startswith('/*', index):
            stop = script.find('*/', index + 2)
            index = len(script) if stop == -1 else stop + 2
        elif char == '/':
            # A regex literal can contain fake calls. This limited lexer does
            # not guess regex-versus-division context, so omit its whole script.
            return None
        elif char in ('"', "'", '`'):
            quote, start = char, index + 1
            index += 1
            while index < len(script) and script[index] != quote:
                index += 2 if script[index] == '\\' else 1
            raw = script[start:index]
            literal = None
            if quote == '`' and '${' in raw:
                return None
            if index < len(script):
                try:
                    if quote == '"':
                        literal = json.loads('"' + raw + '"')
                    elif not re.search(r'\\(?![\\\'"/bfnrt])', raw):
                        escapes = {'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
                        literal = re.sub(r'\\(.)', lambda m: escapes.get(m[1], m[1]), raw)
                except (ValueError, UnicodeError):
                    pass
            tokens.append(('string', literal))
            index += 1
        elif char.isalpha() or char in '_$':
            start = index
            while index < len(script) and (script[index].isalnum() or script[index] in '_$'):
                index += 1
            tokens.append(('name', script[start:index]))
        else:
            tokens.append((char, char))
            index += 1
    return tokens


def _exec_detail(script):
    if not isinstance(script, str) or not script:
        return {}
    if len(script) > MAX_INPUT:
        return {'detail': '[script troppo lungo omesso]', 'detail_truncated': True}
    tokens, lines = _js_tokens(script), []
    if tokens is None:
        return {'detail': '[script non interpretabile omesso]'}
    for index in range(len(tokens) - 3):
        if not (tokens[index:index + 2] == [('name', 'tools'), ('.', '.')]
                and (index == 0 or tokens[index - 1][0] not in ('.', '?'))
                and tokens[index + 2][0] == 'name' and tokens[index + 3][0] == '('):
            continue
        name = tokens[index + 2][1]
        if not _NAME.fullmatch(name):
            continue
        line = 'tools.' + name
        # Only an object literal's top-level cmd/command string qualifies. No
        # interpolation, concatenation, variables, spreads or evaluating JS.
        args, depth, cursor = [], 0, index + 4
        if cursor < len(tokens) and tokens[cursor][0] == '{':
            while cursor < len(tokens):
                token = tokens[cursor]
                args.append((depth, token))
                if token[0] in ('{', '[', '('):
                    depth += 1
                elif token[0] in ('}', ']', ')'):
                    depth -= 1
                cursor += 1
                if depth == 0:
                    break
            spread = any(args[pos:pos + 3] == [(1, ('.', '.'))] * 3 for pos in range(len(args) - 2))
            computed = any(level == 1 and token[0] == '[' for level, token in args)
            if not spread and not computed and depth == 0 and name in {'exec_command', 'shell_command', 'shell'}:
                candidates = []
                for pos in range(1, len(args) - 3):
                    level, token = args[pos]
                    if (level == 1 and token[0] in ('name', 'string') and token[1] in ('cmd', 'command')
                            and args[pos - 1][1][0] in ('{', ',') and args[pos + 1][1][0] == ':'):
                        value, following = args[pos + 2][1], args[pos + 3][1]
                        candidates.append(value[1] if value[0] == 'string' and isinstance(value[1], str)
                                          and following[0] in (',', '}') else None)
                if len(candidates) == 1 and candidates[0] is not None:
                    line += '\n' + sanitize_text(candidates[0])
        lines.append(line)
    clean = '\n\n'.join(lines)
    if not clean:
        return {}
    return {'detail': clean[:MAX_DETAIL - 1] + '…', 'detail_truncated': True} if len(clean) > MAX_DETAIL else {'detail': clean}


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
        result.update(_exec_detail(arguments.get('code')))
    return result
