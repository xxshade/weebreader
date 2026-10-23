"""Marking an EPUB read clears where it was being read.

mark_read gives an EPUB no page (its position is a spine index plus a
paragraph), and the old paragraph kept on its own was applied to spine 0:
the next open was a blank page reading "41 / 3", in both browsers.
"""
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.reading_progress import mark_item_read


class MarkReadLocation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addAsyncCleanup(self.db.close)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute("INSERT INTO libraries (id,name,root_path,media_type) VALUES (1,'L','/m','ebook')")
        await self.db.execute(
            "INSERT INTO items (id,library_id,title,sort_title,file_path,file_format,file_hash,file_size)"
            " VALUES (1,1,'B','b','/m/b.epub','epub','h',1)")
        await self.db.execute(
            "INSERT INTO reading_progress (user_id,item_id,status,current_page,current_location,progress_percent)"
            " VALUES (1,1,'reading',3,'elidx:40',55)")
        await self.db.commit()

    async def test_the_paragraph_goes_with_the_page(self):
        await mark_item_read(self.db, 1, 1, None)
        await self.db.commit()
        row = await (await self.db.execute(
            "SELECT status, current_page, current_location, completed_from_percent"
            " FROM reading_progress")).fetchone()
        self.assertEqual(tuple(row), ("completed", None, None, 55))
