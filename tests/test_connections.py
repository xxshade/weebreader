"""How the app holds its SQLite connections.

* A stream does not keep a pooled connection. FastAPI closes a yield
  dependency only after the response is sent, and /library/events is sent
  for as long as a tab is open: with the session checked through get_db,
  four open tabs held the four pooled connections and every other request
  waited forever (measured against the real server -- three streams, the
  home page in 10 ms; four, no answer at all).
* An HTTP answer is not a broken connection. A 401 or a 404 used to close
  the connection it was raised over (measured: ten 401s, ten new
  connections).
* Writes share one connection behind the write lock, instead of opening
  and closing one per write -- which the readers do on every page turn.
* Columns added after a table shipped are added when missing, not
  attempted and caught on every start.
"""
import tempfile
import unittest
from pathlib import Path

import aiosqlite
from fastapi import HTTPException

from app import database
from app.config import settings
from app.database import close_pool, get_connection, get_db, init_db, write_transaction


class _Fresh(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        await init_db()


class AStreamHoldsNoPooledConnection(unittest.TestCase):
    def _calls(self, dependant):
        for dependency in dependant.dependencies:
            yield dependency.call
            yield from self._calls(dependency)

    def _route(self, router, path):
        return next(r for r in router.routes if getattr(r, "path", None) == path)

    def test_the_event_stream_never_reaches_get_db(self):
        from app.library.router import router

        route = self._route(router, "/library/events")
        calls = list(self._calls(route.dependant))
        self.assertTrue(calls, "the stream is authenticated through something")
        self.assertNotIn(get_db, calls)

    def test_an_ordinary_page_still_does(self):
        # The control: the walk above can see get_db where it IS used.
        from app.library.router import router

        route = self._route(router, "/library/job-status")
        self.assertIn(get_db, list(self._calls(route.dependant)))


class TheStreamStillChecksTheSession(_Fresh):
    async def test_no_cookie_is_refused(self):
        from app.auth.dependencies import get_streaming_user

        with self.assertRaises(HTTPException) as caught:
            await get_streaming_user(session=None)
        self.assertEqual(caught.exception.status_code, 401)

    async def test_the_connection_it_borrowed_is_back_in_the_pool(self):
        from app.auth.dependencies import get_streaming_user

        with self.assertRaises(HTTPException):
            await get_streaming_user(session="not-a-session")
        pool = await database._pool_queue()
        self.assertEqual(pool.qsize(), database.POOL_SIZE)


class AnHttpAnswerKeepsItsConnection(_Fresh):
    async def _raise_through(self, exc):
        agen = get_db()
        db = await agen.__anext__()
        with self.assertRaises(type(exc)):
            await agen.athrow(exc)
        return db

    async def test_a_401_hands_the_same_connection_back(self):
        first = await self._raise_through(HTTPException(401))
        agen = get_db()
        second = await agen.__anext__()
        await agen.aclose()
        self.assertIs(first, second)

    async def test_a_real_failure_still_throws_it_away(self):
        first = await self._raise_through(RuntimeError("boom"))
        agen = get_db()
        second = await agen.__anext__()
        await agen.aclose()
        self.assertIsNot(first, second)


class ThePoolFollowsTheDatabase(_Fresh):
    async def test_a_new_database_gets_new_read_connections(self):
        agen = get_db()
        first = await agen.__anext__()
        with self.assertRaises(StopAsyncIteration):  # handed back, as FastAPI does
            await agen.__anext__()
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        settings.data_dir = Path(other.name)
        await init_db()
        agen = get_db()
        second = await agen.__anext__()
        try:
            self.assertIsNot(first, second)
            files = [row[2] for row in await (await second.execute("PRAGMA database_list")).fetchall()]
            self.assertEqual(Path(files[0]), Path(other.name) / "library.db")
        finally:
            await agen.aclose()


class WritesShareOneConnection(_Fresh):
    async def test_the_next_write_gets_the_same_one(self):
        async with write_transaction() as first:
            await first.commit()
        async with write_transaction() as second:
            await second.commit()
        self.assertIs(first, second)

    async def test_a_write_left_uncommitted_is_not_committed_by_the_next(self):
        async with write_transaction() as db:
            await db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'a', 'h')")
        async with write_transaction() as db:
            await db.execute("INSERT INTO users (id, username, password_hash) VALUES (2, 'b', 'h')")
            await db.commit()
        reader = await get_connection()
        try:
            ids = [r["id"] for r in await (await reader.execute("SELECT id FROM users")).fetchall()]
        finally:
            await reader.close()
        self.assertEqual(ids, [2])

    async def test_a_failed_write_is_rolled_back_and_its_connection_dropped(self):
        with self.assertRaises(RuntimeError):
            async with write_transaction() as broken:
                await broken.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'a', 'h')")
                raise RuntimeError("boom")
        async with write_transaction() as fresh:
            count = (await (await fresh.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        self.assertIsNot(broken, fresh)
        self.assertEqual(count, 0)

    async def test_a_new_database_gets_a_new_connection(self):
        async with write_transaction() as first:
            pass
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        settings.data_dir = Path(other.name)
        await init_db()
        async with write_transaction() as second:
            await second.execute("INSERT INTO users (id, username, password_hash) VALUES (5, 'c', 'h')")
            await second.commit()
        self.assertIsNot(first, second)
        async with aiosqlite.connect(Path(other.name) / "library.db") as check:
            self.assertEqual((await (await check.execute("SELECT COUNT(*) FROM users")).fetchone())[0], 1)

    async def test_its_worker_thread_cannot_keep_the_process_alive(self):
        async with write_transaction() as db:
            pass
        self.assertTrue(db._thread.daemon)


class ColumnsAddedLater(_Fresh):
    async def test_an_old_table_gets_what_it_lacks(self):
        async with aiosqlite.connect(settings.db_path) as db:
            await db.execute("ALTER TABLE users DROP COLUMN cover_blur_px")
            await db.commit()
        await init_db()
        async with aiosqlite.connect(settings.db_path) as db:
            columns = {row[1] for row in await (await db.execute("PRAGMA table_info(users)")).fetchall()}
        self.assertIn("cover_blur_px", columns)

    async def test_every_listed_column_is_also_in_the_schema(self):
        # A column added here and not to schema.sql would exist on an
        # upgraded database and be missing from a new one until the next
        # start -- or the other way round.
        schema = (Path(database.__file__).parent / "schema.sql").read_text()
        for table, column, _ in database.ADDED_COLUMNS:
            with self.subTest(column=f"{table}.{column}"):
                block = schema.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].split("\n);", 1)[0]
                self.assertRegex(block, rf"\n\s+{column}\s")

    async def test_starting_twice_changes_nothing(self):
        await init_db()
        await init_db()


if __name__ == "__main__":
    unittest.main()


class FailedConnectionRecovery(_Fresh):
    async def test_failed_open_and_pragmas_do_not_prevent_a_later_borrow(self):
        import asyncio
        import sqlite3

        async def borrow():
            async with database.read_connection() as db:
                return (await (await db.execute('SELECT 1')).fetchone())[0]

        original = settings.data_dir
        try:
            for mode in ('missing-parent', 'invalid-database'):
                with self.subTest(mode=mode):
                    await close_pool()
                    settings.data_dir = original / mode
                    if mode == 'invalid-database':
                        settings.data_dir.mkdir()
                        settings.db_path.write_bytes(b'not a SQLite database')
                    for _ in range(database.POOL_SIZE + 1):
                        with self.assertRaises(sqlite3.Error):
                            await asyncio.wait_for(borrow(), timeout=1)
                    settings.data_dir.mkdir(exist_ok=True)
                    settings.db_path.unlink(missing_ok=True)
                    self.assertEqual(await asyncio.wait_for(borrow(), timeout=1), 1)
        finally:
            settings.data_dir = original
