"""Disposable, account-scoped ETA snapshots; never source text or reading history.

A separate bounded SQLite cache keeps writes off the application's reader pool
and writer transaction. Signatures invalidate content/position/rule changes;
pace changes revalidate the displayed last result at the isolated endpoint.
"""
from contextlib import closing, contextmanager
import hashlib
import json
import logging
import math
import sqlite3

from app.config import settings

log = logging.getLogger(__name__)
MAX_ENTRIES = 4096


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@contextmanager
def _connection():
    path = settings.data_dir / 'cache' / 'reading-estimates.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
            scope TEXT PRIMARY KEY, signature TEXT NOT NULL, payload TEXT NOT NULL,
            touched INTEGER NOT NULL DEFAULT (unixepoch()))''')
        yield db


def load(scope, fingerprint):
    try:
        with _connection() as db:
            row = db.execute('SELECT payload FROM snapshots WHERE scope=? AND signature=?',
                             (signature(scope), fingerprint)).fetchone()
        value = json.loads(row[0]) if row else None
        if (isinstance(value, list) and len(value) == 3
                and all(type(number) in (int, float) and math.isfinite(number) and number >= 0 for number in value)):
            return value
        return None
    except (OSError, sqlite3.Error, ValueError, OverflowError):
        return None


def save(scope, fingerprint, payload):
    try:
        with _connection() as db:
            db.execute('INSERT OR REPLACE INTO snapshots(scope,signature,payload) VALUES(?,?,?)',
                       (signature(scope), fingerprint, json.dumps(payload)))
            db.execute('''DELETE FROM snapshots WHERE scope IN
                (SELECT scope FROM snapshots ORDER BY touched DESC, rowid DESC LIMIT -1 OFFSET ?)''',
                       (MAX_ENTRIES,))
    except (OSError, sqlite3.Error):
        log.info('estimate_snapshot scope=%s state=cache_unavailable', signature(scope)[:12])
