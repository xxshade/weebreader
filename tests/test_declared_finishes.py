"""Marking something read is a DECLARATION, and the statistics that measure
TIME must not read it as a measurement.

"Mark as read" is available from anywhere -- a book never opened, one
abandoned at 12%, one genuinely finished -- and it writes the same
`progress_percent = 100` in all three cases. Two statistics are built on
that column and on `status = 'completed'`:

  * "how long one volume takes", which averages finished things;
  * the seconds-per-byte reading rate, which is what every estimate on
    every card is scaled from.

Both go through the real handlers here rather than writing rows: what is
being pinned is the coupling between what the button records and what the
statistics believe, and a test that INSERTed its own reading_progress
would keep passing while the two halves drifted apart.
"""
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db, write_transaction
from app.tracking import service as tracking
from app.novels.router import set_read_status, set_volume_read_status
from app.reading_progress import mark_item_read, mark_item_unread, mark_series_read
from app.stats import queries
from app.stats.estimates import build_estimator


class _JsonRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        # These tests drive the real mark-read handlers, and those queue a
        # tracker push (app/tracking/service.py). Nothing is linked here so
        # each one returns immediately -- but a task still pending when
        # this test's event loop closes strands a non-daemon aiosqlite
        # thread, and the whole suite then stops exiting.
        self.addAsyncCleanup(tracking.drain)
        self.user = {"id": 1}
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'L','/l','ebook')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'S', 's', '/f')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def item(self, item_id, *, text_size=100_000):
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size, text_size, page_count)"
            " VALUES (?, 1, 1, ?, ?, ?, 'epub', ?, 1, ?, 200)",
            (item_id, f"I{item_id}", f"i{item_id}", f"/x/{item_id}", f"h{item_id}", text_size))
        await self.db.commit()

    async def read_for(self, seconds, *, item_id=None, chapter_id=None, cap=180):
        remaining = seconds
        while remaining > 0:
            chunk = min(remaining, cap)
            await self.db.execute(
                "INSERT INTO page_reads (user_id, item_id, chapter_id, novel_id, kind, page_index,"
                " started_at, seconds, cap_seconds) VALUES (1,?,?,?,"
                " CASE WHEN ? IS NOT NULL THEN 'novel'"
                "      ELSE COALESCE((SELECT file_format FROM items WHERE id = ?), 'unknown') END,"
                " 0,datetime('now'),?,?)",
                (item_id, chapter_id, 1 if chapter_id else None, chapter_id, item_id, chunk, cap))
            remaining -= chunk
        await self.db.commit()

    async def reached(self, item_id, percent):
        """Where the reader got to, as their own progress saves leave it."""
        await self.db.execute(
            "INSERT OR REPLACE INTO reading_progress (user_id, item_id, status, progress_percent,"
            " last_read_at) VALUES (1, ?, 'reading', ?, datetime('now'))", (item_id, percent))
        await self.db.commit()

    async def mark_read(self, item_id):
        async with write_transaction() as db:
            await mark_item_read(db, 1, item_id, 199)
            await db.commit()

    async def finish_tile(self):
        # These fixtures are EPUBs, so the book half is the one they land
        # in -- the tile is split by format now (a comic volume and a book
        # are not the same question). See queries.time_to_finish.
        return (await queries.time_to_finish(self.db, 1))["per_book"]

    async def volume_tile(self):
        return (await queries.time_to_finish(self.db, 1))["per_volume"]

    async def item_rate(self):
        est = await build_estimator(self.db, 1)
        return est.rate_by_format.get("epub")


