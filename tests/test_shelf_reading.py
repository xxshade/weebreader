"""The two numbers a home-grid card carries about reading.

The badge over the cover counts what is still UNREAD, and the line under
the name says how long that will take. They are one question asked twice,
so they are built in one pass and cannot disagree about what "unread"
means (see `_series_reading` / `_novel_reading` in library/router.py).

Every size here is picked so the arithmetic is checkable by hand. A comic
series learns 10 seconds per page from one 100-page volume read in 1000s;
a novel learns 0.3 seconds per byte from one 1000-byte chapter read in
300s. Nothing below relies on a number this file did not set.
"""
import re
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import _novel_reading, _series_reading
from app.stats.estimates import build_estimator

APP = Path(__file__).resolve().parent.parent / "app"


def _strip(source: str) -> str:
    """Blanks Jinja `{# ... #}` blocks and whole-line `//` comments.

    The source scans below are looking for CODE. Leaving the prose in
    means a comment explaining a rule reads as a breach of it -- which is
    exactly what happened here, on the line that says "NOT .reading-eta".
    """
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    return "\n".join("" if line.lstrip().startswith("//") else line
                      for line in source.splitlines())


def _uncommented(path: Path) -> str:
    return _strip(path.read_text())


class ShelfCase(unittest.IsolatedAsyncioTestCase):
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
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'L','/l','comic')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'S', 's', '/f')")
        await self.db.commit()

    # ---- fixtures -------------------------------------------------------

    async def comic(self, item_id, *, pages=100, series_id=1):
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size, page_count)"
            " VALUES (?, 1, ?, ?, ?, ?, 'cbz', ?, 999999, ?)",
            (item_id, series_id, f"V{item_id}", f"v{item_id}", f"/f/{item_id}",
             f"h{item_id}", pages))
        await self.db.commit()

    async def finished(self, item_id, *, from_percent=100):
        await self.db.execute(
            "INSERT OR REPLACE INTO reading_progress (user_id, item_id, status, progress_percent,"
            " completed_at, completed_from_percent)"
            " VALUES (1, ?, 'completed', 100, datetime('now'), ?)", (item_id, from_percent))
        await self.db.commit()

    async def partway(self, item_id, percent):
        await self.db.execute(
            "INSERT OR REPLACE INTO reading_progress (user_id, item_id, status, progress_percent)"
            " VALUES (1, ?, 'reading', ?)", (item_id, percent))
        await self.db.commit()

    async def spend(self, seconds, *, item_id=None, chapter_id=None, novel_id=None):
        """Whole spans of 100s under a 100,000s cap, so the per-row cap
        never truncates what the test meant to record (see page_reads)."""
        kind = "novel" if chapter_id else "cbz"
        left = seconds
        while left > 0:
            chunk = min(left, 100)
            await self.db.execute(
                "INSERT INTO page_reads (user_id, item_id, novel_id, chapter_id, kind, page_index,"
                " started_at, seconds, cap_seconds) VALUES (1,?,?,?,?,0,datetime('now'),?,100000)",
                (item_id, novel_id, chapter_id, kind, chunk))
            left -= chunk
        await self.db.commit()

    async def learn_the_comic_rate(self):
        """One 100-page volume finished in 1000 seconds: 10 s/page in this
        series. Item 9 is a fixture, never asserted about directly."""
        await self.comic(9, pages=100)
        await self.finished(9)
        await self.spend(1000, item_id=9)

    async def chapter(self, chapter_id, *, volume=1, size=1000, novel_id=1):
        await self.db.execute("INSERT OR IGNORE INTO novels (id,title,source_url)"
                              " VALUES (?,'N',?)", (novel_id, f"https://x/{novel_id}"))
        await self.db.execute("INSERT OR IGNORE INTO volumes (id,novel_id,number)"
                              " VALUES (?,?,?)", (novel_id * 100 + volume, novel_id, volume))
        await self.db.execute(
            "INSERT INTO chapters (id,novel_id,volume_id,number,title,url,content_html,content_size)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (chapter_id, novel_id, novel_id * 100 + volume, float(chapter_id), f"C{chapter_id}",
             f"https://x/c{chapter_id}", None if size is None else "x" * size, size))
        await self.db.commit()

    async def mark_read(self, *chapter_ids):
        for chapter_id in chapter_ids:
            await self.db.execute(
                "INSERT OR IGNORE INTO novel_read_chapters (user_id, chapter_id, read_from_percent)"
                " VALUES (1, ?, 100)", (chapter_id,))
        await self.db.commit()

    async def open_at(self, chapter_id, percent, *, novel_id=1):
        await self.db.execute(
            "INSERT OR REPLACE INTO novel_reading_progress (user_id, novel_id, chapter_id,"
            " scroll_percent, chapter_percent) VALUES (1, ?, ?, 0, ?)",
            (novel_id, chapter_id, percent))
        await self.db.commit()

    async def learn_the_novel_rate(self):
        """300 seconds over a finished 1000-byte chapter: 0.3 s/byte.
        Chapter 901 sits in a volume of its own so it never lands in a
        count under test."""
        await self.chapter(901, volume=9)
        await self.mark_read(901)
        await self.spend(300, chapter_id=901, novel_id=1)

    # ---- readers --------------------------------------------------------

    async def series(self, series_id=1):
        estimator = await build_estimator(self.db, 1)
        return (await _series_reading(self.db, 1, estimator)).get(series_id)

    async def novel(self, novel_id=1):
        estimator = await build_estimator(self.db, 1)
        return (await _novel_reading(self.db, 1, estimator)).get(novel_id)

    async def comic_rate(self):
        return (await build_estimator(self.db, 1)).rate_by_series.get(1)

    async def novel_rate(self):
        return (await build_estimator(self.db, 1)).novel_rate


