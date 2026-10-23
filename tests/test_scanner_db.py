"""The scanner's decisions that need a database to express.

These run against a real (temporary) SQLite file built by the app's own
init_db, not a hand-written fixture schema -- a fixture would be a second
copy of schema.sql to keep in sync, and drift there would make the tests
agree with themselves while disagreeing with production.
"""
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db, write_transaction
from app.library import events
from app.library.scanner import (
    _delete_item,
    _recompute_series_cover,
    _renumber_series,
    _touch_series,
)


class ScannerDatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        # settings.db_path/covers_dir are properties over data_dir, and both
        # are read at call time, so pointing data_dir at a temp directory is
        # enough to isolate a test run from any real library.
        original_data_dir = settings.data_dir
        settings.data_dir = self.tmp
        self.addCleanup(lambda: setattr(settings, "data_dir", original_data_dir))
        (self.tmp / "covers").mkdir()

        # events.publish fans out to connected SSE clients; there are none
        # here, but collecting them lets a test assert what the UI would
        # have been told.
        self.published: list[dict] = []
        original_publish = events.publish
        events.publish = self.published.append
        self.addCleanup(lambda: setattr(events, "publish", original_publish))

        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close_db)
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'Manga', '/lib', 'comic')"
        )
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path) "
            "VALUES (1, 1, 'Serie', 'serie', 'Serie')"
        )
        await self.db.commit()

    async def _close_db(self):
        await self.db.close()

    async def add_item(self, item_id, title, *, cover=None, number=None,
                       manual_order=None, series_id=1):
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path, "
            "file_format, file_hash, file_size, cover_path, number_in_series, manual_order) "
            "VALUES (?, 1, ?, ?, ?, ?, 'cbz', ?, 1, ?, ?, ?)",
            (item_id, series_id, title, title.lower(), f"/lib/Serie/{title}.cbz",
             f"hash{item_id}", cover, number, manual_order),
        )
        await self.db.commit()

    async def series_row(self, series_id=1):
        return await (
            await self.db.execute("SELECT * FROM series WHERE id = ?", (series_id,))
        ).fetchone()

    async def numbers(self, series_id=1):
        rows = await (await self.db.execute(
            "SELECT title, number_in_series FROM items WHERE series_id = ? ORDER BY id", (series_id,)
        )).fetchall()
        return {r["title"]: r["number_in_series"] for r in rows}


class SeriesCover(ScannerDatabaseCase):
    async def test_cover_follows_the_first_item_in_order(self):
        await self.add_item(2, "Serie v02", cover="2-b.webp", number=2)
        await self.add_item(1, "Serie v01", cover="1-a.webp", number=1)
        await _recompute_series_cover(self.db, 1)
        self.assertEqual((await self.series_row())["cover_path"], "1-a.webp")

    async def test_recomputes_after_the_first_item_leaves(self):
        # The live-update bug: removing v01 left the card painting v01's
        # cover until a reload.
        await self.add_item(1, "Serie v01", cover="1-a.webp", number=1)
        await self.add_item(2, "Serie v02", cover="2-b.webp", number=2)
        await _recompute_series_cover(self.db, 1)
        await _delete_item(self.db, 1)
        self.assertEqual((await self.series_row())["cover_path"], "2-b.webp")

    async def test_manual_order_decides_when_manual_sort_is_on(self):
        await self.add_item(1, "Serie v01", cover="1-a.webp", number=1, manual_order=1)
        await self.add_item(2, "Serie v02", cover="2-b.webp", number=2, manual_order=0)
        await self.db.execute("UPDATE series SET manual_sort = 1 WHERE id = 1")
        await _recompute_series_cover(self.db, 1)
        self.assertEqual((await self.series_row())["cover_path"], "2-b.webp")

    async def test_an_uploaded_cover_is_never_overwritten(self):
        # cover_overridden exists because _recompute_series_cover runs from
        # every path that touches a series -- without the early return an
        # upload would survive only until the next scan.
        await self.add_item(1, "Serie v01", cover="1-a.webp", number=1)
        await self.db.execute(
            "UPDATE series SET cover_path = 'series-1-abc.webp', cover_overridden = 1 WHERE id = 1"
        )
        await _recompute_series_cover(self.db, 1)
        self.assertEqual((await self.series_row())["cover_path"], "series-1-abc.webp")

    async def test_a_series_with_no_covers_ends_up_with_none(self):
        await self.add_item(1, "Serie v01", cover=None, number=1)
        await self.db.execute("UPDATE series SET cover_path = 'stale.webp' WHERE id = 1")
        await _recompute_series_cover(self.db, 1)
        self.assertIsNone((await self.series_row())["cover_path"])


