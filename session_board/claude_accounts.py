"""Native Claude profiles with explicit home and isolated CLI environments.

Based on the account contracts of dashboard-ext e543853. Credentials are
created by the official CLI; this module neither reads nor copies them.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

PRINCIPAL_SLUG = 'principale'
CLAUDE_BIN = os.environ.get('CLAUDE_STATUS_BIN', 'claude')
RESERVED_SLUGS = frozenset({'mem'})
SHARED_ENTRIES = ('projects', 'sessions')
_SLUG_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_CONVERSATION_ID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_AUTH_ENV = frozenset({
    'CLAUDE_CONFIG_DIR', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_API_KEY',
    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
    'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR', 'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR',
    'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT',
})


def valid_slug(slug):
    return isinstance(slug, str) and bool(_SLUG_RE.fullmatch(slug))


def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and bool(_EMAIL_RE.fullmatch(value))


def valid_conversation_id(value):
    return isinstance(value, str) and bool(_CONVERSATION_ID_RE.fullmatch(value))


def _home(home=None):
    return Path(home).expanduser().absolute() if home is not None else Path.home()


def config_dir(slug, home=None):
    if not valid_slug(slug) or slug in RESERVED_SLUGS:
        raise ValueError('Nome abbonamento non valido o riservato')
    return str(_home(home) / ('.claude' if slug == PRINCIPAL_SLUG else '.claude-' + slug))


def slug_from_dir(path):
    name = Path(path).name
    if name == '.claude':
        return PRINCIPAL_SLUG
    slug = name[8:] if name.startswith('.claude-') else None
    return slug if valid_slug(slug) and slug not in RESERVED_SLUGS else None


def list_slugs(home=None, strict=False):
    found = []
    try:
        with os.scandir(_home(home)) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                slug = slug_from_dir(entry.name)
                if slug and slug != PRINCIPAL_SLUG:
                    found.append(slug)
    except OSError:
        if strict:
            raise
    return [PRINCIPAL_SLUG] + sorted(found)


def account_env(slug, home=None, base=None):
    env = dict(os.environ if base is None else base)
    for name in _AUTH_ENV:
        env.pop(name, None)
    env['HOME'] = str(_home(home))
    if slug != PRINCIPAL_SLUG:
        env['CLAUDE_CONFIG_DIR'] = config_dir(slug, home)
    return env


def _default_runner(argv, env):
    try:
        return subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=4, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def account_status(slug, runner=None, home=None, binary=None):
    result = (runner or _default_runner)([binary or CLAUDE_BIN, 'auth', 'status'],
                                         account_env(slug, home))
    row = {'slug': slug, 'is_principal': slug == PRINCIPAL_SLUG,
           'logged_in': None, 'email': None, 'plan': None, 'org': None,
           'auth_method': None, 'error': 'Stato non leggibile'}
    if result is None:
        return row
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return row
    if not isinstance(data, dict) or not isinstance(data.get('loggedIn'), bool):
        return row
    row.update(logged_in=data['loggedIn'], error=None)
    for source, target in (('email', 'email'), ('orgName', 'org'), ('authMethod', 'auth_method')):
        value = data.get(source)
        row[target] = value[:254] if isinstance(value, str) else None
    plan = data.get('subscriptionType')
    if isinstance(plan, str) and plan.strip():
        row['plan'] = plan[:100].replace('_', ' ').replace('-', ' ').strip().title()
    return row


def prepare_config_dir(slug, home=None):
    if slug == PRINCIPAL_SLUG:
        raise ValueError('Il principale si configura dal CLI')
    target = Path(config_dir(slug, home))
    principal = Path(config_dir(PRINCIPAL_SLUG, home))
    if target.exists() or target.is_symlink():
        raise ValueError('Esiste già un abbonamento con questo nome')
    if principal.is_symlink() or (principal.exists() and not principal.is_dir()):
        raise ValueError('Configurazione principale non utilizzabile')
    # Preflight every shared path before creating the profile. Existing
    # custom directories are preserved; symlinks never widen write access.
    for name in SHARED_ENTRIES:
        source = principal / name
        if source.is_symlink() or (source.exists() and not source.is_dir()):
            raise ValueError('Archivio condiviso non utilizzabile')
    target.mkdir(mode=0o700)
    try:
        principal.mkdir(mode=0o700, exist_ok=True)
        for name in SHARED_ENTRIES:
            source = principal / name
            source.mkdir(mode=0o700, exist_ok=True)
            (target / name).symlink_to(source, target_is_directory=True)
    except OSError:
        shutil.rmtree(target)
        raise
    return {'created': True, 'config_dir': str(target), 'linked': list(SHARED_ENTRIES)}


def ensure_login_dir(slug, email=None, status_reader=None, home=None):
    if slug == PRINCIPAL_SLUG or not valid_slug(slug) or slug in RESERVED_SLUGS:
        raise ValueError('Nome abbonamento non valido o riservato')
    if email is not None and not valid_email(email):
        raise ValueError('Email non valida')
    directory = Path(config_dir(slug, home))
    if directory.is_symlink():
        raise ValueError('La directory abbonamento non può essere un collegamento')
    if directory.exists():
        if not directory.is_dir():
            raise ValueError('Directory abbonamento non utilizzabile')
        status = (status_reader or (lambda value: account_status(value, home=home)))(slug)
        if status.get('error') or not isinstance(status.get('logged_in'), bool):
            raise ValueError('Stato abbonamento non leggibile: operazione annullata')
        if status['logged_in']:
            raise ValueError('Esiste già un abbonamento collegato con questo nome')
        return str(directory), False
    return prepare_config_dir(slug, home)['config_dir'], True
