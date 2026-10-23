"""What a rescan of an unchanged library costs, and what it may not do.

* An unchanged file is not read. Size and modification time against what
  was recorded at import decide; the hash is only taken for a file whose
  size or time moved. Every scan used to hash every file in full just to
  learn it was the same, which made the daily scheduled scan a read of the
  whole library.
* The walk and the reconciliation's filesystem checks run in threads, not
  on the event loop (237 ms for 20,000 files on a warm local disk; seconds
  on a share, with the whole app frozen).
* One scan per library at a time; asked again meanwhile, it runs once more
  afterwards instead of beside it.
* A broken file that has sat unchanged is reported at once. The waits for
  a file still being copied are for a file modified moments ago.
"""
import asyncio
import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from app.config import settings
from app.database import close_pool, get_connection, init_db
from app.library import events, scanner
from tests.test_archives import PNG_1PX


def write_cbz(path: Path, pages: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for n in range(pages):
            z.writestr(f"{n:03}.png", PNG_1PX)


class ScanCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        original = settings.data_dir
        settings.data_dir = self.tmp / "data"
        settings.data_dir.mkdir()
        (settings.data_dir / "covers").mkdir()
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        original_publish = events.publish
        events.publish = lambda event: None
        self.addCleanup(lambda: setattr(events, "publish", original_publish))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        await init_db()
        self.root = self.tmp / "lib"
        self.root.mkdir()
        db = await get_connection()
        try:
            await db.execute("INSERT INTO libraries (id, name, root_path, media_type)"
                             " VALUES (1, 'L', ?, 'comic')", (str(self.root),))
            await db.commit()
        finally:
            await db.close()

        self.hashed: list[str] = []
        original_hash = scanner._sha256_of

        def counting(path):
            self.hashed.append(Path(path).name)
            return original_hash(path)

        scanner._sha256_of = counting
        self.addCleanup(lambda: setattr(scanner, "_sha256_of", original_hash))

    async def scan(self):
        self.hashed.clear()
        return await scanner.scan_library(1, str(self.root))

    async def items(self):
        db = await get_connection()
        try:
            rows = await (await db.execute(
                "SELECT file_path, file_hash, file_mtime_ns FROM items ORDER BY file_path")).fetchall()
        finally:
            await db.close()
        return [dict(r) for r in rows]


class AnUnchangedFileIsNotRead(ScanCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for n in (1, 2, 3):
            write_cbz(self.root / "Serie" / f"v{n}.cbz")
        await self.scan()

    async def test_the_import_hashes_each_file_once_and_records_its_time(self):
        self.assertEqual(sorted(self.hashed), ["v1.cbz", "v2.cbz", "v3.cbz"])
        for item in await self.items():
            self.assertEqual(item["file_mtime_ns"], Path(item["file_path"]).stat().st_mtime_ns)

    async def test_a_rescan_hashes_nothing(self):
        await self.scan()
        self.assertEqual(self.hashed, [])

    async def test_a_changed_file_is_hashed_and_reimported(self):
        target = self.root / "Serie" / "v2.cbz"
        before = {i["file_path"]: i["file_hash"] for i in await self.items()}
        write_cbz(target, pages=5)
        await self.scan()
        self.assertEqual(self.hashed, ["v2.cbz"])
        after = {i["file_path"]: i["file_hash"] for i in await self.items()}
        self.assertNotEqual(before[str(target)], after[str(target)])

    async def test_a_touched_file_is_hashed_once_then_skipped_again(self):
        target = self.root / "Serie" / "v1.cbz"
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        await self.scan()
        self.assertEqual(self.hashed, ["v1.cbz"])
        await self.scan()
        self.assertEqual(self.hashed, [])

    async def test_a_row_from_before_the_column_is_hashed_once(self):
        db = await get_connection()
        try:
            await db.execute("UPDATE items SET file_mtime_ns = NULL")
            await db.commit()
        finally:
            await db.close()
        await self.scan()
        self.assertEqual(sorted(self.hashed), ["v1.cbz", "v2.cbz", "v3.cbz"])
        await self.scan()
        self.assertEqual(self.hashed, [])


class TheDiskIsAskedOffTheLoop(ScanCase):
    async def test_the_walk_and_the_reconciliation_run_in_threads(self):
        write_cbz(self.root / "Serie" / "v1.cbz")
        # Another series that stays: a library with NO file left is not
        # reconciled at all (test_scan_root_guard.py).
        write_cbz(self.root / "Other" / "o1.cbz")
        await self.scan()
        (self.root / "Serie" / "v1.cbz").unlink()

        where: list[str] = []
        original_walk, original_exists = os.walk, scanner._path_exists

        def walk(*args, **kwargs):
            where.append(threading.current_thread().name)
            return original_walk(*args, **kwargs)

        def exists(path):
            where.append(threading.current_thread().name)
            return original_exists(path)

        scanner.os.walk = walk
        scanner._path_exists = exists
        try:
            await self.scan()
        finally:
            scanner.os.walk = original_walk
            scanner._path_exists = original_exists
        self.assertTrue(where)
        self.assertNotIn(threading.main_thread().name, where)
        self.assertEqual([Path(i["file_path"]).name for i in await self.items()], ["o1.cbz"])


class AFileArrivingDuringReconciliationIsKept(ScanCase):
    """The folders are looked at before the write lock and the deletions
    made inside it, so an empty-looking folder is looked at again there:
    a volume copied in between would otherwise have its series removed
    from under it and come back as a different series."""

    async def test_its_series_is_not_removed_under_it(self):
        db = await get_connection()
        try:
            await db.execute("INSERT INTO series (id, library_id, name, sort_name, folder_path)"
                             " VALUES (50, 1, 'Late', 'late', 'Late')")
            await db.commit()
        finally:
            await db.close()

        original = scanner._empty_folders
        calls = 0

        def arrival_after_first_look(folders):
            nonlocal calls
            calls += 1
            answer = original(folders)
            if calls == 1:
                write_cbz(self.root / "Late" / "v1.cbz")
            return answer

        scanner._empty_folders = arrival_after_first_look
        self.addCleanup(lambda: setattr(scanner, "_empty_folders", original))
        await self.scan()

        db = await get_connection()
        try:
            kept = await (await db.execute("SELECT id FROM series WHERE id = 50")).fetchone()
        finally:
            await db.close()
        self.assertEqual(calls, 2)
        self.assertIsNotNone(kept)

    async def test_without_the_arrival_it_goes(self):
        # The control: the same empty series, nothing arriving.
        db = await get_connection()
        try:
            await db.execute("INSERT INTO series (id, library_id, name, sort_name, folder_path)"
                             " VALUES (50, 1, 'Late', 'late', 'Late')")
            await db.commit()
        finally:
            await db.close()
        await self.scan()
        db = await get_connection()
        try:
            kept = await (await db.execute("SELECT id FROM series WHERE id = 50")).fetchone()
        finally:
            await db.close()
        self.assertIsNone(kept)


class OneScanPerLibrary(ScanCase):
    async def test_asked_twice_meanwhile_it_runs_once_more_after(self):
        write_cbz(self.root / "Serie" / "v1.cbz")
        walks = 0
        original = scanner._walk

        def counting(root):
            nonlocal walks
            walks += 1
            time.sleep(0.05)
            return original(root)

        scanner._walk = counting
        self.addCleanup(lambda: setattr(scanner, "_walk", original))
        results = await asyncio.gather(
            scanner.scan_library(1, str(self.root)),
            scanner.scan_library(1, str(self.root)),
            scanner.scan_library(1, str(self.root)),
        )
        self.assertEqual(walks, 2)
        self.assertEqual([r.get("queued", False) for r in results], [False, True, True])
        self.assertEqual(scanner._scanning, {})

    async def test_a_scan_that_fails_does_not_stay_running(self):
        original = scanner._walk

        def broken(root):
            raise RuntimeError("disk gone")

        scanner._walk = broken
        self.addCleanup(lambda: setattr(scanner, "_walk", original))
        with self.assertRaises(RuntimeError):
            await scanner.scan_library(1, str(self.root))
        self.assertEqual(scanner._scanning, {})


class ABrokenFileIsNotWaitedOn(ScanCase):
    async def test_an_old_broken_file_is_reported_at_once(self):
        broken = self.root / "Serie" / "v1.cbz"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"\x00" * 4096)
        hour_ago = time.time() - 3600
        os.utime(broken, (hour_ago, hour_ago))
        started = time.monotonic()
        with self.assertLogs(scanner.logger, level='ERROR') as logs:
            await self.scan()
        self.assertEqual(len(logs.records), 1)
        self.assertIn('Neither ZIP nor RAR recognized', logs.output[0])
        self.assertLess(time.monotonic() - started, 1.5)
        db = await get_connection()
        try:
            errors = await (await db.execute(
                "SELECT COUNT(*) FROM scan_events WHERE event_type = 'error'")).fetchone()
        finally:
            await db.close()
        self.assertEqual(errors[0], 1)

    async def test_a_fresh_one_is_still_given_time(self):
        # The control: the same file, modified just now, is waited on.
        original = scanner._INCOMPLETE_RETRY_DELAYS
        scanner._INCOMPLETE_RETRY_DELAYS = (0.3,)
        self.addCleanup(lambda: setattr(scanner, "_INCOMPLETE_RETRY_DELAYS", original))
        broken = self.root / "Serie" / "v1.cbz"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"\x00" * 4096)
        started = time.monotonic()
        with self.assertLogs(scanner.logger, level='ERROR') as logs:
            await self.scan()
        self.assertEqual(len(logs.records), 1)
        self.assertIn('Neither ZIP nor RAR recognized', logs.output[0])
        self.assertGreaterEqual(time.monotonic() - started, 0.3)


if __name__ == "__main__":
    unittest.main()