class Renumbering(ScannerDatabaseCase):
    async def test_volume_numbers_are_written_from_the_titles(self):
        for i, title in [(1, "Serie v03"), (2, "Serie v01"), (3, "Serie v02")]:
            await self.add_item(i, title)
        await _renumber_series(self.db, 1)
        self.assertEqual(await self.numbers(),
                         {"Serie v03": 3.0, "Serie v01": 1.0, "Serie v02": 2.0})

    async def test_a_year_already_stored_as_the_index_is_corrected(self):
        # What the startup backfill does for a library imported while a
        # year tag was being read as the chapter: the rows already hold
        # 2026, the files never change, and only a renumber reaches them.
        for i, n in [(1, 31), (2, 32)]:
            await self.add_item(i, f"Serie {n:03d} (2026) (Digital) (Grupo)", number=2026)
        await _renumber_series(self.db, 1)
        self.assertEqual(await self.numbers(), {
            "Serie 031 (2026) (Digital) (Grupo)": 31.0,
            "Serie 032 (2026) (Digital) (Grupo)": 32.0,
        })

    async def test_deleting_items_can_renumber_the_survivors(self):
        # Numbering is a property of the SERIES, not of a file, so removing
        # items can change what the remaining ones should be numbered by --
        # which is why _delete_item calls _renumber_series at all.
        #
        # Here volume wins first on coverage (4 files carry one, only 2
        # carry a chapter), so both chapter files collapse onto v01...
        #
        # (No brackets around the volume: a bracketed group is a tag, and
        # the bare number is read from outside it -- "c002 (v01)" would
        # give a bare 2 that ties volume on coverage and beats it.)
        await self.add_item(1, "Serie - c001 v01")
        await self.add_item(2, "Serie - c002 v01")
        await self.add_item(3, "Serie v03")
        await self.add_item(4, "Serie v04")
        await _renumber_series(self.db, 1)
        self.assertEqual(await self.numbers(), {
            "Serie - c001 v01": 1.0, "Serie - c002 v01": 1.0,
            "Serie v03": 3.0, "Serie v04": 4.0,
        })
        # ...and once the volume-only files leave, coverage ties and
        # distinctness picks chapter instead, so a SURVIVOR's number
        # changes: c002 goes from 1.0 to 2.0.
        await _delete_item(self.db, 3)
        await _delete_item(self.db, 4)
        self.assertEqual(await self.numbers(),
                         {"Serie - c001 v01": 1.0, "Serie - c002 v01": 2.0})

    async def test_an_item_without_the_winning_kind_falls_back_to_its_own(self):
        # Documented behaviour worth pinning: None would sort the item to
        # the very end of the series, which is a worse place for a volume
        # that merely labels its number differently than its neighbours.
        await self.add_item(1, "Serie - c001")
        await self.add_item(2, "Serie - c002")
        await self.add_item(3, "Serie v05")
        await _renumber_series(self.db, 1)
        self.assertEqual(await self.numbers(),
                         {"Serie - c001": 1.0, "Serie - c002": 2.0, "Serie v05": 5.0})

    async def test_a_lone_file_carrying_both_markers_is_read_as_a_chapter(self):
        # Coverage and distinctness both tie at one file, so the fixed kind
        # priority decides -- the same answer extract_number_in_series gives
        # for this shape in isolation.
        await self.add_item(1, "Serie - c003 (v02)")
        await _renumber_series(self.db, 1)
        self.assertEqual(await self.numbers(), {"Serie - c003 (v02)": 3.0})

    async def test_an_unnumberable_item_gets_null(self):
        await self.add_item(1, "Serie - Extras")
        await _renumber_series(self.db, 1)
        self.assertEqual(await self.numbers(), {"Serie - Extras": None})