class HowLongOneVolumeTakes(_Case):
    async def test_a_book_read_to_the_end_is_averaged(self):
        await self.item(1)
        await self.read_for(3600, item_id=1)
        await self.reached(1, 99)
        await self.mark_read(1)
        tile = await self.finish_tile()
        self.assertEqual(tile["n"], 1)
        self.assertAlmostEqual(tile["avg_seconds"], 3600, delta=1)

    async def test_a_book_given_up_on_and_ticked_off_is_not(self):
        # The whole point. Ten minutes at 12% is not how long a book takes,
        # and before completed_from_percent existed this entered the
        # average as one.
        await self.item(1)
        await self.read_for(600, item_id=1)
        await self.reached(1, 12)
        await self.mark_read(1)
        self.assertEqual((await self.finish_tile())["n"], 0)

    async def test_a_book_never_opened_and_ticked_off_is_not(self):
        # It has no time to contribute, so it cannot land in a time
        # average -- but it must also not arrive as a zero.
        await self.item(1)
        await self.mark_read(1)
        tile = await self.finish_tile()
        self.assertEqual(tile["n"], 0)
        self.assertEqual(tile["sum_seconds"], 0)

    async def test_one_abandoned_book_cannot_drag_a_real_one_down(self):
        await self.item(1)
        await self.read_for(3600, item_id=1)
        await self.reached(1, 97)
        await self.mark_read(1)
        await self.item(2)
        await self.read_for(600, item_id=2)
        await self.reached(2, 12)
        await self.mark_read(2)
        tile = await self.finish_tile()
        self.assertEqual(tile["n"], 1)
        self.assertAlmostEqual(tile["avg_seconds"], 3600, delta=1)

    async def test_marking_a_whole_series_read_declares_from_where_you_were(self):
        # One click, three volumes: one nearly finished, one abandoned
        # early, one never opened. Only the first is a finish.
        for item_id in (1, 2, 3):
            await self.item(item_id)
        await self.read_for(3600, item_id=1)
        await self.reached(1, 96)
        await self.read_for(900, item_id=2)
        await self.reached(2, 20)
        async with write_transaction() as db:
            await mark_series_read(db, 1, 1)
            await db.commit()
        tile = await self.finish_tile()
        self.assertEqual(tile["n"], 1)
        self.assertAlmostEqual(tile["avg_seconds"], 3600, delta=1)

    async def test_re_marking_does_not_relaunder_a_declaration(self):
        # Pressing the button twice must not turn 12% into "completed from
        # 100" the second time, when progress_percent is already 100.
        await self.item(1)
        await self.read_for(600, item_id=1)
        await self.reached(1, 12)
        await self.mark_read(1)
        await self.mark_read(1)
        self.assertEqual((await self.finish_tile())["n"], 0)

    async def test_marking_unread_forgets_where_it_was_declared_from(self):
        await self.item(1)
        await self.read_for(600, item_id=1)
        await self.reached(1, 12)
        await self.mark_read(1)
        async with write_transaction() as db:
            await mark_item_unread(db, 1, 1)
            await db.commit()
        # Unread is no row at all, so nothing is left to say where it was
        # declared from.
        row = await (await self.db.execute(
            "SELECT 1 FROM reading_progress WHERE item_id = 1")).fetchone()
        self.assertIsNone(row)

    async def test_a_completion_from_before_this_was_recorded_still_counts(self):
        # NULL means "no idea", and emptying the tile for every existing
        # install would be a bigger change than the bug.
        await self.item(1)
        await self.read_for(3600, item_id=1)
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent,"
            " completed_at, completed_from_percent) VALUES (1, 1, 'completed', 100,"
            " datetime('now'), NULL)")
        await self.db.commit()
        self.assertEqual((await self.finish_tile())["n"], 1)


class TheReadingRate(_Case):
    """What every card's estimate is scaled from -- and the consumer a
    declaration hurts most, because one bad sample is spent on books the
    reader has not touched."""

    async def test_a_book_ticked_off_early_does_not_make_the_reader_fast(self):
        # 600s over 12% of 100 KB is 12 KB read; calling it 100 KB says the
        # reader is 8x faster than they are, on every estimate.
        await self.item(1, text_size=100_000)
        await self.read_for(600, item_id=1)
        await self.reached(1, 12)
        await self.mark_read(1)
        self.assertAlmostEqual(await self.item_rate(), 600 / 12_000, delta=1e-6)

    async def test_a_book_read_to_the_end_pairs_with_its_whole_text(self):
        await self.item(1, text_size=100_000)
        await self.read_for(600, item_id=1)
        await self.reached(1, 100)
        await self.mark_read(1)
        self.assertAlmostEqual(await self.item_rate(), 600 / 100_000, delta=1e-9)

    async def test_a_book_never_opened_teaches_nothing(self):
        await self.item(1, text_size=100_000)
        await self.mark_read(1)
        self.assertIsNone(await self.item_rate())