class TheBadgeCountsWhatIsLeft(ShelfCase):
    """It used to count what the shelf HOLDS, which answered a different
    question from the one in front of a cover grid: "12" on a series
    finished last year said nothing about whether there was anything in
    it."""

    async def test_a_finished_item_is_not_counted(self):
        await self.comic(1)
        await self.comic(2)
        await self.finished(1)
        self.assertEqual((await self.series()).unread, 1)

    async def test_an_item_never_opened_is_counted(self):
        # No reading_progress row at all -- the LEFT JOIN's NULL status is
        # what has to read as "unread", not as a missing row to skip.
        await self.comic(1)
        self.assertEqual((await self.series()).unread, 1)

    async def test_an_item_abandoned_partway_is_still_one_to_read(self):
        # Unread is NOT COMPLETED. A volume at 12% is one the reader still
        # has in front of them, and the time beside this number counts 88%
        # of it for exactly the same reason.
        await self.comic(1)
        await self.partway(1, 12)
        self.assertEqual((await self.series()).unread, 1)

    async def test_a_series_read_to_the_end_has_no_badge_and_no_time(self):
        # Both numbers go at once, which is the whole point of building
        # them together: a card with nothing left has nothing to say.
        await self.comic(1)
        await self.finished(1)
        self.assertIsNone(await self.series())

    async def test_marking_the_last_one_read_empties_it(self):
        await self.comic(1)
        await self.comic(2)
        await self.finished(1)
        self.assertEqual((await self.series()).unread, 1)
        await self.finished(2)
        self.assertIsNone(await self.series())


class TheTimeIsEverythingUnread(ShelfCase):
    async def test_it_adds_the_unread_volumes_up(self):
        # 10 s/page over two untouched 100-page volumes.
        await self.learn_the_comic_rate()
        self.assertEqual(await self.comic_rate(), 10.0)
        await self.comic(1)
        await self.comic(2)
        self.assertAlmostEqual((await self.series()).remaining, 2000, places=4)

    async def test_a_volume_partway_through_counts_only_what_is_left(self):
        await self.learn_the_comic_rate()
        await self.comic(1)
        await self.partway(1, 40)
        # 60% of 1000s. The own-pace path is not in play: nothing has been
        # timed against item 1, so this is the series' rate times its pages.
        self.assertAlmostEqual((await self.series()).remaining, 600, places=4)

    async def test_a_finished_volume_adds_nothing(self):
        await self.learn_the_comic_rate()
        await self.comic(1)
        await self.comic(2)
        await self.finished(2)
        self.assertAlmostEqual((await self.series()).remaining, 1000, places=4)

    async def test_a_volume_the_reader_is_slow_on_uses_its_own_pace(self):
        # The per-item path survives the sum: 600s spent on 25% of item 1
        # projects 2400s for it, so 1800s are left of that one -- against
        # the 960s the series rate alone would claim.
        #
        # Timing item 1 also makes it a SECOND sample of that rate, which
        # is the arithmetic this test got wrong first: 1600 seconds over
        # 125 pages read is 12.8 s/page, not the 10 the fixture taught, so
        # the untouched item 2 is 1280. Asserted rather than assumed,
        # because the whole number turns on it.
        await self.learn_the_comic_rate()
        await self.comic(1)
        await self.comic(2)
        await self.partway(1, 25)
        await self.spend(600, item_id=1)
        self.assertAlmostEqual(await self.comic_rate(), 12.8, places=4)
        self.assertAlmostEqual((await self.series()).remaining, 3080, places=4)

    async def test_no_rate_learned_means_no_number_at_all(self):
        # A comic's rate is learned inside its OWN series, and with no
        # series read anywhere there is not even a stand-in to borrow.
        await self.comic(1)
        await self.comic(2)
        shelf = await self.series()
        self.assertEqual(shelf.unread, 2)
        self.assertIsNone(shelf.remaining)

    async def test_it_sums_what_it_can_when_one_item_has_no_size(self):
        # A comic whose archive would not list has no page_count and gets
        # no estimate; it still counts as something to read. The card shows
        # both, and the time is honest about covering less than the badge.
        await self.learn_the_comic_rate()
        await self.comic(1)
        await self.comic(2, pages=None)
        shelf = await self.series()
        self.assertEqual(shelf.unread, 2)
        self.assertAlmostEqual(shelf.remaining, 1000, places=4)


