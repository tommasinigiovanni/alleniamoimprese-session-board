"""A small independent store for explicitly reported session status."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class Store:
    def __init__(self, state_dir):
        directory = Path(state_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / 'board.sqlite3'
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS status (session TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL, updated_at REAL NOT NULL)')
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        return db

    def record(self, name, status, detail):
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM status WHERE updated_at < ?', (now - 7 * 86400,))
            db.execute('INSERT INTO status VALUES (?,?,?,?) ON CONFLICT(session) DO UPDATE SET status=excluded.status,detail=excluded.detail,updated_at=excluded.updated_at', (name, status, detail, now))

    def latest(self):
        with closing(self.connect()) as db:
            return {r['session']: dict(r) for r in db.execute('SELECT * FROM status WHERE updated_at >= ?', (time.time() - 7 * 86400,))}
