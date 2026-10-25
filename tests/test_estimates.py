"""The "time left" estimate shown on the cards (app/stats/estimates.py).

The case that gets the most attention here is the one with no data: a fresh
install has never measured anything, and the estimate has to be absent
rather than invented -- there is nothing on a card to tell a reader which
of the two they are looking at.
"""
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.stats.estimates import (
    MIN_SAMPLE_FRACTION,
    MIN_SAMPLE_SECONDS,
    build_estimator,
    percentile,
)


class EstimateCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'L','/l','mixed')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def item(self, item_id, *, fmt="epub", size=1000, file_size=None,
                   series_id=None, pages=None):
        """`size` is the TEXT size and `pages` the page count -- an EPUB is
        scaled by the first and a comic by the second, and each is NULL on
        the format that has no such notion, exactly as the scanner writes
        them. file_size defaults to something deliberately unrelated, so a
        test that accidentally goes back to scaling by the file would
        produce visibly wrong numbers rather than quietly passing.

        `series_id` matters only for a comic, whose rate is learned per
        series -- the row is created on demand so a test can name one
        without a separate setup line."""
        if series_id is not None:
            await self.db.execute(
                "INSERT OR IGNORE INTO series (id, library_id, name, sort_name, folder_path)"
                " VALUES (?, 1, ?, ?, ?)",
                (series_id, f"S{series_id}", f"s{series_id}", f"/x/s{series_id}"))
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size, text_size, page_count)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, series_id, f"I{item_id}", f"i{item_id}", f"/x/{item_id}", fmt,
             f"h{item_id}", file_size if file_size is not None else 999_999,
             size if fmt == "epub" else None, pages))
        await self.db.commit()
        return await self.row("SELECT * FROM items WHERE id = ?", (item_id,))

    async def read_comic(self, item_id, *, series_id, pages, seconds, percent=100):
        """A comic volume read `percent` of the way through, with its time
        recorded -- one sample for that series' seconds-per-page rate."""
        item = await self.item(item_id, fmt="cbz", series_id=series_id, pages=pages)
        if percent >= 100:
            await self.db.execute(
                "INSERT INTO reading_progress (user_id, item_id, status, progress_percent,"
                " completed_at, completed_from_percent)"
                " VALUES (1, ?, 'completed', 100, datetime('now'), 100)", (item_id,))
            await self.db.commit()
        else:
            await self.progress(item_id, "reading", percent)
        await self.spend(seconds, item_id=item_id)
        return item

    async def row(self, sql, params=()):
        return await (await self.db.execute(sql, params)).fetchone()

    async def progress(self, item_id, status, percent, *, user_id=1):
        await self.db.execute(
            "INSERT OR REPLACE INTO reading_progress (user_id, item_id, status, progress_percent)"
            " VALUES (?, ?, ?, ?)", (user_id, item_id, status, percent))
        await self.db.commit()
        return await self.row(
            "SELECT * FROM reading_progress WHERE user_id = ? AND item_id = ?", (user_id, item_id))

    async def spend(self, seconds, *, item_id=None, novel_id=None, chapter_id=None, user_id=1, cap=180):
        """Records `seconds` of counted time, split into whole spans so the
        cap never truncates what the test meant to record."""
        remaining = seconds
        while remaining > 0:
            chunk = min(remaining, cap)
            await self.db.execute(
                "INSERT INTO page_reads (user_id, item_id, novel_id, chapter_id, kind, page_index,"
                " started_at, seconds, cap_seconds) VALUES (?,?,?,?,"
                " CASE WHEN ? IS NOT NULL THEN 'novel'"
                "      ELSE COALESCE((SELECT file_format FROM items WHERE id = ?), 'unknown') END,"
                " 0,datetime('now'),?,?)",
                (user_id, item_id, novel_id, chapter_id, chapter_id, item_id, chunk, cap))
            remaining -= chunk
        await self.db.commit()

    async def novel_chapter(self, chapter_id, *, novel_id=1, size=1000):
        """`size` is the byte length of content_html -- a novel's text size,
        the same column the reader weights progress by. size=None leaves the
        chapter undownloaded, which is a real state (the scraper lists a
        chapter before fetching it)."""
        await self.db.execute("INSERT OR IGNORE INTO novels (id,title,source_url) VALUES (?,'N',?)",
                              (novel_id, f"https://x/{novel_id}"))
        await self.db.execute("INSERT OR IGNORE INTO volumes (id,novel_id,number) VALUES (?,?,1)",
                              (novel_id, novel_id))
        await self.db.execute(
            "INSERT INTO chapters (id,novel_id,volume_id,number,title,url,content_html,content_size) "
            "VALUES (?,?,?,?,?,?,?,LENGTH(?))",
            (chapter_id, novel_id, novel_id, float(chapter_id), f"C{chapter_id}",
             f"https://x/c{chapter_id}", None if size is None else "x" * size,
             None if size is None else "x" * size))
        await self.db.commit()

    async def estimator(self, user_id=1):
        return await build_estimator(self.db, user_id)

    async def chapter_size(self, chapter_id):
        """content_size, which is exactly what _reading_items reads off the
        joined chapter row and hands to for_novel."""
        row = await self.row("SELECT content_size AS n FROM chapters WHERE id = ?",
                             (chapter_id,))
        return row["n"]

    async def finish_chapter(self, chapter_id, seconds, *, novel_id=1, user_id=1):
        """Read one chapter through: its time recorded, and marked read the
        way the reader marks it on crossing into the next one."""
        await self.spend(seconds, novel_id=novel_id, chapter_id=chapter_id, user_id=user_id)
        await self.db.execute(
            "INSERT OR IGNORE INTO novel_read_chapters (user_id, chapter_id) VALUES (?, ?)",
            (user_id, chapter_id))
        await self.db.commit()

    async def read_one_chapter_of_novel_one(self):
        """300s over one finished 1000-byte chapter -- the sample the novel
        rate is learned from, i.e. 0.3 s/byte."""
        await self.finish_chapter(1, 300)


