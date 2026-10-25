"""Disposable, account-scoped ETA snapshots; never source text or reading history.

A separate bounded SQLite cache keeps writes off the application's reader pool
and writer transaction. Signatures invalidate content/position/rule changes;
word counts are multiplied by current pace without reopening source inventories.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
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


@dataclass(frozen=True)
class WordCounts:
    remaining: int
    total: int


def read():
    """One bounded read per estimator, shared by its item and novel scopes.

    Only hashed scopes are indexed. Callers still match the complete account
    identity and source fingerprint; loading rows never grants another scope.
    """
    try:
        with _connection() as db:
            return {key: (fingerprint, payload) for key, fingerprint, payload in db.execute(
                'SELECT scope,signature,payload FROM snapshots WHERE length(payload)<=512 LIMIT ?',
                (MAX_ENTRIES,))}
    except (OSError, sqlite3.Error):
        return {}


def load(scope, fingerprint, *, rows=None):
    from app.stats.text_inventory import _CACHE_VERSION
    try:
        if rows is None:
            rows = read()
        saved = rows.get(signature(scope))
        if saved is None or saved[0] != fingerprint:
            return None
        value = json.loads(saved[1])
        if (isinstance(value, dict) and value.get('format') == 2
                and value.get('inventory_version') == _CACHE_VERSION):
            words = value.get('words')
            if (isinstance(words, list) and len(words) == 2
                    and all(type(number) is int for number in words)
                    and 0 <= words[0] <= words[1] <= 2**63-1):
                return WordCounts(*words)
        # Old duration snapshots remain useful to HTML until the endpoint
        # upgrades this scope once. They cannot be treated as source counts.
        if (isinstance(value, list) and len(value) == 3
                and all(type(number) in (int, float) and math.isfinite(number) and number >= 0 for number in value)):
            return value
        return None
    except (ValueError, OverflowError):
        return None


def save(scope, fingerprint, payload):
    from app.stats.text_inventory import _CACHE_VERSION
    payload = {'format': 2, 'inventory_version': _CACHE_VERSION,
               'words': [payload.remaining, payload.total]}
    try:
        with _connection() as db:
            db.execute('INSERT OR REPLACE INTO snapshots(scope,signature,payload) VALUES(?,?,?)',
                       (signature(scope), fingerprint, json.dumps(payload)))
            db.execute('''DELETE FROM snapshots WHERE scope IN
                (SELECT scope FROM snapshots ORDER BY touched DESC, rowid DESC LIMIT -1 OFFSET ?)''',
                       (MAX_ENTRIES,))
    except (OSError, sqlite3.Error):
        log.info('estimate_snapshot scope=%s state=cache_unavailable', signature(scope)[:12])