class ANovelsShelfIsItsChapters(ShelfCase):
    """Exact word inventories share a pace but retain each card's scope."""

    async def chapter_words(self, chapter_id, words=100, *, volume=1):
        await self.chapter(chapter_id, volume=volume)
        source = '<p>' + 'x ' * words + '</p>'
        await self.db.execute('UPDATE chapters SET content_html=?,content_size=? WHERE id=?',
                              (source,len(source),chapter_id))
        await self.db.commit()

    async def word_pace(self):
        # Account evidence independent of positions or legacy durations.
        for index in range(3):
            await self.db.execute("""INSERT INTO page_reads
                (user_id,kind,started_at,seconds,cap_seconds,visit_id,words,pace_words,pace_seconds,pace_samples)
                VALUES (1,'novel',datetime('now'),100,100,?,1000,1000,100,1)""", (f'sample-{index}',))
        await self.db.commit()

    async def anchor(self, chapter_id, offset=0, percent=99):
        await self.open_at(chapter_id, percent)
        await self.db.execute('UPDATE novel_reading_progress SET paragraph_index=0,character_offset=?', (offset,))
        await self.db.commit()

    async def test_unread_counts_include_skipped_and_current_chapters(self):
        await self.chapter_words(1)
        await self.chapter_words(2)
        await self.chapter_words(3)
        await self.anchor(2)
        await self.mark_read(3)
        self.assertEqual((await self.novel()).unread,2)

    async def test_home_scope_crosses_volumes_and_uses_exact_anchor_not_percent(self):
        await self.word_pace()
        await self.chapter_words(1,100,volume=1)
        await self.chapter_words(2,200,volume=2)
        await self.anchor(1,offset=40,percent=99)
        # 20 words traversed, 80 + 200 remain at 0.1 s/word.
        self.assertAlmostEqual((await self.novel()).remaining,28)

    async def test_skipped_chapters_remain_until_explicitly_marked_read(self):
        await self.word_pace()
        await self.chapter_words(1,100)
        await self.chapter_words(2,100)
        await self.anchor(2,offset=100)
        self.assertAlmostEqual((await self.novel()).remaining,15)
        await self.mark_read(1)
        self.assertAlmostEqual((await self.novel()).remaining,5)

    async def test_unavailable_content_does_not_fabricate_a_word_inventory(self):
        await self.word_pace()
        await self.chapter_words(1)
        await self.chapter(2,size=None)
        self.assertIsNone((await self.novel()).remaining)

    async def test_never_opened_means_all_unread_words(self):
        await self.word_pace()
        await self.chapter_words(1,100)
        await self.chapter_words(2,200)
        self.assertAlmostEqual((await self.novel()).remaining,30)

    async def test_marked_current_chapter_contributes_no_words(self):
        await self.word_pace()
        await self.chapter_words(1,100)
        await self.chapter_words(2,200)
        await self.anchor(1,offset=40)
        await self.mark_read(1)
        self.assertAlmostEqual((await self.novel()).remaining,20)

    async def test_completed_scope_has_neither_badge_nor_eta(self):
        await self.word_pace()
        await self.chapter_words(1)
        await self.anchor(1)
        await self.mark_read(1)
        shelf = await self.novel()
        self.assertEqual(shelf.unread,0)
        self.assertIsNone(shelf.remaining)
        self.assertFalse(shelf.text_eta)

    async def test_legacy_time_is_preserved_without_becoming_valid_pace(self):
        await self.chapter_words(1)
        await self.anchor(1,offset=100)
        await self.spend(1000,chapter_id=1,novel_id=1)
        shelf = await self.novel()
        self.assertIsNone(shelf.remaining)
        self.assertTrue(shelf.text_eta)