class NothingMeasuredYet(EstimateCase):
    """What a fresh install looks like. Every path must answer None."""

    async def test_an_item_gets_no_estimate(self):
        item = await self.item(1)
        self.assertIsNone((await self.estimator()).for_item(item, None))

    async def test_an_item_being_read_gets_no_estimate(self):
        item = await self.item(1)
        progress = await self.progress(1, "reading", 40)
        self.assertIsNone((await self.estimator()).for_item(item, progress))

    async def test_a_novel_chapter_gets_no_estimate(self):
        await self.novel_chapter(1)
        est = await self.estimator()
        self.assertIsNone(est.for_novel(1))

    async def test_a_chapter_with_no_text_gets_no_estimate(self):
        self.assertIsNone((await self.estimator()).for_novel(99))


class AComicIsTimedPerSeries(EstimateCase):
    """Seconds per PAGE, learned inside one series and never across them.

    A comic carries no text to measure, so the only unit available is the
    page -- and a page is only comparable to another page of the SAME
    series. Lettering density varies by more than an order of magnitude
    between series, so a library-wide seconds-per-page would describe
    neither a wordless art book nor a dialogue-heavy one, and would be
    confidently wrong on every card of both.
    """

    async def test_an_unread_volume_is_scaled_by_its_own_series_rate(self):
        # 1000s over 100 pages read = 10 s/page; a 50-page volume of the
        # same series is therefore 500s.
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        unread = await self.item(2, fmt="cbz", series_id=1, pages=50)
        eta = (await self.estimator()).for_item(unread, None)
        self.assertAlmostEqual(eta.total, 500, places=4)
        self.assertAlmostEqual(eta.remaining, 500, places=4)

    async def test_one_series_rate_never_reaches_another_that_has_its_own(self):
        # The whole point of the scope. Series 2 is read at 1 s/page, and
        # series 1 at 10 s/page is slower -- the stand-in an unread series
        # would borrow -- but series 2 has a rate, so it keeps it.
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        await self.read_comic(2, series_id=2, pages=100, seconds=100)
        sibling = await self.item(3, fmt="cbz", series_id=2, pages=50)
        self.assertAlmostEqual(
            (await self.estimator()).for_item(sibling, None).total, 50, places=4)

    async def test_two_series_are_timed_at_their_own_speeds(self):
        # The case a global rate gets wrong: the same 50 pages, read at
        # 10 s/page in one series and 1 s/page in the other. A library-wide
        # average would hand both the same number and be off by 5x each way.
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        await self.read_comic(2, series_id=2, pages=100, seconds=100)
        est = await self.estimator()
        dense = est.for_item(await self.item(3, fmt="cbz", series_id=1, pages=50), None)
        sparse = est.for_item(await self.item(4, fmt="cbz", series_id=2, pages=50), None)
        self.assertAlmostEqual(dense.total, 500, places=4)
        self.assertAlmostEqual(sparse.total, 50, places=4)

    async def test_a_half_read_volume_sets_the_rate_from_the_half_it_covered(self):
        # 600s over 40% of a 100-page volume is 40 pages, so 15 s/page --
        # not 6. The fraction is on both sides of the division, the same
        # rule the text rate follows.
        await self.read_comic(1, series_id=1, pages=100, seconds=600, percent=40)
        unread = await self.item(2, fmt="cbz", series_id=1, pages=10)
        self.assertAlmostEqual(
            (await self.estimator()).for_item(unread, None).total, 150, places=4)

    async def test_a_book_in_the_same_series_does_not_feed_the_page_rate(self):
        # A mixed series is a real thing (a manga shelf with an art book in
        # it). What keeps the two rates apart is the SIZE COLUMN each one
        # divides by -- an EPUB has no page_count, so `page_count > 0`
        # drops it here, exactly as `text_size > 0` drops the comic from
        # the text rate. That guard is doing the work, and not the format
        # filter beside it: SUM() would skip the book's NULL pages while
        # still counting its 5000 seconds, which is how a rate gets
        # inflated by something it never measured.
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        await self.item(2, series_id=1, size=1000)
        await self.progress(2, "completed", 100)
        await self.spend(5000, item_id=2)
        unread = await self.item(3, fmt="cbz", series_id=1, pages=50)
        self.assertAlmostEqual(
            (await self.estimator()).for_item(unread, None).total, 500, places=4)

    async def test_a_volume_whose_pages_were_never_counted_does_not_inflate_it(self):
        # The same guard from the comic side: a volume whose archive would
        # not list has seconds but no pages, and SUM() skipping its NULL
        # would leave those seconds divided by somebody else's pages.
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        await self.read_comic(2, series_id=1, pages=None, seconds=5000)
        unread = await self.item(3, fmt="cbz", series_id=1, pages=50)
        self.assertAlmostEqual(
            (await self.estimator()).for_item(unread, None).total, 500, places=4)

    async def test_a_loose_volume_with_a_series_read_borrows_the_stand_in(self):
        # No series means no rate of its own ever, so the stand-in is all
        # a loose volume can have -- and it does have that.
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        loose = await self.item(2, fmt="cbz", series_id=None, pages=50)
        self.assertAlmostEqual(
            (await self.estimator()).for_item(loose, None).total, 500, places=4)

    async def test_a_loose_volume_is_not_a_series_other_loose_volumes_share(self):
        # series_id is nullable (see items in schema.sql). Grouping by it
        # without excluding NULL makes "no series" one bucket, so time
        # spent on any unfiled volume would size every other unfiled volume
        # in the library -- which is the global rate this whole scope
        # exists to avoid, arrived at by accident. The read volume here is
        # itself unfiled, which is what makes that bucket exist at all.
        await self.read_comic(1, series_id=None, pages=100, seconds=1000)
        loose = await self.item(2, fmt="cbz", series_id=None, pages=50)
        self.assertIsNone((await self.estimator()).for_item(loose, None))

    async def test_a_comic_whose_pages_were_never_counted_gets_no_estimate(self):
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        sizeless = await self.item(2, fmt="cbz", series_id=1, pages=None)
        self.assertIsNone((await self.estimator()).for_item(sizeless, None))

    async def test_a_volume_barely_opened_does_not_set_the_rate(self):
        # The same floors as everything else here: a couple of minutes
        # against 1% of a volume divides seconds by almost no pages.
        await self.read_comic(1, series_id=1, pages=100, seconds=600, percent=1)
        unread = await self.item(2, fmt="cbz", series_id=1, pages=50)
        self.assertIsNone((await self.estimator()).for_item(unread, None))

    async def test_a_volume_being_read_is_projected_from_its_own_pace(self):
        # The series rate says 10 s/page, so this 200-page volume "should"
        # take 2000s -- but the reader is 50% into it having spent 600s,
        # and what THIS volume is actually doing outranks what the series
        # usually does. 1200s projected, 600 of them left. Sized so the two
        # paths cannot agree by accident: read at the series rate the
        # answer would be 2000.
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        item = await self.read_comic(2, series_id=1, pages=200, seconds=600, percent=50)
        progress = await self.row(
            "SELECT * FROM reading_progress WHERE user_id = 1 AND item_id = 2")
        eta = (await self.estimator()).for_item(item, progress)
        self.assertAlmostEqual(eta.total, 1200, places=4)
        self.assertAlmostEqual(eta.remaining, 600, places=4)

    async def test_a_finished_volume_has_nothing_left_to_estimate(self):
        item = await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        progress = await self.row(
            "SELECT * FROM reading_progress WHERE user_id = 1 AND item_id = 1")
        self.assertIsNone((await self.estimator()).for_item(item, progress))

    async def test_the_rate_is_per_reader(self):
        # page_reads belongs to a user, so one reader's pace through a
        # series must not size another's cards.
        await self.db.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (2,'v','x')")
        await self.db.commit()
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        unread = await self.item(2, fmt="cbz", series_id=1, pages=50)
        self.assertIsNone((await self.estimator(user_id=2)).for_item(unread, None))


