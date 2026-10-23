"""A reader that came back from a cache asks where it really is.

Three caches can answer a Back into a reader, and all three hand back a
page that was right when it was filed: the browser's back/forward cache
(the DOCUMENT it froze -- DOM, scroll and page number intact), htmx's
history cache, and the plain HTTP cache (the BYTES the server sent the
first time, re-rendered at the position they carry). That is right for
the tab that was frozen and wrong for the library, if the same book was
read on another device meanwhile, and a reader is the one page that
cannot find out any other way: it carries no live-update stream (the
`live_updates` block is empty in all three reader templates), so there is
not even a cursor to compare a render stamp against.

So it asks for one number, and this file pins both ends of that
question: the GET endpoints that answer it, and the rules the client half
follows.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.novels.router import read_novel_progress
from app.reader.router import read_progress

ROOT = Path(__file__).resolve().parents[1]
RESUME = ROOT / "app/static/js/reader-resume.js"
READERS = {
    "app/templates/reader/comic.html": "app/static/js/reader-comic.js",
    "app/templates/reader/epub.html": "app/static/js/reader-text.js",
    "app/templates/novels/reader.html": "app/static/js/reader-text.js",
}


def _code_of(path: Path) -> str:
    """The script with its comments blanked out.

    These are scans for CODE, and the comments below explain each rule by
    naming exactly what it forbids -- `location.reload`, `assign`,
    `!==`. Same treatment, same reason, as tests/test_events.py.
    """
    source = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in source.splitlines())


def _body(json_response):
    return json.loads(json_response.body)


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
        self.user = {"id": 1}
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type)"
            " VALUES (1, 'L', '/l', 'comic')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'S', 'S', 's')")
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size)"
            " VALUES (7, 1, 1, 'V1', 'V1', '/l/s/v1.cbz', 'cbz', 'h', 1)")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url, sync_status, active)"
            " VALUES (1, 'N', 'https://x/1', 'idle', 1)")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def _chapter(self, chapter_id, volume_number, number, *, volume_id=None):
        volume_id = volume_id or volume_number
        await self.db.execute(
            "INSERT OR IGNORE INTO volumes (id, novel_id, number) VALUES (?, 1, ?)",
            (volume_id, volume_number))
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url, order_index)"
            " VALUES (?, 1, ?, ?, ?, ?, ?)",
            (chapter_id, volume_id, float(number), f"C{chapter_id}",
             f"https://x/c{chapter_id}", chapter_id))
        await self.db.commit()


class WhatTheServerAnswers(_Case):
    async def test_an_item_reports_its_percentage_and_itself(self):
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, current_page, progress_percent)"
            " VALUES (1, 7, 'reading', 40, 62)")
        await self.db.commit()
        body = _body(await read_progress(7, user=self.user, db=self.db))
        self.assertEqual(body["progress_percent"], 62)
        # The same page. An item reader is opened per ITEM, so being
        # rendered from the current position means coming back here.
        self.assertEqual(body["resume_url"], "/reader/7")

    async def test_an_item_never_opened_is_zero_rather_than_missing(self):
        # 0 is behind wherever the asking tab is, so a book nobody has
        # read on the other device leaves that tab exactly as it was.
        body = _body(await read_progress(7, user=self.user, db=self.db))
        self.assertEqual(body["progress_percent"], 0)

    async def test_a_novel_points_at_the_chapter_the_position_is_in(self):
        # The whole reason the URL comes from the server: a novel reader
        # is opened per CHAPTER, so reloading where the tab already is
        # would re-render chapter 1 and land at the top of it while the
        # saved position sits in chapter 41.
        await self._chapter(100, 1, 1)
        await self._chapter(141, 2, 41)
        await self.db.execute(
            "INSERT INTO novel_reading_progress"
            " (user_id, novel_id, chapter_id, scroll_percent, chapter_percent, paragraph_index)"
            " VALUES (1, 1, 141, 73.5, 20.0, 12)")
        await self.db.commit()
        body = _body(await read_novel_progress(1, user=self.user, db=self.db))
        self.assertEqual(body["progress_percent"], 73.5)
        self.assertEqual(body["resume_url"], "/novels/1/read/2/41")

    async def test_a_whole_numbered_chapter_is_not_linked_as_a_float(self):
        # `chapters.number` is a REAL, and "/read/1/41.0" is a URL the
        # reader route does not answer the way the page's own links do.
        await self._chapter(141, 1, 41)
        await self.db.execute(
            "INSERT INTO novel_reading_progress"
            " (user_id, novel_id, chapter_id, scroll_percent, chapter_percent, paragraph_index)"
            " VALUES (1, 1, 141, 10.0, 1.0, 0)")
        await self.db.commit()
        body = _body(await read_novel_progress(1, user=self.user, db=self.db))
        self.assertEqual(body["resume_url"], "/novels/1/read/1/41")

    async def test_the_novel_reader_is_rendered_with_the_baseline_in_it(self):
        # The other end of the same number. A page rendered with 0 while
        # the server held 62 would report itself as behind on the first
        # restore and throw itself away for nothing.
        from app.novels.router import _load_chapter_context
        await self._chapter(100, 1, 1)
        await self.db.execute(
            "INSERT INTO novel_reading_progress"
            " (user_id, novel_id, chapter_id, scroll_percent, chapter_percent, paragraph_index)"
            " VALUES (1, 1, 100, 62.0, 30.0, 4)")
        await self.db.commit()
        ctx = await _load_chapter_context(1, 1, 1.0, self.user, self.db)
        self.assertEqual(ctx["start_percent"], 62.0)

    async def test_a_novel_never_opened_is_rendered_at_zero(self):
        # Control: `None` would render as the string "None" into the
        # attribute, and Number("None") is NaN.
        from app.novels.router import _load_chapter_context
        await self._chapter(100, 1, 1)
        ctx = await _load_chapter_context(1, 1, 1.0, self.user, self.db)
        self.assertEqual(ctx["start_percent"], 0)

    async def test_a_novel_with_nowhere_to_go_says_so(self):
        body = _body(await read_novel_progress(1, user=self.user, db=self.db))
        self.assertEqual(body["progress_percent"], 0)
        self.assertIsNone(body["resume_url"])

    async def test_a_chapter_with_no_number_is_nowhere_to_go_either(self):
        # `chapters.number` is nullable -- a scrape can produce one -- and
        # the URL built from it would be "/novels/1/read/1/", which the
        # reader route does not answer. Staying put is the right answer.
        await self._chapter(100, 1, 1)
        await self.db.execute("UPDATE chapters SET number = NULL WHERE id = 100")
        await self.db.execute(
            "INSERT INTO novel_reading_progress"
            " (user_id, novel_id, chapter_id, scroll_percent, chapter_percent, paragraph_index)"
            " VALUES (1, 1, 100, 55.0, 5.0, 1)")
        await self.db.commit()
        body = _body(await read_novel_progress(1, user=self.user, db=self.db))
        self.assertIsNone(body["resume_url"])


class TheOtherWayBackIn(unittest.TestCase):
    """htmx's history cache never unloaded anything.

    Under `hx-boost` the document is alive throughout, so a Back into a
    reader fires no `pageshow` at all -- and the restored page re-runs its
    scripts, which re-initialise the reader from `data-start-percent`.
    Measured before this: left at 6/10, Back put it on **2/10** and
    nothing asked the server anything.

    With the hook, both browsers, one `GET .../progress` each time:

        another device at 90%  -> 9/10, navigated
        only this tab's own    -> 6/10, navigated (no longer rewound)
        nothing read at all    -> 2/10, NOT navigated

    """

    def setUp(self):
        self.js = _code_of(RESUME)

    def test_the_hook_is_the_hit_and_not_the_restore(self):
        # The browsers disagree about when a restored page's scripts run:
        #     Chromium   cache:hit -> watch(20.0) -> cache:after:restore
        #     Firefox    cache:hit -> cache:after:restore -> watch(20.0)
        # `cache:hit` is first in both, so it is the only one `watch()`
        # can rely on finding already set.
        self.assertIn('document.addEventListener("htmx:history:cache:hit"', self.js)
        self.assertNotIn("cache:after:restore", self.js)

    def test_the_restore_is_consumed_by_watch(self):
        watcher = self.js.split("watch(url, percent, position = null) {", 1)[1].split("},", 1)[0]
        self.assertIn("if (restoring) {", watcher)
        self.assertIn("restoring = false;", watcher)
        self.assertIn("catchUp();", watcher)

    def test_the_baseline_is_what_the_restored_page_SHOWS(self):
        # Not what the tab last told the server. A restored reader
        # re-initialises from the snapshot's `data-start-percent`, so that
        # is what is on screen -- and the question this asks is whether
        # the server is ahead of what is on screen.
        watcher = self.js.split("watch(url, percent, position = null) {", 1)[1].split("},", 1)[0]
        self.assertIn("watched = { url, sent: Number(percent) || 0, position,", watcher)
        self.assertLess(watcher.index("watched = {"), watcher.index("catchUp();"))

    def test_a_forward_navigation_disarms_it(self):
        # A hit on some other page must not leave this armed for the next
        # reader opened normally -- that one the server just rendered.
        self.assertIn('document.addEventListener("htmx:before:history:update", '
                      "() => { restoring = false; });", self.js)

    def test_both_ways_in_share_one_body(self):
        # Two copies of the compare-and-navigate would drift. There is one
        # `catchUp`, and both triggers call it.
        self.assertEqual(self.js.count("async function catchUp("), 1)
        self.assertEqual(self.js.count("catchUp();"), 3)
        self.assertEqual(self.js.count("window.location.replace(saved.resume_url);"), 1)


class TheThirdWayBackIn(unittest.TestCase):
    """The HTTP cache answers a traversal, and nothing here could tell.

    `Cache-Control: private, no-cache` plus an ETag makes Back
    REVALIDATE; it does not stop the browser answering a session-history
    navigation from its own cache. Only `no-store` would, and that is the
    back/forward cache traded away. What comes back is a real, new
    document -- `persisted` is false -- rendered from the position the
    bytes were rendered at.

    Measured on the real build, left at 6/10 with the server on 90%,
    Firefox, Back:

        without this  pageshow persisted false, type "back_forward",
                      zero asks, the page at 2/10
        with it       one GET /reader/3/progress, the page at 9/10

    Chromium restored the same Back from its back/forward cache and was
    already right; the widened check changes nothing there. And an
    ordinary forward load of a comic or a novel asks nothing in either
    browser, which is the control that says this stayed narrow.
    """

    def setUp(self):
        self.js = _code_of(RESUME)
        self.fn = self.js.split("function cameBackInHistory()", 1)[1].split("}", 1)[0]

    def test_a_traversal_is_asked_about_even_when_nothing_was_persisted(self):
        handler = self.js.split('window.addEventListener("pageshow"', 1)[1]
        self.assertIn("event.persisted || cameBackInHistory()", handler)

    def test_the_signal_is_what_the_navigation_WAS(self):
        # Not what it transferred. `transferSize` reads 0 for a cache hit
        # in the field and 0 for everything under a driven browser, which
        # is how the same idea suppressed the real case once already
        # (see library-events.js).
        self.assertIn('performance.getEntriesByType("navigation")', self.fn)
        self.assertIn('=== "back_forward"', self.fn)
        self.assertNotIn("transferSize", self.js)
        self.assertNotIn("decodedBodySize", self.js)

    def test_an_ordinary_load_is_not_one(self):
        # The inverted sense would ask on every page view instead --
        # which is exactly the cost the `persisted` check existed to
        # avoid.
        self.assertNotIn('!== "back_forward"', self.fn)
        self.assertIn("return entry?.type", self.fn)

    def test_a_browser_with_no_entry_to_read_asks_nothing(self):
        # `getEntriesByType` can come back empty. `entry.type` would
        # throw here, inside a `pageshow` handler, taking the bfcache
        # half down with it.
        self.assertIn("entry?.type", self.fn)


class TheFourRulesTheClientFollows(unittest.TestCase):
    def setUp(self):
        self.js = _code_of(RESUME)

    def test_it_only_runs_for_a_restore(self):
        # An ordinary load was already rendered from the saved position.
        # Asking again would be one request per page view for nothing.
        self.assertIn('"pageshow"', self.js)
        handler = self.js.split('window.addEventListener("pageshow"', 1)[1]
        self.assertIn("if (event.persisted || cameBackInHistory()) catchUp();",
                      handler)

    def test_it_only_acts_when_the_server_is_AHEAD(self):
        # Reading backwards on a phone -- a scroll up, a chapter re-read
        # -- is not a request to drag this tab back with it. `!==` here
        # would make every such move throw this page away.
        self.assertIn("if (there <= baseline.sent + 1) return;", self.js)
        self.assertIn("if (difference <= 0) return;", self.js)

    def test_it_replaces_rather_than_pushing_a_history_entry(self):
        # This runs on a back/forward traversal. `assign` would leave a
        # Back button in front of a page that no longer describes
        # anything, and `reload` cannot follow a novel to another chapter.
        self.assertIn("window.location.replace(saved.resume_url)", self.js)
        self.assertNotIn("location.assign", self.js)
        self.assertNotIn("location.reload", self.js)

    def test_a_failed_ask_changes_nothing(self):
        # Offline, or the server gone. The position is already on screen.
        self.assertIn("if (!response.ok) return;", self.js)
        self.assertIn("} catch {", self.js)


class BothEndsAreWiredUp(unittest.TestCase):
    def test_it_is_loaded_from_the_head_and_not_from_the_readers(self):
        # It used to sit above the engine in each reader template, on the
        # strength of `defer` keeping document order. A swap does not
        # keep it -- htmx re-inserts each script with
        # `document.createElement`, which is force-async -- and the
        # engines call this one through `?.`, so losing the race is
        # silent. See tests/test_swapped_scripts.py for the measurement.
        base = (ROOT / "app/templates/base.html").read_text()
        self.assertIn("asset_url('js/reader-resume.js')",
                      base[:base.index("</head>")])
        for template, engine in READERS.items():
            markup = (ROOT / template).read_text()
            self.assertNotIn("asset_url('js/reader-resume.js')", markup, template)
            self.assertIn(f"asset_url('js/{Path(engine).name}')", markup, template)

    def test_every_reader_template_carries_the_baseline(self):
        # Without it a tab opened and never scrolled has nothing to
        # measure the server's answer against, and the one case this
        # exists for -- sitting in the cache while another device reads --
        # is exactly the case where the tab never scrolled.
        for template in READERS:
            self.assertIn("data-start-percent", (ROOT / template).read_text(), template)

    def test_both_engines_watch_and_report(self):
        for engine in set(READERS.values()):
            js = _code_of(ROOT / engine)
            self.assertIn("window.readerResume?.watch(", js, engine)
            self.assertIn("window.readerResume?.sent(", js, engine)

    def test_the_comic_only_reports_the_item_the_page_is(self):
        # Scroll mode posts a chapter that has been scrolled past under
        # its own id. Reporting that as this page's baseline would make a
        # finished chapter read as 100% of the page asking.
        js = _code_of(ROOT / "app/static/js/reader-comic.js")
        # The two lines immediately above the report, so an `if` that
        # drifted away from it (or was deleted) fails rather than passing
        # on a match somewhere else in the file.
        guard = js.split("window.readerResume?.sent(", 1)[0].rstrip().splitlines()[-1]
        self.assertIn("String(targetItemId) === String(itemId)", guard)

    def test_the_shared_file_installs_itself_once(self):
        # Same rule as every other file here: a swap re-runs what is in
        # page content, and a second pageshow listener would ask twice.
        self.assertIn("if (window.readerResume) return;", self.js_resume())

    @staticmethod
    def js_resume():
        return _code_of(RESUME)
