"""Serialized read-only access to the disposable estimate snapshot database."""
from contextlib import closing
import sqlite3
import threading

class _RepairRequired(Exception):
    pass

class _ReplacedDuringRead(Exception):
    pass

class SnapshotReader:
    def __init__(self, path, repair, limit=4096):
        self.path = path
        self.repair = repair
        self.limit = limit
        self.lock = threading.Lock()
        self.db = None
        self.identity = None
        self.schema = None

    def _identity(self):
        path = self.path()
        st = path.stat()
        return path, st.st_dev, st.st_ino

    def _close(self):
        if self.db is not None:
            self.db.close()
        self.db = self.identity = self.schema = None

    def close(self):
        if not self.lock.acquire(timeout=2):
            raise sqlite3.OperationalError('Snapshot reader still borrowed during close')
        try:
            self._close()
        finally:
            self.lock.release()

    @staticmethod
    def _one(db, sql):
        with closing(db.execute(sql)) as cursor:
            return cursor.fetchone()

    def _read(self, index):
        identity = self._identity()
        if self.db is None or self.identity != identity:
            self._close()
            self.db = sqlite3.connect(identity[0].resolve().as_uri()+'?mode=ro',uri=True,
                                      timeout=1,check_same_thread=False,isolation_level=None)
            self.db.execute('PRAGMA query_only=ON').close()
            self.identity = identity
        db = self.db
        try:
            db.execute('BEGIN').close()
            schema = self._one(db,'PRAGMA schema_version')[0]
            if schema != self.schema:
                wanted = {('table','snapshots'),('table','snapshot_revision')}
                wanted.update(('trigger','snapshot_version_'+op) for op in ('INSERT','UPDATE','DELETE'))
                with closing(db.execute("SELECT type,name FROM sqlite_master WHERE name IN ('snapshots','snapshot_revision','snapshot_version_INSERT','snapshot_version_UPDATE','snapshot_version_DELETE')")) as cursor:
                    actual = set(cursor.fetchall())
                if actual != wanted:
                    raise _RepairRequired()
                self.schema = schema
            revision = self._one(db,'SELECT epoch,revision FROM snapshot_revision WHERE id=1')
            if revision is None:
                raise _RepairRequired()
            # The local token includes file/schema identity. An external table
            # replacement can leave the persisted epoch+revision unchanged.
            token = (f'{revision[0]}:{identity[1]}:{identity[2]}:{schema}',revision[1])
            rows = None
            if index:
                with closing(db.execute('SELECT scope,signature,payload FROM snapshots WHERE length(payload)<=512 LIMIT ?', (self.limit,))) as cursor:
                    rows = {key:(fingerprint,payload) for key,fingerprint,payload in cursor}
        finally:
            if db.in_transaction:
                db.rollback()
        if self._identity() != identity:
            raise _ReplacedDuringRead()
        return (token,rows) if index else token

    def read(self, index=False):
        if not self.lock.acquire(timeout=1):
            raise sqlite3.OperationalError('Snapshot reader busy')
        try:
            for _ in range(3):
                try:
                    return self._read(index)
                except (_RepairRequired, FileNotFoundError):
                    self._close()
                    self.repair()  # Native writer initializes/repairs; never delete corrupt data.
                except _ReplacedDuringRead:
                    self._close()
                except (OSError,sqlite3.Error):
                    self._close()
                    raise
            raise sqlite3.OperationalError('Snapshot database changed repeatedly during read')
        finally:
            self.lock.release()