class AnUnreadComicSeriesBorrowsTheSlowEnd(EstimateCase):
    """A comic series with no rate of its own is timed at the 95th
    percentile of the reader's series rates, until it has one.

    Four series read at 1, 2, 3 and 11 s/page make the stand-in
    3 + 0.85 * (11 - 3) = 9.8 s/page: interpolated between the two
    slowest, and nowhere near the mean (4.25) or the median (2.5). Sized
    so each of those wrong answers is a different number.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for series_id, seconds in ((1, 100), (2, 200), (3, 300), (4, 1100)):
            await self.read_comic(series_id, series_id=series_id, pages=100, seconds=seconds)

    async def test_an_unread_series_gets_the_95th_percentile(self):
        stranger = await self.item(10, fmt="cbz", series_id=9, pages=50)
        eta = (await self.estimator()).for_item(stranger, None)
        self.assertAlmostEqual(eta.total, 9.8 * 50, places=4)
        self.assertAlmostEqual(eta.remaining, 9.8 * 50, places=4)

    async def test_it_counts_each_series_once_however_much_of_it_was_read(self):
        # Ten more volumes of the FASTEST series, each at 1 s/page. Counted
        # per volume they would drag the stand-in down to the fast end;
        # counted per series they change nothing, which is the point.
        for n in range(10):
            await self.read_comic(20 + n, series_id=1, pages=100, seconds=100)
        stranger = await self.item(10, fmt="cbz", series_id=9, pages=50)
        self.assertAlmostEqual(
            (await self.estimator()).for_item(stranger, None).total, 9.8 * 50, places=4)

    async def test_a_volume_opened_but_not_yet_a_sample_still_borrows(self):
        # 2% in and 30s spent is under both floors: not its own pace, and
        # not a series rate yet either. So still the stand-in, scaled by
        # what is left.
        started = await self.read_comic(10, series_id=9, pages=50, seconds=30, percent=2)
        progress = await self.row(
            "SELECT * FROM reading_progress WHERE user_id = 1 AND item_id = 10")
        eta = (await self.estimator()).for_item(started, progress)
        self.assertAlmostEqual(eta.total, 9.8 * 50, places=4)
        self.assertAlmostEqual(eta.remaining, 9.8 * 50 * 0.98, places=4)

    async def test_the_first_real_sample_replaces_it_with_the_series_own(self):
        # One volume of series 9 read at 5 s/page: every other volume in
        # it switches from the borrowed 9.8 to that.
        await self.read_comic(10, series_id=9, pages=100, seconds=500)
        unread = await self.item(11, fmt="cbz", series_id=9, pages=50)
        self.assertAlmostEqual(
            (await self.estimator()).for_item(unread, None).total, 250, places=4)

    async def test_the_whole_shelf_is_summed_at_it(self):
        items = [await self.item(10 + n, fmt="cbz", series_id=9, pages=50) for n in range(3)]
        rows = [await self.row(
            "SELECT items.*, NULL AS status, NULL AS progress_percent FROM items WHERE id = ?",
            (item["id"],)) for item in items]
        self.assertAlmostEqual(
            (await self.estimator()).total_remaining(rows), 3 * 9.8 * 50, places=4)

    async def test_it_is_per_reader(self):
        await self.db.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (2,'v','x')")
        await self.db.commit()
        stranger = await self.item(10, fmt="cbz", series_id=9, pages=50)
        self.assertIsNone((await self.estimator(user_id=2)).for_item(stranger, None))

    async def test_a_book_is_untouched_by_it(self):
        # The stand-in is seconds per PAGE; a book has no pages and no
        # text rate here, so it must stay blank rather than borrow it.
        book = await self.item(10, size=1000, series_id=9)
        self.assertIsNone((await self.estimator()).for_item(book, None))


class Percentile(unittest.TestCase):
    def test_nothing_to_take_it_of(self):
        self.assertIsNone(percentile([], 0.95))

    def test_one_sample_is_itself(self):
        self.assertEqual(percentile([7.0], 0.95), 7.0)

    def test_it_interpolates_between_the_two_either_side(self):
        # (2 - 1) * 0.95 = 0.95 of the way from 10 to 20.
        self.assertAlmostEqual(percentile([20.0, 10.0], 0.95), 19.5)

    def test_the_ends(self):
        self.assertEqual(percentile([3.0, 1.0, 2.0], 0), 1.0)
        self.assertEqual(percentile([3.0, 1.0, 2.0], 1), 3.0)


class WhichItemsCouldEverCarryAnEstimate(EstimateCase):
    """can_estimate, which is what the series page's sort control asks.

    It must not consult the estimate itself: that is absent for an item
    already finished, so the control would appear and disappear as the
    series is read. For a comic it MUST consult the rate, because the rate
    is per series, and with no series rate anywhere there is not even a
    stand-in to borrow.
    """

    async def test_a_book_does_not_qualify_with_no_book_rate_yet(self):
        # The comic rule, for a book: with no ebook rate anywhere an unstarted
        # book has nothing to be ordered by, and the control offered an
        # order that ordered nothing.
        self.assertFalse((await self.estimator()).can_estimate(await self.item(1)))

    async def test_a_comic_series_nobody_has_read_does_not_qualify(self):
        comic = await self.item(1, fmt="cbz", series_id=1, pages=50)
        self.assertFalse((await self.estimator()).can_estimate(comic))

    async def test_it_does_once_another_series_has_a_rate_to_borrow(self):
        await self.read_comic(1, series_id=2, pages=100, seconds=1000)
        comic = await self.item(2, fmt="cbz", series_id=1, pages=50)
        self.assertTrue((await self.estimator()).can_estimate(comic))

    async def test_a_comic_series_with_a_rate_does(self):
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        comic = await self.item(2, fmt="cbz", series_id=1, pages=50)
        self.assertTrue((await self.estimator()).can_estimate(comic))

    async def test_a_finished_comic_of_a_timed_series_still_qualifies(self):
        item = await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        self.assertTrue((await self.estimator()).can_estimate(item))


class EstimateShape(EstimateCase):
    async def test_the_fields_survive_template_attribute_lookup(self):
        # The templates read eta.remaining and eta.total. A dict would put
        # `.items`-style collisions back on the table (see the statistics
        # page's finish.items bug); a namedtuple cannot have them.
        await self.read_comic(1, series_id=1, pages=100, seconds=1000)
        item = await self.item(2, fmt='cbz', series_id=1, pages=100)
        eta = (await self.estimator()).for_item(item, None)
        self.assertEqual(eta._fields, ("remaining", "total", "rest"))
        for field in eta._fields:
            self.assertFalse(hasattr({}, field))


if __name__ == "__main__":
    unittest.main()
