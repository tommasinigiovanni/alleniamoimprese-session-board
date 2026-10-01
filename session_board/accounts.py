"""Portable account orchestration, without the vm3 database or launchers."""
import copy
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from . import claude_accounts as ca
from . import login_pty


class AccountError(ValueError):
    def __init__(self, message, http_status=400):
        super().__init__(message)
        self.http_status = http_status


class AccountService:
    STATUS_TTL = 30.0
    STATUS_TIMEOUT = 4.0
    LIST_WAIT = 0.1

    def __init__(self, home, claude_bin, *, usage_loader=None, active_account_checker=None):
        self.home = Path(home).expanduser().absolute()
        self.claude_bin = str(claude_bin)
        self.usage_loader = usage_loader
        self._active_account_checker = active_account_checker
        self._lock = threading.RLock()
        self._mutations = threading.RLock()
        self.mutation_lock = self._mutations
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='board-accounts')
        self._usage_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='board-usage')
        self._cache = {}
        self._futures = {}
        self._usage_cache = {}
        self._usage_futures = {}
        self._generation = {}
        self._closed = False
        self._login = login_pty.LoginManager(
            preparer=self._prepare_login, status_reader=self._read_status,
            spawner=lambda slug, directory, email: login_pty._spawn_login_pty(
                slug, directory, email, binary=self.claude_bin,
                environment=ca.account_env(slug, self.home)),
        )

    def set_active_account_checker(self, checker):
        self._active_account_checker = checker

    def _available(self):
        return shutil.which(self.claude_bin) is not None

    def _require_binary(self):
        if not self._available():
            raise AccountError('Claude CLI non disponibile su questa macchina', 503)

    def path_for(self, slug):
        try:
            directory = Path(ca.config_dir(slug, self.home))
        except ValueError as error:
            raise AccountError(str(error)) from error
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise AccountError('Directory abbonamento non utilizzabile')
        return directory

    def _known(self, slug):
        directory = self.path_for(slug)
        if slug != ca.PRINCIPAL_SLUG and not directory.is_dir():
            raise AccountError('Abbonamento sconosciuto', 404)
        return directory

    def _run(self, argv, env):
        try:
            return subprocess.run(argv, env=env, cwd=self.home, capture_output=True,
                                  text=True, timeout=self.STATUS_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def _unknown(slug, message='Verifica in corso'):
        return {'slug': slug, 'is_principal': slug == ca.PRINCIPAL_SLUG,
                'logged_in': None, 'email': None, 'plan': None, 'org': None,
                'auth_method': None, 'error': message}

    def _read_status(self, slug):
        try:
            self._known(slug)
            row = ca.account_status(slug, runner=self._run, home=self.home,
                                    binary=self.claude_bin)
        except (OSError, ValueError):
            row = self._unknown(slug, 'Stato non leggibile')
        row['checked_at'] = time.time()
        return row

    def _refresh(self, slug, force=False):
        with self._lock:
            cached = self._cache.get(slug)
            if not force and cached and time.monotonic() - cached[0] < self.STATUS_TTL:
                return None
            previous = self._futures.get(slug)
            if previous is not None and not previous.done():
                return previous
            if self._closed:
                return None
            generation = self._generation.get(slug, 0)
            future = self._executor.submit(self._read_status, slug)
            self._futures[slug] = future

            def completed(value):
                try:
                    row = value.result()
                except Exception:
                    row = self._unknown(slug, 'Stato non leggibile')
                with self._lock:
                    if self._generation.get(slug, 0) == generation:
                        self._cache[slug] = (time.monotonic(), row)
                    if self._futures.get(slug) is value:
                        self._futures.pop(slug, None)
            future.add_done_callback(completed)
            return future

    def _invalidate(self, slug, *, usage=True):
        with self._lock:
            self._cache.pop(slug, None)
            if usage:
                self._usage_cache.pop(slug, None)
            self._generation[slug] = self._generation.get(slug, 0) + 1
            previous = self._futures.pop(slug, None)
            if previous is not None:
                previous.cancel()
            if usage:
                previous_usage = self._usage_futures.pop(slug, None)
                if previous_usage is not None:
                    previous_usage.cancel()

    def _refresh_usage(self, slug):
        if self.usage_loader is None:
            return
        with self._lock:
            cached = self._usage_cache.get(slug)
            if cached and time.monotonic() - cached[0] < self.STATUS_TTL:
                return
            previous = self._usage_futures.get(slug)
            if previous is not None and not previous.done():
                return
            if self._closed:
                return
            generation = self._generation.get(slug, 0)
            future = self._usage_executor.submit(self.usage_loader, slug)
            self._usage_futures[slug] = future

            def completed(value):
                try:
                    result = value.result()
                    if not isinstance(result, dict):
                        raise ValueError('Consumi non leggibili')
                except Exception:
                    result = {'stale': True, 'message': 'Consumi non leggibili'}
                with self._lock:
                    if self._generation.get(slug, 0) == generation:
                        self._usage_cache[slug] = (time.monotonic(), result)
                    if self._usage_futures.get(slug) is value:
                        self._usage_futures.pop(slug, None)
            future.add_done_callback(completed)

    def _with_usage(self, row):
        row = copy.deepcopy(row)
        row.update(limits=[], scoped_limits=[], sampled_at=None,
                   usage_stale=True, usage_error=None)
        if self.usage_loader is not None:
            self._refresh_usage(row['slug'])
            try:
                with self._lock:
                    cached = self._usage_cache.get(row['slug'])
                    usage = copy.deepcopy(cached[1]) if cached else None
                if isinstance(usage, dict):
                    for key in ('limits', 'scoped_limits', 'sampled_at', 'usage_stale', 'usage_error'):
                        if key in usage:
                            row[key] = copy.deepcopy(usage[key])
                    if 'stale' in usage:
                        row['usage_stale'] = bool(usage['stale'])
                    if 'message' in usage:
                        row['usage_error'] = usage['message'] if isinstance(usage['message'], str) else None
            except Exception:
                row['usage_error'] = 'Consumi non leggibili'
        return row

    def list_accounts(self, force=False):
        available = self._available()
        try:
            slugs = ca.list_slugs(self.home, strict=True)
            discovery_error = None
        except OSError:
            slugs = [ca.PRINCIPAL_SLUG]
            discovery_error = 'Elenco abbonamenti non leggibile'
        pending = []
        if available and not discovery_error:
            for slug in slugs:
                future = self._refresh(slug, force=force)
                if future is not None:
                    pending.append(future)
            if pending:
                wait(pending, timeout=self.LIST_WAIT)
        with self._lock:
            rows = []
            for slug in slugs:
                cached = self._cache.get(slug)
                row = cached[1] if cached and available and not discovery_error else self._unknown(
                    slug, discovery_error or ('Verifica in corso' if available else 'Claude CLI non disponibile'))
                rows.append(copy.deepcopy(row))
            refreshing = any(not f.done() for f in self._futures.values())
        by_email = {}
        for row in rows:
            email = (row.get('email') or '').strip().lower()
            row['duplicate_of'] = by_email.get(email) if email else None
            if email:
                by_email.setdefault(email, row['slug'])
        return {'accounts': [self._with_usage(row) for row in rows],
                'checked_at': time.time(), 'available': available,
                'refreshing': refreshing, 'error': discovery_error}

    def require_account(self, slug):
        # A switch can replace a healthy process. Its authentication check must
        # start now, rather than reuse a cached or already-running observation
        # that predates an external CLI logout or credential expiry.
        with self._mutations:
            self._known(slug)
            self._require_binary()
            with self._lock:
                cached = self._cache.get(slug)
                previous = cached[1] if cached else None
            self._invalidate(slug, usage=False)
            row = self._read_status(slug)
            with self._lock:
                identity_keys = ('logged_in', 'email', 'plan', 'auth_method')
                if previous and any(previous.get(key) != row.get(key) for key in identity_keys):
                    self._usage_cache.pop(slug, None)
                    pending_usage = self._usage_futures.pop(slug, None)
                    if pending_usage is not None:
                        pending_usage.cancel()
                self._cache[slug] = (time.monotonic(), copy.deepcopy(row))
            if row.get('error') or not isinstance(row.get('logged_in'), bool):
                raise AccountError('Stato abbonamento non leggibile', 503)
            return self._with_usage(row)

    def _validate_login(self, slug, email):
        if slug == ca.PRINCIPAL_SLUG:
            raise AccountError('Il principale si configura dal CLI')
        self.path_for(slug)
        if email is not None and not ca.valid_email(email):
            raise AccountError('Email non valida')
        self._require_binary()

    def _prepare_login(self, slug, email=None):
        try:
            return ca.ensure_login_dir(slug, email=email, home=self.home,
                                       status_reader=self._read_status)
        except OSError as error:
            raise AccountError('Impossibile preparare la directory abbonamento', 503) from error
        except ValueError as error:
            raise AccountError(str(error)) from error

    def create(self, slug, email=None):
        return self.start_login(slug, email)

    def start_login(self, slug, email=None):
        with self._mutations:
            self._validate_login(slug, email)
            directory = self.path_for(slug)
            if directory.exists():
                status = self._read_status(slug)
                if status.get('error') or not isinstance(status.get('logged_in'), bool):
                    raise AccountError('Stato abbonamento non leggibile', 503)
                if status['logged_in']:
                    raise AccountError('Questo abbonamento è già collegato', 409)
            try:
                result = self._login.start(slug, email)
            except ValueError as error:
                raise AccountError(str(error), getattr(error, 'http_status', 400)) from error
            except OSError as error:
                raise AccountError('Impossibile avviare il login', 503) from error
            finally:
                self._invalidate(slug)
            return result

    def poll_login(self, slug):
        self.path_for(slug)
        with self._mutations:
            result = self._login_call('poll', slug)
            if result.get('state') == 'ok':
                self._invalidate(slug)
            return result

    def submit_code(self, slug, code):
        self.path_for(slug)
        with self._mutations:
            result = self._login_call('submit_code', slug, code)
            self._invalidate(slug)
            return result

    def cancel_login(self, slug):
        self.path_for(slug)
        with self._mutations:
            result = self._login_call('cancel', slug)
            self._invalidate(slug)
            return result

    def _login_call(self, method, *args):
        try:
            return getattr(self._login, method)(*args)
        except ValueError as error:
            raise AccountError(str(error), getattr(error, 'http_status', 400)) from error
        except OSError as error:
            raise AccountError('Operazione di login non riuscita', 503) from error

    def _guard_removable(self, slug):
        if slug == ca.PRINCIPAL_SLUG:
            raise AccountError('Il principale non si disconnette e non si rimuove dalla board')
        directory = self._known(slug)
        if self._active_account_checker is None:
            raise AccountError('Verifica delle sessioni non disponibile', 503)
        try:
            active = self._active_account_checker(slug)
        except Exception as error:
            raise AccountError('Sessioni non verificabili: operazione annullata', 503) from error
        if not isinstance(active, (list, tuple, set)):
            raise AccountError('Sessioni non verificabili: operazione annullata', 503)
        if active:
            raise AccountError('Abbonamento in uso dalle sessioni: ' + ', '.join(sorted(map(str, active))), 409)
        return directory

    def _cancel_pending(self, slug):
        try:
            self._login.cancel(slug)
        except login_pty.NoPendingLogin:
            pass

    def logout(self, slug):
        with self._mutations:
            self._guard_removable(slug)
            self._require_binary()
            self._cancel_pending(slug)
            result = self._run([self.claude_bin, 'auth', 'logout'], ca.account_env(slug, self.home))
            self._invalidate(slug)
            if result is None or result.returncode != 0:
                raise AccountError('Disconnessione non riuscita', 503)
            return {'ok': True}

    def remove(self, slug):
        with self._mutations:
            directory = self._guard_removable(slug)
            self._cancel_pending(slug)
            self.path_for(slug)
            try:
                shutil.rmtree(directory)
            except OSError as error:
                raise AccountError('Rimozione non riuscita', 503) from error
            self._invalidate(slug)
            return {'ok': True}

    def close(self):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._usage_executor.shutdown(wait=True, cancel_futures=True)
        for slug in self._login.active_slugs():
            try:
                self._login.cancel(slug)
            except ValueError:
                pass