class ItemDeletion(ScannerDatabaseCase):
    async def test_reading_progress_is_cascaded(self):
        await self.add_item(1, "Serie v01", cover="1-a.webp", number=1)
        await self.add_item(2, "Serie v02", cover="2-b.webp", number=2)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'a', 'x')")
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent) "
            "VALUES (1, 1, 'reading', 40)"
        )
        await self.db.commit()
        await _delete_item(self.db, 1)
        left = await (await self.db.execute("SELECT COUNT(*) AS n FROM reading_progress")).fetchone()
        self.assertEqual(left["n"], 0)

    async def test_the_items_thumbnail_is_unlinked(self):
        thumb = settings.covers_dir / "1-a.webp"
        thumb.write_bytes(b"x")
        await self.add_item(1, "Serie v01", cover="1-a.webp", number=1)
        await self.add_item(2, "Serie v02", cover="2-b.webp", number=2)
        await _delete_item(self.db, 1)
        self.assertFalse(thumb.exists())

    async def test_the_last_item_leaving_removes_the_series(self):
        await self.add_item(1, "Serie v01", cover="1-a.webp", number=1)
        await _delete_item(self.db, 1)
        self.assertIsNone(await self.series_row())

    async def test_an_uploaded_series_cover_keeps_the_series_and_the_file(self):
        # An uploaded cover is work the reader did, so emptying the series
        # no longer throws it away: the row is kept (see
        # _series_holds_reader_state) and the file with it, because a
        # chapter landing back in that folder finds both again.
        # tests/test_empty_series.py covers the rule itself.
        uploaded = settings.covers_dir / "series-1-abc123456789.webp"
        uploaded.write_bytes(b"x")
        await self.add_item(1, "Serie v01", number=1)
        await self.db.execute(
            "UPDATE series SET cover_path = 'series-1-abc123456789.webp', cover_overridden = 1 WHERE id = 1"
        )
        await _delete_item(self.db, 1)
        self.assertIsNotNone(await self.series_row())
        self.assertTrue(uploaded.exists())

    async def test_an_items_thumbnail_is_never_deleted_as_a_series_cover(self):
        # A series whose cover_path is an ITEM's file (the normal case) must
        # not have that file unlinked when the series row goes -- it is the
        # item's, and is unlinked by the item's own branch.
        item_thumb = settings.covers_dir / "1-a.webp"
        item_thumb.write_bytes(b"x")
        await self.add_item(1, "Serie v01", cover=None, number=1)
        await self.db.execute("UPDATE series SET cover_path = '1-a.webp' WHERE id = 1")
        await _delete_item(self.db, 1)
        self.assertTrue(item_thumb.exists())

    async def test_deleting_a_missing_item_is_a_no_op(self):
        await _delete_item(self.db, 999)
        self.assertEqual(self.published, [])

    async def test_the_open_screens_are_told(self):
        await self.add_item(1, "Serie v01", number=1)
        await _delete_item(self.db, 1)
        self.assertEqual([e["type"] for e in self.published],
                         ["item_removed", "series_removed"])


class SeriesTimestamps(ScannerDatabaseCase):
    async def test_touch_moves_updated_at_but_not_created_at(self):
        # updated_at was a dead column: it defaulted to created_at and
        # nothing ever wrote to it, so "Recently modified" and "Recently
        # added" could only ever produce the same order.
        before = await self.series_row()
        await self.db.execute("UPDATE series SET updated_at = '2000-01-01 00:00:00' WHERE id = 1")
        await _touch_series(self.db, 1)
        after = await self.series_row()
        self.assertNotEqual(after["updated_at"], "2000-01-01 00:00:00")
        self.assertEqual(after["created_at"], before["created_at"])


