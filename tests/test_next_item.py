"""Which item the reader hands over to at the end of one.

The ordering has to be the series page's own, and the last item has to
answer "there is nothing next" -- that answer is what decides whether a reader is shown
an end-of-book offer at all.
"""
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.reader.router import _next_item


class NextItemCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'B', '/b', 'ebook')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path) "
            "VALUES (1, 1, 'Serie', 'serie', 'Serie')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def add(self, item_id, title, number, *, series_id=1):
        await self.db.execute(
            "INSERT INTO items (id, series_id, library_id, title, sort_title, file_path, "
            "file_hash, file_size, file_format, number_in_series) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, 1, 'epub', ?)",
            (item_id, series_id, title, title.lower(), f"/b/{item_id}.epub",
             f"h{item_id}", number),
        )
        await self.db.commit()

    async def test_the_next_item_is_the_one_after_it_in_series_order(self):
        # Inserted out of order on purpose: it is number_in_series that
        # decides, not the id and not insertion order.
        await self.add(7, "Volume 2", 2)
        await self.add(3, "Volume 1", 1)
        await self.add(9, "Volume 3", 3)
        row = await _next_item(self.db, 1, 3)
        self.assertEqual(row["id"], 7)
        self.assertEqual(row["title"], "Volume 2")

    async def test_a_hand_ordered_series_is_read_in_its_hand_order(self):
        # The series page shows the reader's own order once they have
        # dragged items into place; "next" followed the numbers instead.
        await self.add(1, "Volume 1", 1)
        await self.add(2, "Volume 2", 2)
        await self.add(3, "Volume 3", 3)
        await self.db.execute("UPDATE series SET manual_sort = 1 WHERE id = 1")
        for item_id, position in ((1, 0), (3, 1), (2, 2)):
            await self.db.execute("UPDATE items SET manual_order = ? WHERE id = ?", (position, item_id))
        await self.db.commit()
        self.assertEqual((await _next_item(self.db, 1, 1))["id"], 3)
        self.assertEqual((await _next_item(self.db, 1, 3))["id"], 2)
        self.assertIsNone(await _next_item(self.db, 1, 2))

    async def test_the_last_item_has_nothing_after_it(self):
        await self.add(1, "Volume 1", 1)
        await self.add(2, "Volume 2", 2)
        self.assertIsNone(await _next_item(self.db, 1, 2))

    async def test_an_item_in_no_series_has_nothing_after_it(self):
        await self.add(1, "Loose book", None, series_id=None)
        self.assertIsNone(await _next_item(self.db, None, 1))

    async def test_an_unnumbered_item_sorts_after_the_numbered_ones(self):
        await self.add(1, "Volume 1", 1)
        await self.add(2, "Extras", None)
        row = await _next_item(self.db, 1, 1)
        self.assertEqual(row["id"], 2)
        self.assertIsNone(await _next_item(self.db, 1, 2))