class OneNumberUnderTwoNames(unittest.TestCase):
    """The badge, the sort and the live card builder read ONE number.

    The sort's option VALUE is still "items" -- it is what readers already
    have in localStorage -- so the thing that has to stay in step is the
    DATA KEY behind it, in three files that cannot import each other.
    """

    def setUp(self):
        self.index = (APP / "templates/library/index.html").read_text()
        self.home_js = (APP / "static/js/home.js").read_text()
        self.events_js = (APP / "static/js/library-events.js").read_text()

    def test_the_grid_renders_the_unread_count(self):
        self.assertIn("data-unread-count=\"{{ card['unread_count'] }}\"", self.index)
        self.assertIn("{% if card['unread_count'] %}", self.index)

    def test_the_sort_and_the_live_builder_read_that_same_attribute(self):
        self.assertIn('items: "unreadCount"', self.home_js)
        self.assertIn('items: "unreadCount"', self.index)      # the pre-paint copy
        self.assertIn("a.dataset.unreadCount", self.events_js)

    def test_nothing_still_reads_the_old_total(self):
        # The rename is the point: a card showing what is unread while a
        # control orders by what the shelf holds is two numbers wearing one
        # name, which is the drift this project keeps getting bitten by.
        for source in (self.index, self.home_js, self.events_js):
            self.assertNotIn("item_count", source)
            self.assertNotIn("itemCount", source)
            self.assertNotIn("data-item-count", source)

    def test_the_control_says_which_number_it_orders_by(self):
        self.assertIn('<option value="items">Unread</option>', self.index)


class TheGridsEstimateIsNotTheStrips(unittest.TestCase):
    """The home grid's time must NOT carry `.reading-eta`.

    That class is the hook `applyEta` rewrites when a progress event
    arrives, using the CHAPTER percentage carried in it -- and a web novel
    appears in both grids under the same `data-novel-id`, which is what
    `handleNovelProgressUpdated` selects on. Sharing the name would have
    had one card's chapter percentage scale the other card's whole-novel
    total, live, on screen: `~14h` for a novel becoming `~8h` because the
    reader is 40% through one chapter of it.
    """

    def setUp(self):
        self.index = _uncommented(APP / "templates/library/index.html")
        self.events_js = _uncommented(APP / "static/js/library-events.js")
        self.strip = _uncommented(APP / "templates/library/_reading_grid.html")

    def test_the_strip_is_what_carries_the_live_hook(self):
        self.assertIn("reading-eta", self.strip)
        self.assertIn('querySelector(".reading-eta")', self.events_js)

    def test_the_grid_card_does_not(self):
        # Both copies of that card: the template and the builder that has
        # to produce markup indistinguishable from it. Bounded to the card
        # itself -- from the grid it lives in to the endfor that closes the
        # loop -- so this cannot pass on some other part of the page.
        grid = self.index.split('id="series-grid"', 1)[1].split("{% endfor %}", 1)[0]
        self.assertIn("card['remaining_seconds']", grid)
        self.assertNotIn("reading-eta", grid)
        builder = self.events_js.split("function buildCardElement(", 1)[1]
        builder = builder.split("\n    }", 1)[0]
        self.assertIn("card.remaining_seconds", builder)
        self.assertNotIn("reading-eta", builder)

    def test_the_scan_is_looking_at_code(self):
        # The guard above is a source scan, and the comment right beside
        # the line it guards SAYS "NOT .reading-eta" -- so a scan that read
        # prose failed on the very explanation of the rule. Both blanking
        # rules are checked against a sample here, because a regex that
        # quietly stopped matching would leave the test green and guarding
        # nothing.
        self.assertEqual(_strip("a {# x reading-eta y #} b"), "a  b")
        self.assertEqual(_strip("a\n// reading-eta\nb"), "a\n\nb")
        # The negative control, against the file as it is on disk: the
        # template DOES say "reading-eta", in the comment explaining why
        # the line below it must not, and the assertion above is green only
        # because the blanking took that sentence out.
        raw = (APP / "templates/library/index.html").read_text()
        self.assertNotIn("reading-eta", _strip(raw))


