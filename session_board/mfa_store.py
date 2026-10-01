"""Private, fail-closed MFA storage shared by the full and portable boards.

Only the local operator's ``prepare_enrollment`` creates state. A missing
installation cannot authenticate; incomplete or damaged existing state raises
``MFAStoreError``. Keep the entire directory on a local filesystem and back up
the database, independent encryption key and initialization marker together.
"""

from contextlib import closing, contextmanager
import fcntl
import hashlib
import hmac
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from cryptography.fernet import Fernet, InvalidToken
import pyotp


class MFAStoreError(RuntimeError):
    """The second-factor store cannot safely be used."""


class AlreadyEnrolled(MFAStoreError):
    """Initial setup cannot replace an active second factor."""


class RateLimited(Exception):
    """The account has exhausted its persistent second-factor attempt budget."""

    def __init__(self, retry_after):
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__("Too many second-factor attempts")


class MFAStore:
    """SQLite MFA state in ``path``, a private directory owned by this user."""

    _FILES = ("mfa.initialized", "mfa.key", "mfa.sqlite3")
    _SCHEMA = """
        CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK (id = 1), binding BLOB NOT NULL);
        CREATE TABLE accounts (
            username TEXT PRIMARY KEY,
            bootstrap_hash TEXT,
            bootstrap_expires REAL,
            enrollment_id TEXT,
            pending_secret BLOB,
            pending_expires REAL,
            secret BLOB,
            generation TEXT,
            last_counter INTEGER
        );
        CREATE TABLE recovery (
            username TEXT NOT NULL REFERENCES accounts(username),
            code_hash TEXT NOT NULL,
            PRIMARY KEY (username, code_hash)
        );
        CREATE TABLE failures (username TEXT NOT NULL, attempted_at REAL NOT NULL);
        CREATE INDEX failures_account_time ON failures(username, attempted_at);
        PRAGMA user_version = 1;
    """

    def __init__(self, path, clock=time.time):
        self.path = Path(path).expanduser().absolute()
        self.clock = clock
        self._seen_state = False
        with self._state():
            pass

    @staticmethod
    def _check_private(info, *, directory=False):
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (
            not valid_type
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)
        ):
            raise MFAStoreError("MFA state must use private files owned by the service user")

    @classmethod
    def _open_file(cls, directory, name, *, create=False, exclusive=False):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if create:
            flags |= os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        descriptor = os.open(name, flags, 0o600, dir_fd=directory)
        try:
            cls._check_private(os.fstat(descriptor))
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @classmethod
    def _read_file(cls, directory, name):
        descriptor = cls._open_file(directory, name)
        with os.fdopen(descriptor, "rb") as handle:
            value = handle.read(4097)
        if len(value) > 4096:
            raise MFAStoreError("Invalid MFA state file")
        return value

    @classmethod
    def _write_new(cls, directory, name, value):
        descriptor = cls._open_file(directory, name, create=True, exclusive=True)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _exists(directory, name):
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _initialize(self, directory):
        marker = secrets.token_hex(32).encode("ascii")
        key = Fernet.generate_key()
        # The marker is written first: interrupted initialization remains closed.
        self._write_new(directory, "mfa.initialized", marker)
        self._write_new(directory, "mfa.key", key)
        self._write_new(directory, "mfa.sqlite3", b"")
        with closing(sqlite3.connect(f"/proc/self/fd/{directory}/mfa.sqlite3")) as connection:
            with connection:
                connection.executescript(self._SCHEMA)
                connection.execute(
                    "INSERT INTO metadata (id, binding) VALUES (1, ?)",
                    (Fernet(key).encrypt(b"session-board-mfa-v1:" + marker),),
                )
        os.fsync(directory)

    @contextmanager
    def _state(self, *, create=False):
        """Lock files and transact all validation, comparisons and consumption."""
        directory = lock = None
        connection = None
        try:
            if create and not self._seen_state:
                self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except FileNotFoundError:
                if self._seen_state:
                    raise MFAStoreError("Initialized MFA state is missing") from None
                yield None
                return
            self._check_private(os.fstat(directory), directory=True)
            present = [self._exists(directory, name) for name in self._FILES]
            if not any(present) and not create:
                if self._seen_state or self._exists(directory, "mfa.lock"):
                    raise MFAStoreError("Initialized MFA state is missing")
                yield None
                return
            if any(present) and (not all(present) or not self._exists(directory, "mfa.lock")):
                raise MFAStoreError("Initialized MFA state is incomplete")
            lock = self._open_file(directory, "mfa.lock", create=create)
            fcntl.flock(lock, fcntl.LOCK_EX)
            present = [self._exists(directory, name) for name in self._FILES]
            if not any(present) and create and not self._seen_state:
                self._initialize(directory)
            elif not all(present):
                raise MFAStoreError("Initialized MFA state is incomplete")
            self._seen_state = True
            marker = self._read_file(directory, "mfa.initialized")
            if not re.fullmatch(rb"[a-f0-9]{64}", marker):
                raise MFAStoreError("Invalid MFA initialization marker")
            cipher = Fernet(self._read_file(directory, "mfa.key"))
            # Inspect with O_NOFOLLOW before SQLite opens the file inside the
            # already-open, private directory. Its path cannot redirect us.
            descriptor = self._open_file(directory, "mfa.sqlite3")
            os.close(descriptor)
            connection = sqlite3.connect(
                f"file:/proc/self/fd/{directory}/mfa.sqlite3?mode=rw", uri=True, timeout=10,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MFAStoreError("MFA database integrity check failed")
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise MFAStoreError("Unsupported MFA database version")
            binding = connection.execute("SELECT binding FROM metadata WHERE id = 1").fetchone()
            if binding is None or cipher.decrypt(binding[0]) != b"session-board-mfa-v1:" + marker:
                raise MFAStoreError("MFA encryption key does not match the database")
            # Session reads need every table and active factor to remain valid.
            connection.execute("SELECT username, code_hash FROM recovery LIMIT 0")
            connection.execute("SELECT username, attempted_at FROM failures LIMIT 0")
            for account in connection.execute("SELECT secret, pending_secret, generation, last_counter FROM accounts"):
                active_fields = (account["secret"], account["generation"], account["last_counter"])
                if any(value is not None for value in active_fields) and (
                    account["secret"] is None
                    or not isinstance(account["generation"], str) or not account["generation"]
                    or not isinstance(account["last_counter"], int) or account["last_counter"] < 0
                ):
                    raise MFAStoreError("Incomplete MFA account state")
                for value in (account["secret"], account["pending_secret"]):
                    if value is not None:
                        self._decrypt(cipher, value)
            yield connection, cipher
            connection.commit()
        except (MFAStoreError, RateLimited):
            raise
        except (OSError, sqlite3.Error, InvalidToken, ValueError, TypeError, UnicodeError):
            raise MFAStoreError("MFA state is unavailable or damaged") from None
        finally:
            if connection is not None:
                connection.close()
            if lock is not None:
                os.close(lock)
            if directory is not None:
                os.close(directory)

    @staticmethod
    def _decrypt(cipher, value):
        secret = cipher.decrypt(value).decode("ascii")
        if not re.fullmatch(r"[A-Z2-7]{32,}", secret):
            raise MFAStoreError("Invalid encrypted MFA secret")
        return secret

    @staticmethod
    def _hash(username, value, purpose):
        return hashlib.sha256((purpose + "\0" + username + "\0" + value).encode()).hexdigest()

    @staticmethod
    def _account(connection, username):
        return connection.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()

    def enrolled(self, username):
        with self._state() as state:
            if state is None:
                return False
            account = self._account(state[0], username)
            return account is not None and account["secret"] is not None

    def generation(self, username):
        with self._state() as state:
            if state is None:
                return None
            account = self._account(state[0], username)
            return account["generation"] if account is not None else None

    def prepare_enrollment(self, username):
        """Authorize initial setup locally; never reset an active factor."""
        if not isinstance(username, str) or not username.strip() or len(username) > 256:
            raise ValueError("A nonempty username of at most 256 characters is required")
        with self._state(create=True) as (connection, _):
            account = self._account(connection, username)
            if account is not None and account["secret"] is not None:
                raise AlreadyEnrolled("This account already has a second factor")
            bootstrap = secrets.token_urlsafe(24)
            connection.execute(
                """INSERT INTO accounts (username, bootstrap_hash, bootstrap_expires)
                   VALUES (?, ?, ?) ON CONFLICT(username) DO UPDATE SET
                   bootstrap_hash = excluded.bootstrap_hash,
                   bootstrap_expires = excluded.bootstrap_expires,
                   enrollment_id = NULL, pending_secret = NULL, pending_expires = NULL""",
                (username, self._hash(username, bootstrap, "bootstrap"), self.clock() + 900),
            )
            return bootstrap

    @staticmethod
    def _check_limit(connection, username, now):
        connection.execute("DELETE FROM failures WHERE attempted_at <= ?", (now - 300,))
        failures = connection.execute(
            "SELECT count(*), min(attempted_at) FROM failures WHERE username = ?", (username,),
        ).fetchone()
        if failures[0] >= 5:
            raise RateLimited(failures[1] + 300 - now)

    @staticmethod
    def _failure(connection, username, now):
        connection.execute("INSERT INTO failures VALUES (?, ?)", (username, now))

    def begin_enrollment(self, username, bootstrap):
        with self._state() as state:
            if state is None:
                return None
            connection, cipher = state
            now = self.clock()
            self._check_limit(connection, username, now)
            account = self._account(connection, username)
            if (
                account is None or account["secret"] is not None
                or account["bootstrap_hash"] is None or now >= account["bootstrap_expires"]
                or not isinstance(bootstrap, str) or len(bootstrap) > 256
                or not hmac.compare_digest(account["bootstrap_hash"], self._hash(username, bootstrap, "bootstrap"))
            ):
                self._failure(connection, username, now)
                return None
            enrollment_id = secrets.token_urlsafe(24)
            secret = pyotp.random_base32()
            connection.execute(
                """UPDATE accounts SET bootstrap_hash = NULL, bootstrap_expires = NULL,
                   enrollment_id = ?, pending_secret = ?, pending_expires = ? WHERE username = ?""",
                (enrollment_id, cipher.encrypt(secret.encode("ascii")), now + 300, username),
            )
            return enrollment_id, secret

    @staticmethod
    def _counter(secret, code, now, last_counter=-1):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code.strip()):
            return None
        counter = math.floor(now / 30)
        totp = pyotp.TOTP(secret)
        # Take the greatest match if the truncated codes happen to collide.
        matches = [
            candidate for candidate in range(max(0, counter - 1), counter + 2)
            if candidate > last_counter and hmac.compare_digest(totp.at(candidate * 30), code.strip())
        ]
        return max(matches) if matches else None

    def confirm_enrollment(self, username, enrollment_id, code):
        with self._state() as state:
            if state is None:
                return None
            connection, cipher = state
            now = self.clock()
            self._check_limit(connection, username, now)
            account = self._account(connection, username)
            if (
                account is None or account["secret"] is not None
                or account["enrollment_id"] is None or now >= account["pending_expires"]
                or not isinstance(enrollment_id, str)
                or not hmac.compare_digest(account["enrollment_id"], enrollment_id)
            ):
                self._failure(connection, username, now)
                return None
            counter = self._counter(self._decrypt(cipher, account["pending_secret"]), code, now)
            if counter is None:
                self._failure(connection, username, now)
                return None
            recovery = ["-".join(secrets.token_hex(4) for _ in range(4)) for _ in range(10)]
            connection.execute(
                """UPDATE accounts SET secret = pending_secret, generation = ?, last_counter = ?,
                   enrollment_id = NULL, pending_secret = NULL, pending_expires = NULL WHERE username = ?""",
                (secrets.token_urlsafe(24), counter, username),
            )
            connection.executemany(
                "INSERT INTO recovery (username, code_hash) VALUES (?, ?)",
                [(username, self._hash(username, value, "recovery")) for value in recovery],
            )
            return recovery

    def verify(self, username, code):
        with self._state() as state:
            if state is None:
                return False
            connection, cipher = state
            now = self.clock()
            self._check_limit(connection, username, now)
            account = self._account(connection, username)
            if account is not None and account["secret"] is not None and isinstance(code, str) and len(code) <= 256:
                counter = self._counter(self._decrypt(cipher, account["secret"]), code, now, account["last_counter"])
                if counter is not None:
                    connection.execute("UPDATE accounts SET last_counter = ? WHERE username = ?", (counter, username))
                    return True
                used = connection.execute(
                    "DELETE FROM recovery WHERE username = ? AND code_hash = ?",
                    (username, self._hash(username, code.strip().lower(), "recovery")),
                )
                if used.rowcount == 1:
                    return True
            self._failure(connection, username, now)
            return False