class Novels(_Case):
    """The same declaration, on the other half of the app: 'Mark all as
    read' finishes every chapter of a volume at once."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url) VALUES (1, 'N', 'https://x/1')")
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (1, 1, 1)")
        for chapter_id in (1, 2, 3):
            await self.db.execute(
                "INSERT INTO chapters (id, novel_id, volume_id, number, title, url, content_html,"
                " content_size, order_index) VALUES (?, 1, 1, ?, 'C', ?, ?, LENGTH(?), ?)",
                (chapter_id, float(chapter_id), f"https://x/c{chapter_id}",
                 "x" * 10_000, "x" * 10_000, chapter_id))
        await self.db.commit()

    async def mark_volume_read(self):
        await set_volume_read_status(1, _JsonRequest({"read": True}), self.user, self.db)

    async def test_a_volume_ticked_off_after_one_chapter_is_not_a_finish(self):
        # Its chapters are all "read", but the only time recorded covers
        # one of the three -- so the sum is not how long that volume took.
        await self.read_for(600, chapter_id=1)
        await self.mark_volume_read()
        self.assertEqual((await self.volume_tile())["n"], 0)

    async def test_a_volume_read_all_the_way_through_is(self):
        for chapter_id in (1, 2, 3):
            await self.read_for(600, chapter_id=chapter_id)
        await self.mark_volume_read()
        tile = await self.volume_tile()
        self.assertEqual(tile["n"], 1)
        self.assertAlmostEqual(tile["avg_seconds"], 1800, delta=1)

    async def test_a_chapter_ticked_off_early_does_not_teach_the_novel_rate(self):
        await self.db.execute(
            "INSERT INTO novel_reading_progress (user_id, novel_id, chapter_id, scroll_percent,"
            " chapter_percent, updated_at) VALUES (1, 1, 1, 5, 10, datetime('now'))")
        await self.db.commit()
        await self.read_for(600, chapter_id=1)
        await self.mark_volume_read()
        est = await build_estimator(self.db, 1)
        # 600s over 10% of one 10 KB chapter, not over 10 KB.
        self.assertAlmostEqual(est.novel_rate, 600 / 1_000, delta=1e-6)

    async def test_marking_the_chapter_you_are_in_records_where_you_are(self):
        await self.db.execute(
            "INSERT INTO novel_reading_progress (user_id, novel_id, chapter_id, scroll_percent,"
            " chapter_percent, updated_at) VALUES (1, 1, 2, 50, 98, datetime('now'))")
        await self.db.commit()
        await set_read_status(2, _JsonRequest({"read": True}), self.user)
        row = await (await self.db.execute(
            "SELECT read_from_percent FROM novel_read_chapters WHERE chapter_id = 2")).fetchone()
        self.assertAlmostEqual(row["read_from_percent"], 98, delta=0.001)

    async def test_reading_out_of_a_chapter_records_its_end(self):
        # Paginated: the saved position is where the LAST page began (say
        # 75% of a 4-page chapter), and crossing into the next chapter is
        # the reader saying they read to the end -- which the reader sends.
        await self.db.execute(
            "INSERT INTO novel_reading_progress (user_id, novel_id, chapter_id, scroll_percent,"
            " chapter_percent, updated_at) VALUES (1, 1, 2, 50, 75, datetime('now'))")
        await self.db.commit()
        await set_read_status(2, _JsonRequest({"read": True, "read_from_percent": 100}), self.user)
        row = await (await self.db.execute(
            "SELECT read_from_percent FROM novel_read_chapters WHERE chapter_id = 2")).fetchone()
        self.assertEqual(row["read_from_percent"], 100)

    async def test_a_chapter_ticked_off_a_list_records_nothing_read(self):
        await set_read_status(3, _JsonRequest({"read": True}), self.user)
        row = await (await self.db.execute(
            "SELECT read_from_percent FROM novel_read_chapters WHERE chapter_id = 3")).fetchone()
        self.assertEqual(row["read_from_percent"], 0)
