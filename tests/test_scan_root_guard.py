"""A scan that cannot see the library must not empty it.

Reconciliation deletes every item whose file it did not find. With the
library root missing (a share or drive not mounted when the nightly scan
runs) or empty (a bare mount point), every file reads as confidently gone:
measured before the guard, 4 items and their reading progress went to 0
and 0 in one scan, and the error saying why was deleted in the same pass.
"""
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.config import settings
from app.database import close_pool, get_connection, init_db
from app.library import events, scanner


class RootGuard(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        original = settings.data_dir
        settings.data_dir = tmp / "data"
        settings.data_dir.mkdir()
        (settings.data_dir / "covers").mkdir()
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        original_publish = events.publish
        events.publish = lambda event: None
        self.addCleanup(lambda: setattr(events, "publish", original_publish))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        await init_db()
        self.root = tmp / "lib"
        for n in (1, 2):
            path = self.root / "S" / f"S {n:02}.cbz"
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("001.txt", "not an image")  # no cover needed
        db = await get_connection()
        try:
            await db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
            await db.execute("INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'L', ?, 'comic')",
                             (str(self.root),))
            await db.commit()
        finally:
            await db.close()
        await scanner.scan_library(1, str(self.root))
        db = await get_connection()
        try:
            await db.execute("INSERT INTO reading_progress (user_id, item_id, status) "
                             "SELECT 1, id, 'reading' FROM items")
            await db.commit()
        finally:
            await db.close()

    async def counts(self):
        db = await get_connection()
        try:
            out = []
            for sql in ("SELECT COUNT(*) FROM items", "SELECT COUNT(*) FROM reading_progress",
                        "SELECT COUNT(*) FROM scan_events WHERE event_type = 'error'"):
                out.append((await (await db.execute(sql)).fetchone())[0])
            return tuple(out)
        finally:
            await db.close()

    async def test_the_setup_imported_both(self):
        self.assertEqual((await self.counts())[:2], (2, 2))

    async def test_a_missing_root_removes_nothing_and_says_so(self):
        self.root.rename(self.root.with_name("elsewhere"))
        await scanner.scan_library(1, str(self.root))
        self.assertEqual(await self.counts(), (2, 2, 1))

    async def test_an_empty_root_removes_nothing_and_says_so(self):
        shutil.rmtree(self.root)
        self.root.mkdir()  # the bare mount point
        await scanner.scan_library(1, str(self.root))
        self.assertEqual(await self.counts(), (2, 2, 1))

    async def test_the_error_clears_once_the_files_are_back(self):
        moved = self.root.with_name("elsewhere")
        self.root.rename(moved)
        await scanner.scan_library(1, str(self.root))
        await scanner.scan_library(1, str(self.root))
        self.assertEqual((await self.counts())[2], 1)  # replaced, not piled up
        moved.rename(self.root)
        await scanner.scan_library(1, str(self.root))
        self.assertEqual(await self.counts(), (2, 2, 0))

    async def test_one_file_really_deleted_still_goes(self):
        (self.root / "S" / "S 01.cbz").unlink()
        await scanner.scan_library(1, str(self.root))
        self.assertEqual((await self.counts())[:2], (1, 1))
