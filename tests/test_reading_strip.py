"""The home page's "Reading" strip, and the order its cards are in.

That order is not decoration. `library-events.js` moves a card to the
FRONT of the strip when a progress event names it, with no refetch at all
-- which is only correct because this list is sorted by the very column
the progress writers set to `now`. So the cases below go through the real
handlers rather than writing the rows themselves: what they pin is the
COUPLING between the two halves, and a test that issued its own UPDATE
would keep passing while the pair drifted apart.
"""
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import _reading_items
from app.novels.router import save_novel_progress
from app.reader.router import save_progress

APP = Path(__file__).resolve().parent.parent / "app"


class _JsonRequest:
    """Enough of a Request for a handler that only reads its body."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class ReadingStripCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        self.user = {"id": 1}
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'M', '/m', 'comic')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'S', 's', '/f')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def item(self, item_id, title, *, hours_ago):
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size, page_count) VALUES (?, 1, 1, ?, ?, ?, 'cbz', ?, 1, 100)",
            (item_id, title, title.lower(), f"/{title}", f"h{item_id}"))
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, current_page, progress_percent,"
            " last_read_at) VALUES (1, ?, 'reading', 10, 10, datetime('now', ?))",
            (item_id, f"-{hours_ago} hours"))
        await self.db.commit()

    async def novel(self, novel_id, title, chapter_id, *, hours_ago):
        await self.db.execute("INSERT INTO novels (id, title, source_url) VALUES (?, ?, ?)",
                              (novel_id, title, f"https://x/{novel_id}"))
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (?, ?, 1)",
                              (novel_id * 100, novel_id))
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url, content_html,"
            " content_size) VALUES (?, ?, ?, 1.0, 'C', ?, '<p>x</p>', LENGTH('<p>x</p>'))",
            (chapter_id, novel_id, novel_id * 100, f"https://x/c{chapter_id}"))
        await self.db.execute(
            "INSERT INTO novel_reading_progress (user_id, novel_id, chapter_id, scroll_percent,"
            " chapter_percent, updated_at) VALUES (1, ?, ?, 10, 10, datetime('now', ?))",
            (novel_id, chapter_id, f"-{hours_ago} hours"))
        await self.db.commit()

    async def titles(self):
        return [r["title"] for r in await _reading_items(self.db, 1)]


class Ordering(ReadingStripCase):
    async def test_it_is_most_recently_read_first(self):
        # Deliberately against the id order: the oldest read has the LOWEST
        # id here, so a list that came out right by accident of insertion
        # order fails this.
        await self.item(1, "D", hours_ago=3)
        await self.item(2, "C", hours_ago=2)
        await self.item(3, "B", hours_ago=1)
        self.assertEqual(await self.titles(), ["B", "C", "D"])

    async def test_saving_progress_moves_an_item_to_the_front(self):
        # The property library-events.js relies on when it promotes a card
        # with no refetch: the writer behind a progress event sets exactly
        # the column this list is sorted by, so the event's arrival IS the
        # new position.
        await self.item(1, "B", hours_ago=1)
        await self.item(2, "C", hours_ago=2)
        await self.item(3, "D", hours_ago=3)
        await save_progress(3, _JsonRequest(
            {"current_page": 77, "progress_percent": 77, "status": "reading"}), self.user, self.db)
        self.assertEqual(await self.titles(), ["D", "B", "C"])

    async def test_two_saves_in_one_second_both_pass_what_was_not_touched(self):
        # What is actually guaranteed, and no more. `last_read_at` is
        # `datetime('now')`, which is second-granular, so two saves inside
        # the same second TIE and nothing orders them against each other --
        # measured, not assumed. Asserting a winner between them would be a
        # test that passes on the stable sort's input order rather than on
        # anything the code promises.
        await self.item(1, "B", hours_ago=1)
        await self.item(2, "C", hours_ago=2)
        await self.item(3, "D", hours_ago=3)
        body = {"current_page": 20, "progress_percent": 20, "status": "reading"}
        await save_progress(3, _JsonRequest(body), self.user, self.db)
        await save_progress(2, _JsonRequest(body), self.user, self.db)
        self.assertEqual((await self.titles())[-1], "B")
        self.assertEqual(sorted((await self.titles())[:2]), ["C", "D"])

    async def test_a_novel_is_ordered_on_the_same_clock_as_an_item(self):
        # The two content types come from different tables and different
        # timestamp columns; the strip interleaves them, so both columns
        # have to mean the same thing.
        await self.item(1, "B", hours_ago=2)
        await self.novel(1, "N", 501, hours_ago=1)
        self.assertEqual(await self.titles(), ["N", "B"])

    async def test_saving_a_novel_position_moves_it_to_the_front(self):
        await self.item(1, "B", hours_ago=1)
        await self.novel(1, "N", 501, hours_ago=3)
        await save_novel_progress(1, _JsonRequest(
            {"chapter_id": 501, "scroll_percent": 40, "chapter_percent": 40}), self.user, self.db)
        self.assertEqual(await self.titles(), ["N", "B"])


class TheCardCarriesItsEstimate(ReadingStripCase):
    """A comic on this strip is timed by the page at its own series' rate
    (see stats/estimates.py), and the estimator asks the ROW for the
    series it belongs to. The strip builds that row from a hand-written
    column list, so a column the estimator needs and the SELECT does not
    name is a crash on the home page -- for comics only, which is the half
    of the library the ebook tests never touch."""

    async def _time_a_sibling_volume(self):
        # Volume 9 of the same series, read through: 1800s over 100 pages.
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size, page_count)"
            " VALUES (9, 1, 1, 'v9', 'v9', '/v9', 'cbz', 'h9', 1, 100)")
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent,"
            " completed_at, completed_from_percent)"
            " VALUES (1, 9, 'completed', 100, datetime('now'), 100)")
        for _ in range(10):
            await self.db.execute(
                "INSERT INTO page_reads (user_id, item_id, kind, page_index, started_at,"
                " seconds, cap_seconds) VALUES (1, 9, 'cbz', 0, datetime('now'), 180, 180)")
        await self.db.commit()

    async def test_a_comic_shows_one_once_its_series_has_a_rate(self):
        await self.item(1, "A", hours_ago=1)
        await self._time_a_sibling_volume()
        entry, = [e for e in await _reading_items(self.db, 1) if e["kind"] == "item"]
        # 18 s/page over a 100-page volume is 1800s, of which 90% is left.
        self.assertAlmostEqual(entry["eta"].total, 1800, places=4)
        self.assertAlmostEqual(entry["eta"].remaining, 1620, places=4)

    async def test_and_none_while_nothing_in_that_series_has_been_timed(self):
        await self.item(1, "A", hours_ago=1)
        entry, = [e for e in await _reading_items(self.db, 1) if e["kind"] == "item"]
        self.assertIsNone(entry["eta"])


class ANovelCardsTimeIsItsVolumes(ReadingStripCase):
    """The card's percentage is the CHAPTER's and its time is the VOLUME's,
    which is what the reader is deciding about: "have I got an evening for
    this" is a question about a volume, the unit a novel is followed in.

    The sum that answers it lives in _reading_items' SQL rather than in the
    estimator -- which chapters of the volume are still ahead is a question
    about novel_read_chapters, not about reading speed -- so these drive
    the real strip rather than the estimator directly. Every size in them
    is chosen so the arithmetic is checkable by hand: the rate is 0.3 s per
    byte throughout (300 seconds over one finished 1000-byte chapter).
    """

    async def volume_of(self, sizes, *, current, percent=40):
        """A novel whose one volume holds `sizes` -- {chapter_id: bytes},
        with None for a chapter whose body has not been downloaded.

        Chapter 501 is read and timed, which is where the rate comes from.
        """
        await self.db.execute("INSERT INTO novels (id, title, source_url) VALUES (1,'N','https://x/1')")
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (100, 1, 1)")
        for chapter_id, size in sizes.items():
            await self.db.execute(
                "INSERT INTO chapters (id, novel_id, volume_id, number, title, url, content_html,"
                " content_size) VALUES (?, 1, 100, ?, ?, ?, ?, ?)",
                (chapter_id, float(chapter_id), f"C{chapter_id}", f"https://x/c{chapter_id}",
                 None if size is None else "x" * size, size))
        await self.db.execute(
            "INSERT INTO novel_reading_progress (user_id, novel_id, chapter_id, scroll_percent,"
            " chapter_percent, updated_at) VALUES (1, 1, ?, ?, ?, datetime('now'))",
            (current, percent, percent))
        await self.db.commit()

    async def mark_read(self, *chapter_ids):
        for chapter_id in chapter_ids:
            await self.db.execute(
                "INSERT OR IGNORE INTO novel_read_chapters (user_id, chapter_id, read_from_percent)"
                " VALUES (1, ?, 100)", (chapter_id,))
        await self.db.commit()

    async def time_it(self, chapter_id, seconds):
        await self.db.execute(
            "INSERT INTO page_reads (user_id, novel_id, chapter_id, kind, page_index, started_at,"
            " seconds, cap_seconds) VALUES (1, 1, ?, 'novel', 0, datetime('now'), ?, 100000)",
            (chapter_id, seconds))
        await self.db.commit()

    async def learn_the_rate(self):
        """300 seconds over the finished 1000-byte chapter 501: 0.3 s/byte."""
        await self.mark_read(501)
        await self.time_it(501, 300)

    async def estimator_rate(self):
        from app.stats.estimates import build_estimator
        return (await build_estimator(self.db, 1)).novel_rate

    async def card(self):
        entry, = [e for e in await _reading_items(self.db, 1) if e["kind"] == "novel"]
        return entry

    async def test_the_time_covers_every_chapter_of_the_volume_still_unread(self):
        # 501 read, 502 in hand at 40%, 503 and 504 ahead. At 0.3 s/byte:
        # the two ahead are 2000 bytes = 600s, and 60% of the 1000-byte
        # chapter in hand is another 180s.
        await self.volume_of({501: 1000, 502: 1000, 503: 1000, 504: 1000}, current=502)
        await self.learn_the_rate()
        eta = (await self.card())["eta"]
        self.assertAlmostEqual(eta.rest, 600, places=4)
        self.assertAlmostEqual(eta.total, 300, places=4)
        self.assertAlmostEqual(eta.remaining, 780, places=4)

    async def test_a_chapter_already_marked_read_is_not_counted(self):
        await self.volume_of({501: 1000, 502: 1000, 503: 1000, 504: 1000}, current=502)
        await self.learn_the_rate()
        await self.mark_read(503)
        eta = (await self.card())["eta"]
        self.assertAlmostEqual(eta.rest, 300, places=4)      # 504 alone
        self.assertAlmostEqual(eta.remaining, 480, places=4)

    async def test_a_chapter_skipped_past_is_still_ahead(self):
        # "Ahead" is NOT MARKED READ, not "later in the volume". A chapter
        # jumped over is still one this volume needs before it is done, and
        # marking it read is how the reader says otherwise -- counting by
        # position instead would quietly forgive it.
        await self.volume_of({501: 1000, 502: 1000, 503: 1000}, current=503)
        await self.learn_the_rate()
        eta = (await self.card())["eta"]
        self.assertAlmostEqual(eta.rest, 300, places=4)      # 502, before the position
        self.assertAlmostEqual(eta.remaining, 480, places=4)

    async def test_a_chapter_not_downloaded_yet_counts_as_the_volumes_average(self):
        # It has no length to measure, so it takes the average of the ones
        # that do -- here 1000 bytes, the same as its siblings. Leaving it
        # out would understate the volume by exactly one chapter, silently,
        # and a number wrong downwards looks identical to a right one.
        await self.volume_of({501: 1000, 502: 1000, 503: None}, current=502)
        await self.learn_the_rate()
        eta = (await self.card())["eta"]
        self.assertAlmostEqual(eta.rest, 300, places=4)

    async def test_the_average_is_the_volumes_own_and_not_a_flat_guess(self):
        # Same shape, but this volume's chapters run 3000 bytes. The
        # unmeasured one has to follow THEM.
        await self.volume_of({501: 3000, 502: 3000, 503: None}, current=502)
        await self.mark_read(501)
        await self.time_it(501, 900)                          # 900/3000 = 0.3 s/byte
        eta = (await self.card())["eta"]
        self.assertAlmostEqual(eta.rest, 900, places=4)       # 3000 bytes at 0.3

    async def test_the_chapter_in_hand_is_not_counted_twice(self):
        # It is the half the percentage measures, so it belongs to `total`
        # and must be out of `rest` -- otherwise every card overstates by
        # a whole chapter.
        await self.volume_of({501: 1000, 502: 1000}, current=502)
        await self.learn_the_rate()
        eta = (await self.card())["eta"]
        self.assertEqual(eta.rest, 0)
        self.assertAlmostEqual(eta.remaining, 180, places=4)

    async def test_another_volume_of_the_same_novel_stays_out_of_it(self):
        await self.volume_of({501: 1000, 502: 1000}, current=502)
        await self.learn_the_rate()
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (200, 1, 2)")
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url, content_html,"
            " content_size) VALUES (601, 1, 200, 601.0, 'C', 'https://x/c601', 'x', 5000)")
        await self.db.commit()
        self.assertEqual((await self.card())["eta"].rest, 0)

    async def test_the_split_is_what_the_percentage_may_move(self):
        # library-events.js recomputes `rest + total * (1 - percent)` when a
        # progress event arrives. The percentage it carries is the
        # CHAPTER's, so `rest` must not move with it -- and the server's own
        # number has to be that same formula, or a card would change the
        # moment it updated live.
        await self.volume_of({501: 1000, 502: 1000, 503: 1000}, current=502, percent=40)
        await self.learn_the_rate()
        first = (await self.card())["eta"]
        await self.db.execute(
            "UPDATE novel_reading_progress SET chapter_percent = 80 WHERE user_id = 1")
        await self.db.commit()
        second = (await self.card())["eta"]
        self.assertEqual(first.rest, second.rest)
        self.assertEqual(first.total, second.total)
        self.assertAlmostEqual(second.remaining, second.rest + second.total * 0.2, places=6)

    async def test_a_finished_chapter_still_shows_the_volume_behind_it(self):
        # Where this differs from the per-chapter estimate it replaced,
        # which answered None at 100% -- on a card that still had two
        # unread chapters' worth of volume left to describe.
        await self.volume_of({501: 1000, 502: 1000, 503: 1000}, current=502, percent=100)
        await self.learn_the_rate()
        eta = (await self.card())["eta"]
        self.assertAlmostEqual(eta.remaining, 300, places=4)

    async def test_a_volume_read_to_the_end_has_no_time_left(self):
        # The chapter in hand is kept out of the volume's remaining sum by
        # ID, so nothing else would notice it had been ticked off -- and a
        # volume with every chapter read went on claiming that chapter's
        # whole time. Reproduced in a browser against a real novel: "about
        # 30m left in volume 3", with all four chapters marked read.
        await self.volume_of({501: 1000, 502: 1000, 503: 1000}, current=502)
        await self.learn_the_rate()
        await self.mark_read(502, 503)
        self.assertIsNone((await self.card())["eta"])

    async def test_the_chapter_in_hand_stops_counting_once_it_is_ticked_off(self):
        # The same rule with the rest of the volume still unread: what is
        # left is what is BEHIND it, and the read chapter adds nothing.
        await self.volume_of({501: 1000, 502: 1000, 503: 1000}, current=502)
        await self.learn_the_rate()
        await self.mark_read(502)
        eta = (await self.card())["eta"]
        self.assertEqual(eta.total, 0)
        self.assertAlmostEqual(eta.remaining, 300, places=4)     # chapter 503 alone

    async def test_a_ticked_off_chapter_does_not_come_back_through_its_own_pace(self):
        # Own pace is the one path that does not consult the chapter's
        # size, so a chapter with real time logged against it would have
        # projected its whole total straight past the rule above -- here,
        # 900s at 30% would have claimed 3000s for a chapter already read.
        #
        # Note what those 900 seconds legitimately DO change: chapter 502
        # is now a second sample of the rate, so it is 1200s over 2000
        # bytes -- 0.6 s/byte, not 0.3 -- and chapter 503's 1000 bytes come
        # to 600s. That is the rate doing its job, not the chapter leaking
        # back in; what would signal the leak is `total`.
        await self.volume_of({501: 1000, 502: 1000, 503: 1000}, current=502, percent=30)
        await self.learn_the_rate()
        await self.time_it(502, 900)
        await self.mark_read(502)
        est = await self.estimator_rate()
        self.assertAlmostEqual(est, 0.6, places=6)
        eta = (await self.card())["eta"]
        self.assertEqual(eta.total, 0)
        self.assertAlmostEqual(eta.remaining, 600, places=4)     # chapter 503 alone, at 0.6

    async def test_the_card_names_the_volume_the_time_is_about(self):
        # The percentage beside it measures the chapter, so the one thing
        # the line must not do is leave the reader guessing which span the
        # time belongs to. _reading_grid.html puts this in the tooltip.
        await self.volume_of({501: 1000, 502: 1000, 503: 1000}, current=502)
        await self.learn_the_rate()
        card = await self.card()
        self.assertEqual(card["eta_scope"], "volume 1")
        self.assertEqual(card["chapter_label"], "Vol. 1 · Ch. 502")

    async def test_an_item_card_claims_no_scope(self):
        # Its estimate is about the item the card already names. The
        # template and library-events.js both key the "in ..." clause off
        # this being empty.
        await self.item(1, "A", hours_ago=1)
        entry, = [e for e in await _reading_items(self.db, 1) if e["kind"] == "item"]
        self.assertEqual(entry["eta_scope"], "")

    def test_the_tooltip_reads_the_same_before_and_after_a_live_update(self):
        # The card is rendered by _reading_grid.html and then RE-rendered
        # in place by library-events.js when a progress event arrives, so
        # the two write the same sentence -- the same coupling
        # formatDuration already carries. A card that silently reworded
        # itself the first time the reader scrolled would be the kind of
        # bug nobody reports and everybody notices.
        card = (APP / "templates/library/_reading_grid.html").read_text()
        js = (APP / "static/js/library-events.js").read_text()
        for fragment in ("About ", " left", " in ", ", going by how fast you read"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, card)
                self.assertIn(fragment, js)
        # And both hang the "in ..." clause off the same empty-or-not
        # value, which is what lets an item card omit it entirely.
        self.assertIn("entry.eta_scope", card)
        self.assertIn("dataset.etaScope", js)

    async def test_no_rate_yet_means_no_time_at_all(self):
        # A novel nobody has finished a chapter of has nothing to scale by,
        # and a guessed number is worse than none (see stats/estimates.py).
        await self.volume_of({501: 1000, 502: 1000, 503: 1000}, current=502)
        self.assertIsNone((await self.card())["eta"])


if __name__ == "__main__":
    unittest.main()
