"""The reusable cache reader must detect external changes and release its lease."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.stats import estimate_snapshots as snapshots
from app.stats.snapshot_reader import SnapshotReader


class SnapshotReads(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        old = snapshots.settings.data_dir
        snapshots.close_reader()
        self.addCleanup(setattr, snapshots.settings, 'data_dir', old)
        snapshots.settings.data_dir = self.folder
        self.path = self.folder / 'cache/reading-estimates.sqlite3'
        self.reader = SnapshotReader(lambda: self.path, snapshots._initialize)
        self.addCleanup(self.reader.close)
        self.addCleanup(snapshots.close_reader)

    def write(self, sql):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(sql)

    def test_fresh_commits_exclusive_reuse_and_bounded_lock(self):
        before = self.reader.read()
        connection = self.reader.db
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: self.reader.read(), range(32)))
        self.assertEqual(values, [before] * 32)
        self.assertIs(self.reader.db, connection, 'reader opened a new connection per access')
        self.write("INSERT INTO snapshots(scope,signature,payload) VALUES('s','f','old')")
        after = self.reader.read()
        self.assertNotEqual(before, after, 'external commit was not observed')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE snapshots SET payload='rolled back'")
            db.rollback()
        self.assertEqual(after, self.reader.read())
        self.assertFalse(connection.in_transaction, 'reader retained a transaction')
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute('DELETE FROM snapshots')
        self.reader.lock.acquire()
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'busy'):
                self.reader.read()
        finally:
            self.reader.lock.release()
        self.reader.close()
        self.assertIsNone(self.reader.db)
        self.reader.read()
        self.assertIsNot(self.reader.db, connection)

    def test_schema_repair_and_generation_index_share_a_transaction(self):
        before = self.reader.read()
        self.write('DROP TABLE snapshots')
        self.assertNotEqual(before, self.reader.read(), 'dropped cache was accepted')
        before = self.reader.read()
        self.write('DROP TRIGGER snapshot_version_INSERT')
        self.assertNotEqual(before, self.reader.read(), 'missing trigger was not repaired')
        self.write('DELETE FROM snapshot_revision')
        before = self.reader.read()
        self.assertIsNotNone(before)
        # Recreating a table can preserve the persisted epoch/revision. Schema
        # identity must still change the local validation token.
        self.write('DROP TABLE snapshots')
        self.write('CREATE TABLE snapshots(scope TEXT PRIMARY KEY,signature TEXT,payload TEXT,touched INTEGER)')
        self.assertNotEqual(before, self.reader.read(), 'recreated table reused generation')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
        self.reader.read()
        self.write("INSERT INTO snapshots(scope,signature,payload) VALUES('s','f','old')")
        original = self.reader._one
        changed = False

        def one(db, sql):
            nonlocal changed
            value = original(db, sql)
            if sql.startswith('SELECT epoch') and not changed:
                changed = True
                self.write("UPDATE snapshots SET payload='new'")
            return value

        with patch.object(self.reader, '_one', side_effect=one):
            token, rows = self.reader.read(index=True)
        self.assertEqual(rows['s'], ('f', 'old'), 'mixed snapshot generation and rows')
        self.assertNotEqual(token, self.reader.read())
        self.assertEqual(self.reader.read(index=True)[1]['s'], ('f', 'new'))

    def test_file_replacement_removal_corruption_and_directory_change(self):
        before = self.reader.read()
        replacement = self.path.with_suffix('.replacement')
        shutil.copyfile(self.path, replacement)
        original = self.reader._identity
        calls = 0

        def identity():
            nonlocal calls
            calls += 1
            if calls == 2:
                os.replace(replacement, self.path)
            return original()

        with patch.object(self.reader, '_identity', side_effect=identity):
            after = self.reader.read()
        self.assertNotEqual(before, after, 'replacement during read was not detected')
        self.path.unlink()
        self.assertNotEqual(after, self.reader.read(), 'removed cache retained old generation')
        valid = self.path.with_suffix('.valid')
        shutil.copyfile(self.path, valid)
        self.path.write_bytes(b'corrupt')
        with self.assertRaises(sqlite3.DatabaseError):
            self.reader.read()
        self.assertIsNone(self.reader.db, 'failed connection was retained')
        os.replace(valid, self.path)
        self.assertIsNotNone(self.reader.read())
        connection = self.reader.db
        second = self.folder / 'second'
        snapshots.settings.data_dir = second
        self.path = second / 'cache/reading-estimates.sqlite3'
        self.reader.read()
        self.assertIsNot(self.reader.db, connection, 'data directory change reused old connection')

    def test_cancelled_borrower_cannot_release_active_worker(self):
        async def scenario():
            before = await asyncio.to_thread(self.reader.read)
            original = self.reader._one
            entered, release = threading.Event(), threading.Event()
            armed = True

            def one(db, sql):
                nonlocal armed
                value = original(db, sql)
                if armed and sql.startswith('SELECT epoch'):
                    armed = False
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError('worker release timed out')
                return value

            with patch.object(self.reader, '_one', side_effect=one):
                task = asyncio.create_task(asyncio.to_thread(self.reader.read))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    acquired = self.reader.lock.acquire(blocking=False)
                    if acquired:
                        self.reader.lock.release()
                    self.assertFalse(acquired, 'cancelled caller released the active worker lock')
                    next_read = asyncio.create_task(asyncio.to_thread(self.reader.read))
                finally:
                    release.set()
                self.assertEqual(await asyncio.wait_for(next_read, 3), before)
            self.assertFalse(self.reader.db.in_transaction)
            await asyncio.to_thread(self.reader.close)
            self.assertIsNone(self.reader.db, 'shutdown retained the cache reader')

        asyncio.run(scenario())