class TheLiveCardCarriesBothNumbers(unittest.TestCase):
    """`buildCardElement` has to produce what the template produces.

    A card refetched over SSE replaces the server-rendered one in place
    (see refreshCard), so anything the builder leaves out simply vanishes
    from a card that updates -- silently, and only on the screens that
    stayed open. There is no DOM here to render it in, so what is pinned is
    the source: each number's GATE as well as its name, because a builder
    that still mentions `card.remaining_seconds` inside a block it no
    longer enters reads exactly like one that works.
    """

    def setUp(self):
        events = _uncommented(APP / "static/js/library-events.js")
        self.builder = events.split("function buildCardElement(", 1)[1].split("\n    }", 1)[0]

    def test_the_badge_is_the_unread_count_and_is_absent_at_zero(self):
        self.assertIn("if (card.unread_count) {", self.builder)
        self.assertIn("countBadge.textContent = String(card.unread_count);", self.builder)
        self.assertIn("thumb.appendChild(countBadge);", self.builder)

    def test_the_time_is_rendered_only_when_there_is_one(self):
        self.assertIn("if (card.remaining_seconds || card.text_eta) {", self.builder)
        self.assertIn("formatDuration(card.remaining_seconds)", self.builder)
        self.assertIn("a.appendChild(eta);", self.builder)

    def test_it_says_the_same_words_the_server_did(self):
        # A card must not change its wording the first time it updates --
        # the same rule that keeps formatDuration in step with the Python
        # filter. Both halves of the sentence, since the tooltip is where
        # the card says what the number covers.
        index = (APP / "templates/library/index.html").read_text()
        for phrase in ("About ", " of this left to read, going by how fast you read"):
            self.assertIn(phrase, self.builder)
            self.assertIn(phrase, index)


class TheCardFollowsWhatMovesIt(unittest.TestCase):
    """Which events refresh a home card, and which deliberately do not.

    The numbers move in WHOLE items, so what has to be caught is an item or
    a chapter crossing the read line -- and nothing else, because a
    percentage arrives every few seconds while reading and each one would
    buy a refetch.

    The item rule is keyed off the status in the EVENT rather than off a
    disagreement with something on screen. Measured in a browser: an item
    never opened, marked read from its series page, has no [data-item-id]
    element on the home page at all -- so the on-screen comparison found
    nothing to disagree with and the card sat on "3 / ~1h 27m" after the
    third of three volumes was finished, where it should read "2 / ~53m".
    """

    def setUp(self):
        self.js = _uncommented(APP / "static/js/library-events.js")

    def _handler(self, name):
        return self.js.split(f"function {name}(", 1)[1].split("\n    }", 1)[0]

    def test_an_item_crossing_the_read_line_refreshes_its_series_card(self):
        handler = self._handler("handleProgressUpdated")
        self.assertIn('if (data.series_id && (data.status || "") !== "reading") {', handler)
        self.assertIn("refreshSeriesCard(data.series_id)", handler)

    def test_mark_all_read_refreshes_it_too(self):
        handler = self._handler("handleSeriesProgressUpdated")
        self.assertIn("refreshSeriesCard(seriesId)", handler)

    def test_a_chapter_boundary_refreshes_the_novel_card(self):
        # Crossing into another chapter is when the one behind is marked
        # read (reader-text.js's onChapterAdvance), which is the only thing
        # that moves a novel's count.
        handler = self._handler("handleNovelProgressUpdated")
        self.assertIn("if (chapterChanged && data.novel_id) {", handler)
        self.assertIn('refreshCard("novel", data.novel_id)', handler)

    def test_a_bare_percentage_does_not(self):
        # The guard against the opposite mistake. A card refreshed on every
        # progress save is a fetch every few seconds per open tab, for a
        # number that moved by seconds -- so neither handler may refresh
        # unconditionally.
        item = self._handler("handleProgressUpdated")
        self.assertNotIn("\n        scheduleRefresh(`card:", item)
        novel = self._handler("handleNovelProgressUpdated")
        self.assertNotIn("\n        scheduleRefresh(`novel-card:", novel)


class OneEstimatorPerPage(unittest.TestCase):
    """`build_estimator` reads the reader's whole history to answer both
    the grid and the "Reading" strip, so the home page builds it once and
    hands it down. Measured at 2 ms of a 182 ms page -- small, and exactly
    the kind of small that gets doubled by accident."""

    def test_home_builds_it_once_and_passes_it_on(self):
        source = (APP / "library/router.py").read_text()
        home = source.split("async def home(", 1)[1].split("\n@router", 1)[0]
        self.assertEqual(home.count("build_estimator("), 1)
        self.assertIn("_reading_items(db, user[\"id\"], estimator)", home)

    def test_the_strip_on_its_own_still_builds_one(self):
        # /library/reading-grid renders that section alone and has no
        # estimator to be given, so the parameter has to be optional.
        source = (APP / "library/router.py").read_text()
        strip = source.split("async def _reading_items(", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("if estimator is None:", strip)


if __name__ == "__main__":
    unittest.main()
