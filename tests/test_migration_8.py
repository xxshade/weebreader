"""Migration 8: items.missing and libraries.scan_settings go, and scan_events keeps only errors.

`missing` was written by nothing once removing a vanished file became
automatic, but some twenty queries still filtered on it -- and a row an old
version had set to 1 stayed hidden even with its file back. scan_events
grew by one row per import or removal, and only its errors were ever read.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import close_pool, get_connection, init_db


class Migration8(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        await init_db()
        # Back to what a version-7 database looked like.
        db = sqlite3.connect(settings.db_path)
        db.execute("ALTER TABLE items ADD COLUMN missing INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE libraries ADD COLUMN scan_settings TEXT")
        db.execute("INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'L', '/l', 'comic')")
        db.execute("INSERT INTO items (id, library_id, title, sort_title, file_path, file_format, file_hash,"
                   " file_size, missing) VALUES (1, 1, 'A', 'a', '/l/a.cbz', 'cbz', 'h', 1, 1)")
        for kind in ("added", "modified", "removed", "error"):
            db.execute("INSERT INTO scan_events (library_id, event_type, file_path) VALUES (1, ?, '/l/a.cbz')",
                       (kind,))
        db.execute("PRAGMA user_version = 7")
        db.commit()
        db.close()

    async def test_the_column_goes_and_the_row_stays(self):
        await init_db()
        db = await get_connection()
        try:
            columns = {r[1] for r in await (await db.execute("PRAGMA table_info(items)")).fetchall()}
            self.assertNotIn("missing", columns)
            columns = {r[1] for r in await (await db.execute("PRAGMA table_info(libraries)")).fetchall()}
            self.assertNotIn("scan_settings", columns)
            self.assertEqual((await (await db.execute("SELECT COUNT(*) FROM items")).fetchone())[0], 1)
            kinds = [r[0] for r in await (await db.execute("SELECT event_type FROM scan_events")).fetchall()]
            self.assertEqual(kinds, ["error"])
        finally:
            await db.close()