class WriteTransaction(unittest.IsolatedAsyncioTestCase):
    """The global write lock is what every mutation goes through, and the
    reconciliation bug was a failure to roll back cleanly."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()

    async def count_libraries(self):
        db = await get_connection()
        try:
            return (await (await db.execute("SELECT COUNT(*) AS n FROM libraries")).fetchone())["n"]
        finally:
            await db.close()

    async def insert_library(self, db, library_id):
        await db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (?, 'X', '/x', 'comic')",
            (library_id,),
        )

    async def test_the_caller_commits(self):
        # Not an auto-commit context manager, by design -- the docstring
        # spells out `async with write_transaction() as db: ...; commit()`.
        # Pinned because a block that forgets the commit fails silently.
        async with write_transaction() as db:
            await self.insert_library(db, 1)
        self.assertEqual(await self.count_libraries(), 0)
        async with write_transaction() as db:
            await self.insert_library(db, 1)
            await db.commit()
        self.assertEqual(await self.count_libraries(), 1)

    async def test_a_raising_block_rolls_back_work_already_committed_inside(self):
        # The reconciliation bug in the scan pass was exactly this shape: an
        # unguarded PermissionError propagating out of the block after some
        # of the series had already been updated.
        with self.assertRaises(RuntimeError):
            async with write_transaction() as db:
                await self.insert_library(db, 1)
                raise RuntimeError("boom")
        self.assertEqual(await self.count_libraries(), 0)

    async def test_the_lock_is_released_after_a_failure(self):
        # If the write lock leaked on the error path, the next writer would
        # hang forever -- which is worse than the failure that caused it.
        with self.assertRaises(RuntimeError):
            async with write_transaction() as db:
                raise RuntimeError("boom")
        async with write_transaction() as db:
            await self.insert_library(db, 2)
            await db.commit()
        self.assertEqual(await self.count_libraries(), 1)


if __name__ == "__main__":
    unittest.main()


class SeriesListing(ScannerDatabaseCase):
    """The series page renders every item, with no pagination.

    It used to render 60 at a time, which cost that page its own search:
    the box filters cards already in the DOM, so it could only ever see the
    current page. Pinned here because re-introducing a LIMIT would silently
    take the search back with it.
    """

    async def load(self):
        """(items, progress_map) -- the third value is the per-item time
        estimate, which has its own tests in tests/test_estimates.py."""
        from app.library.router import _load_series_items
        series = await self.series_row()
        items, progress_map, _eta, _rank, _sortable = await _load_series_items(self.db, 1, series)
        return items, progress_map

    async def test_every_item_is_returned_past_the_old_page_size(self):
        for i in range(1, 131):
            await self.add_item(i, f"Serie v{i:03d}", number=float(i))
        items, _ = await self.load()
        self.assertEqual(len(items), 130)

    async def test_items_come_back_in_natural_order(self):
        await self.add_item(1, "Serie v10", number=10.0)
        await self.add_item(2, "Serie v02", number=2.0)
        await self.add_item(3, "Serie v01", number=1.0)
        items, _ = await self.load()
        self.assertEqual([i["title"] for i in items],
                         ["Serie v01", "Serie v02", "Serie v10"])

    async def test_manual_order_wins_when_it_is_set(self):
        await self.add_item(1, "Serie v01", number=1.0, manual_order=2)
        await self.add_item(2, "Serie v02", number=2.0, manual_order=0)
        await self.add_item(3, "Serie v03", number=3.0, manual_order=1)
        await self.db.execute("UPDATE series SET manual_sort = 1 WHERE id = 1")
        items, _ = await self.load()
        self.assertEqual([i["title"] for i in items],
                         ["Serie v02", "Serie v03", "Serie v01"])

    async def test_progress_is_returned_for_the_items_that_have_it(self):
        await self.add_item(1, "Serie v01", number=1.0)
        await self.add_item(2, "Serie v02", number=2.0)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'a', 'x')")
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent) "
            "VALUES (1, 2, 'reading', 40)"
        )
        await self.db.commit()
        _, progress_map = await self.load()
        self.assertEqual(set(progress_map), {2})
        self.assertEqual(progress_map[2]["progress_percent"], 40)
