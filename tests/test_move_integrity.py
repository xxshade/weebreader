"""Failed and concurrent imports cannot discard an item's reading state."""
import asyncio
import os
import time
from unittest.mock import patch

from watchdog.events import FileMovedEvent, DirMovedEvent

from app.config import settings
from app.library import scanner, watcher
from tests.test_library_moves import Case, write_cbz


class MoveIntegrity(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Each IsolatedAsyncioTestCase has its own event loop. A lock
        # contended in another test belongs to that test's loop.
        from app import database
        lock = patch.object(database, '_write_lock', asyncio.Lock())
        lock.start()
        self.addCleanup(lock.stop)

    async def seed(self):
        src = self.root / 'Old' / 'Book 1.cbz'
        write_cbz(src)
        await scanner.process_file(1, str(self.root), src)
        item = (await self.rows('SELECT id FROM items'))[0]['id']
        await self.execute("INSERT INTO reading_progress(user_id,item_id,status,progress_percent) "
                           "VALUES(1,?,'reading',50)", (item,))
        return src, item

    async def deliver(self, event):
        original_delay = settings.watcher_debounce_seconds
        settings.watcher_debounce_seconds = .01
        self.addCleanup(lambda: setattr(settings, 'watcher_debounce_seconds', original_delay))
        submitted = []
        submit = asyncio.run_coroutine_threadsafe

        def capture(coro, loop):
            future = submit(coro, loop)
            submitted.append(future)
            return future

        handler = watcher._DebouncedHandler(1, str(self.root), asyncio.get_running_loop())
        with patch.object(asyncio, 'run_coroutine_threadsafe', capture):
            handler.on_moved(event)
            async with asyncio.timeout(5):
                while not submitted:
                    await asyncio.sleep(.01)
                await asyncio.wrap_future(submitted[0])

    async def assert_progress(self, item, path):
        self.assertEqual(await self.rows('SELECT id,file_path FROM items'),
                         [{'id': item, 'file_path': str(path)}])
        self.assertEqual(await self.rows('SELECT item_id,progress_percent FROM reading_progress'),
                         [{'item_id': item, 'progress_percent': 50.0}])

    async def test_failed_file_move_and_rescan_keep_progress_until_repair(self):
        src, item = await self.seed()
        dest = src.with_name('Book 01.cbz')
        original = src.read_bytes()
        src.rename(dest)
        dest.write_bytes(original[:8])
        os.utime(dest, (time.time() - 180, time.time() - 180))
        with self.assertLogs(scanner.logger, level='ERROR') as logs:
            await self.deliver(FileMovedEvent(str(src), str(dest)))
        self.assertEqual(len(logs.records), 1)
        self.assertIn('Incomplete or corrupt archive', logs.output[0])
        await self.assert_progress(item, src)
        self.assertTrue(await self.rows("SELECT id FROM scan_events WHERE event_type='error'"))
        with self.assertLogs(scanner.logger, level='ERROR') as logs:
            await self.scan()
        self.assertEqual(len(logs.records), 1)
        self.assertIn('Incomplete or corrupt archive', logs.output[0])
        await self.assert_progress(item, src)
        dest.write_bytes(original)
        await self.scan()
        await self.assert_progress(item, dest)

    async def test_failed_folder_move_keeps_progress_until_repair(self):
        src, item = await self.seed()
        original = src.read_bytes()
        old = src.parent
        new = old.with_name('New')
        old.rename(new)
        dest = new / src.name
        dest.write_bytes(original[:8])
        os.utime(dest, (time.time() - 180, time.time() - 180))
        with self.assertLogs(scanner.logger, level='ERROR') as logs:
            await self.deliver(DirMovedEvent(str(old), str(new)))
        self.assertEqual(len(logs.records), 1)
        self.assertIn('Incomplete or corrupt archive', logs.output[0])
        await self.assert_progress(item, src)
        dest.write_bytes(original)
        await self.scan()
        await self.assert_progress(item, dest)

    async def test_folder_reconciliation_cannot_delete_an_item_adopted_while_waiting(self):
        from app.database import _write_lock, write_transaction
        src, item = await self.seed()
        old = src.parent
        new = old.with_name('New')
        old.rename(new)
        dest = new / src.name

        async def queued(count):
            async with asyncio.timeout(5):
                while len(_write_lock._waiters or []) < count:
                    await asyncio.sleep(.01)

        # Arrange two real writers, import first. The deletion observes the
        # old path before either writer is allowed to commit.
        async with write_transaction() as db:
            importing = asyncio.create_task(scanner.process_file(1, str(self.root), dest))
            await queued(1)
            deleting = asyncio.create_task(scanner.mark_folder_missing(1, str(old)))
            await queued(2)
            await db.commit()
        await asyncio.gather(importing, deleting)
        await self.assert_progress(item, dest)
        self.assertTrue(dest.exists())
        # Control: a file genuinely removed from its current folder goes away.
        dest.unlink()
        await scanner.mark_folder_missing(1, str(new))
        self.assertEqual(await self.rows('SELECT id FROM items'), [])
