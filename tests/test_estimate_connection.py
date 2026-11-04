"""The estimate reader is reusable without borrowing interactive capacity."""
import asyncio
from contextlib import AsyncExitStack, closing
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import database
from app.config import settings
from tests.test_connections import _Fresh


class EstimateConnection(_Fresh):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        slots = patch.object(database, '_estimate_slots', asyncio.Semaphore(1))
        slots.start()
        self.addCleanup(slots.stop)

    async def test_reuse_is_read_only_and_observes_commits_without_holding_wal(self):
        with closing(sqlite3.connect(settings.db_path)) as writer, writer:
            writer.execute('CREATE TABLE marker(value)')
            writer.execute('INSERT INTO marker VALUES(1)')
        async with database.estimate_connection() as first:
            self.assertEqual((await (await first.execute('SELECT value FROM marker')).fetchone())[0], 1)
        for value in range(2, 5):
            with closing(sqlite3.connect(settings.db_path, timeout=.1)) as writer, writer:
                writer.execute('UPDATE marker SET value=?', (value,))
            async with database.estimate_connection() as db:
                self.assertIs(db, first, 'Healthy requests must reuse their exclusive reader')
                self.assertFalse(db.in_transaction)
                self.assertEqual((await (await db.execute('SELECT value FROM marker')).fetchone())[0], value)
            with closing(sqlite3.connect(settings.db_path, timeout=.1)) as writer, writer:
                self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0,
                                 'An idle reader must not retain a WAL snapshot')

        async with database.estimate_connection() as db:
            with self.assertRaises(sqlite3.OperationalError, msg='Estimate reader must reject writes'):
                await db.execute('UPDATE marker SET value=99')

    async def test_expected_http_error_reuses_but_unexpected_errors_discard(self):
        with self.assertRaises(HTTPException):
            async with database.estimate_connection() as first:
                raise HTTPException(400)
        async with database.estimate_connection() as again:
            self.assertIs(first, again, 'An HTTP answer must retain a healthy reader')
        with self.assertRaisesRegex(RuntimeError, 'broken'):
            async with database.estimate_connection():
                raise RuntimeError('broken')
        with self.assertRaises(ValueError, msg='Unexpected failure must close the reader'):
            await first.execute('SELECT 1')
        async with database.estimate_connection() as recovered:
            self.assertIsNot(first, recovered)
            self.assertEqual((await (await recovered.execute('SELECT 1')).fetchone())[0], 1)

    async def test_cancelled_sqlite_work_is_discarded_before_the_next_borrow(self):
        entered = threading.Event()
        used = []
        def slow():
            entered.set()
            time.sleep(.03)
            return 1
        async def request():
            async with database.estimate_connection() as db:
                used.append(db)
                await db.create_function('slow_estimate', 0, slow)
                await db.execute('SELECT slow_estimate()')
        task = asyncio.create_task(request())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(database._estimate_reader, 'Cancellation must discard the reader')
        with self.assertRaises(ValueError):
            await used[0].execute('SELECT 1')
        async with database.estimate_connection() as db:
            self.assertIsNot(db, used[0])

    async def test_failed_or_cancelled_setup_closes_partial_connection_and_recovers(self):
        for error in (RuntimeError('setup'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                actual = await database.get_connection()
                self.addAsyncCleanup(actual.close)
                closed = []
                class Partial:
                    async def execute(self, *args):
                        raise error
                    async def close(self):
                        closed.append(True)
                        await actual.close()
                with patch.object(database, 'get_connection', AsyncMock(return_value=Partial())):
                    with self.assertRaises(type(error)):
                        async with database.estimate_connection():
                            self.fail('Failed setup must not yield')
                self.assertTrue(closed, 'Failed setup must close its partial connection')
                self.assertFalse(database._estimate_slots.locked())
                async with database.estimate_connection() as db:
                    self.assertEqual((await (await db.execute('SELECT 1')).fetchone())[0], 1)
                await database.close_pool()

    async def test_path_change_reopens_the_exclusive_reader(self):
        async with database.estimate_connection() as first:
            pass
        original = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            try:
                settings.data_dir = Path(folder)
                await database.init_db()
                async with database.estimate_connection() as second:
                    self.assertIsNot(first, second, 'A new database needs a new reader')
                    with self.assertRaises(ValueError):
                        await first.execute('SELECT 1')
                    actual = (await (await second.execute('PRAGMA database_list')).fetchone())[2]
                    self.assertEqual(actual, str(settings.db_path))
                await database.close_pool()
            finally:
                settings.data_dir = original

    async def test_serialization_pool_independence_and_shutdown_during_a_borrow(self):
        scope = database.estimate_connection()
        first = await scope.__aenter__()
        async def second():
            async with database.estimate_connection() as db:
                self.assertIs(db, first)
        pending = asyncio.create_task(second())
        try:
            for _ in range(20):
                if database._estimate_slots._waiters:
                    break
                await asyncio.sleep(0)
            self.assertFalse(pending.done(), 'Estimate borrowers must be serialized')
            async with AsyncExitStack() as stack:
                for _ in range(database.POOL_SIZE):
                    interactive = await asyncio.wait_for(stack.enter_async_context(database.read_connection()), 2)
                    self.assertIsNot(first, interactive, 'Estimate work must not borrow interactive capacity')
            closing = asyncio.create_task(database.close_pool())
            await asyncio.sleep(0)
            self.assertFalse(closing.done(), 'Shutdown must wait for the active estimate borrower')
        finally:
            await scope.__aexit__(None, None, None)
        await pending
        await closing
        with self.assertRaises(ValueError, msg='Shutdown must close the exclusive reader'):
            await first.execute('SELECT 1')

    async def test_explicit_transaction_is_not_handed_to_the_next_request(self):
        async with database.estimate_connection() as first:
            await first.execute('BEGIN')
            self.assertTrue(first.in_transaction)
        with self.assertRaises(ValueError, msg='An unfinished transaction must not be reused'):
            await first.execute('SELECT 1')
        async with database.estimate_connection() as second:
            self.assertIsNot(first, second)
            self.assertFalse(second.in_transaction)
