"""Navigation is htmx's now, and the history cache is the point of it.

`hx-boost` on `<body>` makes a click on a same-origin `<a>` fetch the page
and swap `<body>`'s children instead of loading a document. That is worth
having on its own, but the reason it is here is that the `history-cache`
extension only ever acts inside htmx's OWN history lifecycle -- it
serialises the DOM to `sessionStorage` when htmx pushes a URL and swaps it
back when htmx restores. Without a boosted navigation the extension is
dead code, which is exactly what it was.

Three things this has to keep right, and each one fails silently:

1. The whitelist separator. `htmx.config.extensions` is split on
   `/,\\s*/`, so "sse history-cache" is ONE name that matches neither and
   **both** extensions quietly fail to register. Measured: with the space,
   `/library/events` was never requested at all -- the live stream simply
   did not exist.
2. `:inherited`. htmx 4 has no implicit inheritance, so a bare `hx-boost`
   on `<body>` boosts `<body>` and nothing inside it.
3. The readers. A boosted navigation does not unload the document, so
   `pagehide` never fires -- and that is where all three readers save the
   position and where `reader-resume.js` measures a back/forward-cache
   restore.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUMP = "window.libraryEventsSeen = (window.libraryEventsSeen || 0) + 1;"
BASE = ROOT / "app/templates/base.html"
TEMPLATES = ROOT / "app/templates"
READERS = ("reader/comic.html", "reader/epub.html", "novels/reader.html")
# The four places a link reaches a reader. Three name the URL; the fourth
# is the "Reading" strip, whose `entry.url` IS a reader URL (see the "url"
# key in library/router.py).
READER_LINKS = ("library/_item_card.html", "library/_search_items.html",
                "library/_reading_grid.html", "novels/detail.html")


def _markup_of(path: Path) -> str:
    """The template with its comments blanked out.

    These are scans for ATTRIBUTES, and the comments below explain each
    rule by naming exactly what it forbids. Same treatment, same reason, as
    tests/test_swapped_scripts.py.
    """
    source = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)
    return re.sub(r"<!--.*?-->", "", source, flags=re.S)


def _config() -> dict:
    content = re.search(r'name="htmx-config"\s+content=\'([^\']+)\'', BASE.read_text())
    assert content, "htmx-config meta has no content attribute"
    return json.loads(content.group(1))


class TheExtensionsAreActuallyRegistered(unittest.TestCase):
    def test_the_whitelist_is_comma_separated(self):
        # THE trap. htmx splits this on /,\s*/ and a space-separated value
        # is one name matching nothing, so BOTH extensions fail to
        # register -- and the one that fails loudest is `sse`, whose
        # stream is then never opened. Measured with a space:
        # /library/events was never requested; with the comma,
        # htmx.config.historyCache came back filled in by the extension's
        # own init (size 10, swapStyle "outerSync") instead of bare.
        names = re.split(r",\s*", _config()["extensions"])
        self.assertEqual(sorted(names), ["history-cache", "preload", "resync", "sse"])
        self.assertNotIn(" ", _config()["extensions"])

    def test_the_cache_is_not_left_disabled(self):
        # The extension registers and then does nothing: core sets
        # `historyCache = {disable: true}` before the extension's `??=`
        # can fill it, so saying so in the meta is the only way past it.
        self.assertIs(_config()["historyCache"]["disable"], False)

    def test_the_error_bodies_rule_is_untouched(self):
        # Control: this meta carries three settings and the other two are
        # load-bearing elsewhere (tests/test_htmx_version.py). A rewrite
        # that dropped one would pass both tests above.
        self.assertEqual(_config()["noSwap"], [204, 304, "4xx", "5xx"])


class ANewPageIsNotHandedTheOldPagesAttributes(unittest.TestCase):
    """With a settle delay, htmx gives each incoming element whose id is on
    the outgoing page that element's OLD attributes until the delay is up,
    and the page's scripts run in between. Measured boosting /series/1 ->
    /series/2: `#series-page` said data-series-id="1" when the pre-paint
    block and series-search.js read it, so series 2 opened with series 1's
    "Unread only" (and `#series-sort-control` gave series-sort.js series
    1's saved order and data-has-manual). Zero is the one delay that skips
    the swap outright."""

    def test_the_settle_delay_is_zero(self):
        self.assertEqual(_config()["defaultSettleDelay"], 0)

    def test_zero_is_what_skips_the_attribute_swap_in_this_bundle(self):
        # Pinned against the vendored core, so a new bundle that changes
        # the condition fails here rather than in a browser.
        bundle = (ROOT / "app/static/src/vendor/htmax.min.js").read_text()
        self.assertIn("l=i.settle??this.config.defaultSettleDelay", bundle)
        self.assertIn("a=t&&l?this.#Ue(n,r):[]", bundle)


class BoostIsOnTheBodyAndInheritable(unittest.TestCase):
    def test_the_body_boosts_its_descendants(self):
        markup = _markup_of(BASE)
        self.assertIn('hx-boost:inherited="true"', markup)
        # `:inherited` and not a bare hx-boost: htmx 4 has no implicit
        # inheritance, so the bare form boosts <body> itself -- an element
        # nobody clicks -- and every link in the page keeps loading
        # documents, with nothing on screen saying so.
        self.assertNotIn('<body hx-boost="', markup)

    def test_it_sits_in_a_block_a_page_can_override(self):
        markup = _markup_of(BASE)
        self.assertIn("{% block boost %}", markup)
        block = markup.split("{% block boost %}", 1)[1].split("{% endblock %}", 1)[0]
        self.assertIn('hx-boost:inherited="true"', block)


class TheStreamOutlivesAPageThatHasNoneOfItsOwn(unittest.TestCase):
    """`hx-preserve` cannot carry it into a reader, because a reader has
    nowhere to carry it TO.

    htmx matches preserved nodes by id against the INCOMING content (the
    bundle's `querySelectorAll("[hx-preserve]")` runs on the new
    fragment), so the element survives a swap into any page that also
    renders it. Every reader template renders an empty `live_updates`
    block on purpose, so there is no match, and the element goes the way
    of every other child of `<body>`: cleaned up -- which the hx-sse
    extension answers by aborting the stream.

    Measured before this, one boosted click from the home page into a
    comic reader, both browsers:

        without   0 stream elements, `htmx:sse:close` reason `cleanup`,
                  an uncaught `BodyStreamBuffer was aborted` (Chromium) /
                  `The operation was aborted` (Firefox), and a second
                  `GET /library/events` on the way back out
        with      one connection held across the whole tour, 0 errors

    And it is alive rather than merely present: three events published
    from another client while the tab sat in the reader were delivered
    live (3 frames), and the home page it came back to already showed
    52%. The control -- the same tour with these two listeners neutered
    -- delivered 0, and the strip came back showing the stale 40% with no
    page request, because the reconnect on the way out is a NEW element
    with no id to be compared.
    """

    def setUp(self):
        source = (ROOT / "app/static/js/library-events.js").read_text()
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        self.js = "\n".join("" if line.lstrip().startswith("//") else line
                            for line in source.splitlines())
        self.block = self.js.split("let parked = null;", 1)[1].split(
            "// ---- ", 1)[0]

    def test_it_steps_out_before_the_swap_and_back_after_it(self):
        self.assertIn('document.addEventListener("htmx:before:swap"', self.block)
        self.assertIn('document.addEventListener("htmx:finally:swap"', self.block)
        # `finally:swap` and not `after:swap`: a cancelled swap returns
        # before `after:swap` is raised, and the element would be left
        # outside <body> for good.
        self.assertNotIn("htmx:after:swap", self.block)

    def test_out_means_out_of_the_body_and_still_in_the_document(self):
        # The extension's read loop tests `sourceElement.isConnected`
        # before every frame, so the node may not leave the document --
        # and it may not stay inside the swap target either. After
        # `</body>` is both, and is where htmx parks its own preserved
        # nodes.
        park = self.block.split('"htmx:before:swap"', 1)[1]
        self.assertIn('document.body.insertAdjacentElement("afterend", stream)', park)

    def test_it_parks_only_what_is_in_the_body_right_now(self):
        # Swaps can overlap (an SSE-driven swap during a boosted
        # navigation). Without this, the second one overwrites `parked`
        # with a node that is already parked -- and the first one's
        # restore puts nothing back.
        park = self.block.split('"htmx:before:swap"', 1)[1]
        self.assertIn("if (!stream || !document.body.contains(stream)) return;", park)
        self.assertLess(park.index("document.body.contains(stream)"),
                        park.index("parked = stream;"))

    def test_hx_preserve_having_done_the_job_leaves_nothing_to_do(self):
        # The two compose rather than compete: htmx looks the parked node
        # up by id -- `getElementById` does not care that it is outside
        # <body> -- and puts it back in the incoming page's own position.
        # Appending it again would move it to the end of every page.
        restore = self.block.split('"htmx:finally:swap"', 1)[1]
        self.assertIn("if (stream && !document.body.contains(stream)) {", restore)

    def test_the_reference_is_dropped_before_anything_else(self):
        # A swap that leaves `parked` set would put the element back on
        # the NEXT swap as well, after that swap had already dealt with
        # it.
        restore = self.block.split('"htmx:finally:swap"', 1)[1]
        self.assertLess(restore.index("parked = null;"),
                        restore.index("document.body.appendChild(stream)"))

    def test_it_is_bound_once_per_document(self):
        # This file runs again for every boosted swap, and `document`
        # survives them all -- a listener per navigation is how the
        # handlers on the element itself went from 14 to 53.
        guard = self.js.split("if (!window.libraryEventsRestoreBound) {", 1)[1]
        self.assertIn("let parked = null;", guard.split("\n    }", 1)[0])


class AReaderSavesItsPositionOnEveryWayOut(unittest.TestCase):
    """And takes its listeners with it when it goes.

    A boosted navigation does not unload the document, so neither
    `pagehide` nor `visibilitychange` fires -- the two the readers used to
    be able to save on. Two other events do, and they are different
    events because the two boosted ways out are different things:

    - `htmx:before:history:update`, for a navigation that pushes or
      replaces the URL. It is the hook because of WHERE the vendored
      bundle raises it: the first statement of `swap()`, before the
      response is parsed and long before the DOM is touched, so the
      reader can still measure itself. It is also narrow -- these readers
      move their own URL with `history.replaceState`, which htmx knows
      nothing about, so a page turn cannot reach it.
    - `htmx:history:cache:hit`, for **Back**, which htmx's history cache
      answers from a copy of the DOM without ever pushing or replacing a
      URL -- so the event above does not fire at all. Measured under a
      this build, boosted Back out of the comic reader, both browsers:
      0 progress POSTs before it, 1 after.

    The same swap is why nothing here may bind to `document` or `window`
    directly. Those outlive the body, and so does every handler closed
    over a reader that is no longer on screen. Measured before this,
    both browsers: leave the comic reader by its own link, then press
    the arrow keys on
    the library page -- five presses, four `POST /reader/3/progress`, and
    the book nobody is reading goes from page 6 to page 9 and is recorded
    as FINISHED. Three visits also left 15 such listeners registered
    where 5 belong.

    So `bindOutside` wraps every one of them in the `root.isConnected`
    check the boosted-exit save used to do by hand, and the exits above
    drop the whole set at once. Both halves are needed: the drop is what
    keeps the count flat, and the check is what covers Back, where htmx
    raises no event this file can hang a drop on.

    All four exits save on this build, both browsers -- Back and the
    reader's own link, which are these two, plus closing the tab and the
    app-switch that only hides the document, which htmx never sees.
    (Closing the tab is a `sendBeacon` race in the harness: one loss in
    twelve runs, against four for four without these listeners.)
    """

    READER_SCRIPTS = ("reader-comic.js", "reader-text.js")

    def script(self, name):
        source = (ROOT / "app/static/js" / name).read_text()
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        return "\n".join("" if line.lstrip().startswith("//") else line
                          for line in source.splitlines())

    def helper(self, name):
        """`bindOutside` itself, and no further."""
        js = self.script(name)
        return js.split("function bindOutside(", 1)[1].split(
            "function releaseOutside(", 1)[0]

    def exit_block(self, name):
        """From the exit handler to its registrations, and no further:
        `saveOnExit` is called from two other places above."""
        js = self.script(name)
        return js.split("function leaveOnBoost(", 1)[1].split(
            "bindOutside(document, \"htmx:history:cache:hit\"", 1)[0]

    def test_both_readers_save_when_htmx_takes_the_page_away(self):
        for name in self.READER_SCRIPTS:
            js = self.script(name)
            self.assertIn('bindOutside(document, "htmx:before:history:update", '
                          "leaveOnBoost);", js, name)
            self.assertIn("saveOnExit();", self.exit_block(name), name)

    def test_back_is_the_other_way_out_and_raises_none_of_that(self):
        # htmx's history cache answers a traversal from a copy of the DOM
        # and never touches the URL, so `before:history:update` does not
        # fire. `cache:hit` does, on `document`, before the swap.
        for name in self.READER_SCRIPTS:
            self.assertIn('bindOutside(document, "htmx:history:cache:hit", '
                          "leaveOnBoost);", self.script(name), name)

    def test_the_old_two_exits_are_kept(self):
        # These ADD ways out; they do not replace the ones that cover
        # closing the tab and switching apps, which htmx never sees.
        for name in self.READER_SCRIPTS:
            js = self.script(name)
            self.assertIn('bindOutside(window, "pagehide", saveOnExit);', js, name)
            self.assertIn('bindOutside(document, "visibilitychange"', js, name)

    def test_nothing_binds_to_what_outlives_the_page(self):
        # The whole rule in one scan. `document` and `window` survive the
        # swap; a handler on either of them, closed over a detached
        # reader, is the arrow keys turning pages of a book that is not
        # on screen.
        for name in self.READER_SCRIPTS:
            stray = re.findall(
                r"\b(?:document|window)(?:\.visualViewport)?\.addEventListener\(",
                self.script(name))
            self.assertEqual(stray, [], f"{name}: {stray}")

    def test_a_detached_reader_does_not_get_the_event_at_all(self):
        # Not merely "does not post": the handler is not called, and the
        # listener takes itself off on the way past.
        for name in self.READER_SCRIPTS:
            wrapper = self.helper(name)
            self.assertIn("!root.isConnected", wrapper, name)
            self.assertIn("drop();", wrapper, name)
            self.assertLess(wrapper.index("!root.isConnected"),
                            wrapper.index("handler(event);"), name)

    def test_the_reading_clock_is_stopped_too(self):
        # `createReadingTimeTracker` is one per reader opened, and the
        # only thing that ever stopped one was `pagehide` -- which a
        # boosted navigation does not fire. Measured under a scratch
        # build with the readers boosted, three visits: 6 of its
        # listeners and 3 of its intervals still running, against 2 and
        # 1. Its `pagehide` handler would also post a span for a page
        # nobody is looking at.
        for name in self.READER_SCRIPTS:
            self.assertIn("readingTime.stop();", self.exit_block(name), name)
        stop = (ROOT / "app/static/js/reading-time.js").read_text().split(
            "function stop() {", 1)[1].split("}", 1)[0]
        self.assertIn("flush(true);", stop)       # the span so far is real
        self.assertIn("clearInterval(timer);", stop)
        self.assertIn('removeEventListener("visibilitychange", onVisibilityChange)',
                      stop)
        self.assertIn('removeEventListener("pagehide", onPageHide)', stop)

    def test_leaving_drops_the_whole_set(self):
        # Lazily unbinding each listener on its own next event is not
        # enough on its own: `pagehide` in a document that is never
        # unloaded waits for an event that may never come, and the
        # closure it holds is a whole reader.
        for name in self.READER_SCRIPTS:
            self.assertIn("releaseOutside();", self.exit_block(name), name)
            release = self.script(name).split("function releaseOutside(", 1)[1]
            self.assertIn("for (const drop of outside) drop();", release)
            self.assertIn("outside.length = 0;", release)


class TheFullscreenToggleIsWiredOncePerDocument(unittest.TestCase):
    """`setupButton` runs again for every reader opened.

    It is called from the reader engines, which are page content, so a
    boosted swap re-executes them -- while `document` outlives every one
    of them. A `fullscreenchange` listener per call leaves each past
    visit's sync writing "Exit full" into a button that is no longer in
    the page. Measured over three reader visits, both browsers: **2, 3
    and 4** of them before this, against the 1 that belongs to the
    document itself.

    The shape is the one `novel-chapters.js` uses for the same reason:
    one listener per document, and the CURRENT element looked up when it
    fires. With it the count is flat at 2 over three visits, and the
    toggle still works in the second reader reached by a boosted swap --
    label to "Exit full" on entering and back on leaving, both browsers,
    which is the property one shared listener has to keep.
    """

    def setUp(self):
        source = (ROOT / "app/static/js/fullscreen.js").read_text()
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        self.js = "\n".join("" if line.lstrip().startswith("//") else line
                            for line in source.splitlines())
        self.setup = self.js.split("function setupButton(button) {", 1)[1].split(
            "\n    }", 1)[0]
        self.sync = self.js.split("function syncButton() {", 1)[1].split("\n    }", 1)[0]

    def test_the_sync_listener_is_bound_at_most_once(self):
        self.assertIn("if (!syncBound) {", self.setup)
        self.assertIn("syncBound = true;", self.setup)
        bound = self.setup.split("if (!syncBound) {", 1)[1]
        self.assertIn('document.addEventListener("fullscreenchange", syncButton);', bound)
        # And nowhere else: the module's own userLeft listener is the
        # only other one, and it is not this.
        self.assertEqual(self.js.count('addEventListener("fullscreenchange"'), 2)

    def test_it_reads_the_button_when_it_fires(self):
        # Not the one that was there when it was bound. That is the whole
        # difference between one listener and one per visit.
        self.assertIn("currentButton", self.sync)
        self.assertNotIn("function sync() {", self.js)

    def test_every_call_points_it_at_this_page_s_button(self):
        self.assertIn("currentButton = button;", self.setup)

    def test_a_reader_that_has_left_is_silent(self):
        # `setupButton` is not called on the way out, so nothing else
        # clears this -- and writing into a detached button is the leak
        # in a different shape.
        self.assertIn("if (!currentButton?.isConnected) return;", self.sync)
        self.assertLess(self.sync.index("isConnected"),
                        self.sync.index("textContent"))

    def test_the_click_stays_on_the_button(self):
        # An element-level listener goes away with the element, which is
        # why this one needs no guard of its own.
        self.assertIn('button.addEventListener("click"', self.setup)


class TheReadersAreBoostedToo(unittest.TestCase):
    """They were the one exception, and they are not one any more.

    A boosted navigation leaves the document in place, so `pagehide`
    never fires -- and `pagehide` was the only place all three readers
    saved the position. Leaving by a boosted link dropped the reading
    since the last page turn, so every reader overrode `{% block boost %}`
    to nothing and every link that reached one carried
    `hx-boost="false"`.

    Four things were paid to lift that, each with its own class here or in
    tests/test_swapped_scripts.py: the readers save on the two events htmx
    DOES raise, they take their own `document`/`window` listeners with
    them when they go, the live stream steps out of `<body>` instead of
    being torn down, and the two shared reader files moved into `<head>`
    where a swap cannot reorder them.

    Measured on this build, both browsers, home -> comic reader -> back:
    one document throughout, `#reader-root` 800px at 420x800 with
    `overflow: hidden` and `viewport-fit=cover` (identical to a full load
    of the same page, title included), the stream held across the visit
    with **one** `/library/events` in the life of the tab, 7 listeners
    while the reader is on screen and **0** left behind, the position
    posted on the way out, and the Reading strip back to
    `3:60% 2:90% 1:40%` -- matching a full load exactly.
    """

    def test_no_reader_overrides_the_block_any_more(self):
        for name in READERS:
            markup = _markup_of(TEMPLATES / name)
            self.assertNotIn("{% block boost %}", markup, name)

    def test_the_block_is_still_there_to_opt_out_with(self):
        # Nothing overrides it today. It stays as the way out for a page
        # that turns out to need one, and it is what puts hx-boost on
        # <body> in the first place.
        base = _markup_of(BASE)
        self.assertIn('{% block boost %}hx-boost:inherited="true"{% endblock %}', base)

    def test_no_template_opts_a_link_out(self):
        # The exclusion in its other half: four link sites carried
        # `hx-boost="false"` so that ARRIVING was a document load too.
        # A LINK: the sign-in forms are plain POSTs on purpose (a refusal is
        # the page saying why, and htmx swaps no 4xx), which is not arriving
        # anywhere by a document load.
        offenders = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            if re.search(r'<a\b[^>]*hx-boost="false"', _markup_of(path), flags=re.S):
                offenders.append(path.relative_to(TEMPLATES).as_posix())
        self.assertEqual(offenders, [])

    def test_every_link_that_reaches_a_reader_is_still_there(self):
        # The other way this could pass: a link that stopped existing.
        for name in READER_LINKS:
            markup = _markup_of(TEMPLATES / name)
            anchors = re.findall(r"<a\b[^>]*>", markup, flags=re.S)
            reaching = [a for a in anchors
                        if "/reader/" in a or "/read/" in a or "entry.url" in a]
            self.assertTrue(reaching, f"{name} no longer links to a reader")

    def test_the_scan_can_see_an_opt_out(self):
        # Negative control: a scan for a string that is no longer anywhere
        # passes for good whether it works or not.
        self.assertIn('hx-boost="false"', '<a href="/reader/3" hx-boost="false">')


class ASnapshotTheLibraryHasOutgrownIsBroughtForward(unittest.TestCase):
    """Back stopped reaching the server, and this app's Back was made to
    revalidate on purpose (`no-cache` + an ETag on every HTML response).

    The live stream is what makes that payable: it survives a boosted swap
    (`hx-preserve`), so the document knows whether anything happened while
    a page was off screen. A snapshot nothing has happened since is served
    with zero requests of any kind; any other is still restored -- the page
    a moment ago rather than a blank -- and then re-synced (fetched and
    morphed, tests/test_resync.py) once the extension has put it back.
    """

    def setUp(self):
        source = (ROOT / "app/static/js/library-events.js").read_text()
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        self.js = "\n".join("" if line.lstrip().startswith("//") else line
                            for line in source.splitlines())

    def test_every_event_moves_the_counter(self):
        # Every event, not only the ones that changed something here: a
        # handler that found nothing to patch is exactly the case where
        # the page it would have patched is the one off screen.
        #
        # Bounded to the stream's own listener. The counter is bumped in
        # TWO places -- here and on a back/forward-cache restore -- so an
        # unbounded search finds the other one and this passes with the
        # per-event bump deleted.
        listener = self.js.split("stream.addEventListener(name,", 1)[1]
        self.assertIn(BUMP, listener.split("handle(data);", 1)[0])

    def _hit_listener(self):
        """The `cache:hit` listener alone, bounded at the closing `});` of
        its own registration."""
        return self.js.split('"htmx:history:cache:hit"', 1)[1].split("\n        });", 1)[0]

    def test_a_hit_still_holding_its_stamp_is_kept_untouched(self):
        # The property this whole mechanism is FOR: Back with nothing
        # changed in between is the snapshot, with zero requests. Both
        # halves of the stamp, and each one is a way it can be a lie -- a
        # document that is gone, or a count from before something
        # happened.
        listener = self._hit_listener()
        self.assertIn("stamp.doc === THIS_DOCUMENT", listener)
        self.assertIn("stamp.seen === window.libraryEventsSeen", listener)
        kept = listener.split("stamp.seen === window.libraryEventsSeen", 1)[1]
        self.assertLess(kept.index("return;"), kept.index("resync()"),
                        "an unchanged snapshot no longer short-circuits")

    def test_any_other_snapshot_is_restored_and_then_resynced(self):
        # Never refused: the restore goes ahead, and the re-sync waits for
        # it -- `cache:hit` fires BEFORE the snapshot is swapped in, and a
        # morph that landed first would be painted over by the snapshot.
        listener = self._hit_listener()
        self.assertNotIn("preventDefault", listener)
        self.assertIn('"htmx:history:cache:after:restore",\n'
                      '                                      () => resync(), { once: true }',
                      listener)

    def test_the_bundle_still_fires_after_restore_once_the_swap_is_done(self):
        # The extension's restore awaits its swap and then, a frame later,
        # puts the scroll back and fires this. That ordering is the whole
        # reason it is the hook.
        bundle = (ROOT / "app/static/src/vendor/htmax.min.js").read_text()
        restore = bundle.split("await htmx.swap(u)", 1)[1].split("htmx.registerExtension", 1)[0]
        self.assertIn('"htmx:history:cache:after:restore"', restore)

    def test_the_stamp_holds_no_cursor(self):
        # The cursor was for asking the server for a delta, which is gone.
        writer = self.js.split("sessionStorage.setItem(stampKey(", 1)[1].split("}));", 1)[0]
        self.assertIn("doc: THIS_DOCUMENT", writer)
        self.assertIn("seen: window.libraryEventsSeen", writer)
        self.assertNotIn("streamPosition", writer)

    def test_the_stamp_is_kept_PER_SNAPSHOT_and_not_in_a_variable(self):
        """The whole reason the plain counter was not enough.

        The extension stores each snapshot in `sessionStorage` under
        `htmx-history-<htmxId>`, which outlives the document; a counter on
        `window` dies with it. So a NEW document reads its own 0 against a
        baseline of 0 and agrees by coincidence, over a page that may be
        any amount out of date — which is what Firefox does on the
        ordinary way back from a reader, the reader not being boosted.
        """
        self.assertIn("sessionStorage.setItem(stampKey(", self.js)
        self.assertIn("sessionStorage.getItem(stampKey(", self.js)
        self.assertIn("THIS_DOCUMENT", self.js)
        # Not a module-level variable holding one baseline for all of them.
        self.assertNotIn("seenWhenSnapshotted", self.js)

    def test_the_stamp_is_written_going_FORWARD_only(self):
        """`htmx:history:cache:after:save` looks like the place and is a
        trap: a restore saves the page it is leaving too, and by then
        `history.state` has already moved to the entry being restored — so
        stamping there overwrites the very stamp the hit is about to
        check, with this document and this count. Measured: a stale
        snapshot stopped being caught at all, in both browsers.

        `htmx:before:history:update` fires only going forward and still
        names the entry being left, which is the one just snapshotted.
        """
        self.assertIn('"htmx:before:history:update"', self.js)
        writer = self.js.split('"htmx:before:history:update"', 1)[1]
        self.assertIn("history.state?.htmxId", writer)
        self.assertIn("sessionStorage.setItem(stampKey(id)", writer)
        self.assertNotIn('"htmx:history:cache:after:save"', self.js)

    def test_storage_that_throws_is_read_as_no_stamp(self):
        # Private mode, blocked site data. A re-sync costs one page, so
        # failing that way is cheap -- and
        # a catch that invented a PASSING stamp instead would hand the
        # reader a stale page exactly where storage is least trustworthy.
        reader = self.js.split("sessionStorage.getItem(stampKey(", 1)[1]
        catch = reader.split("} catch {", 1)[1].split("}", 1)[0]
        self.assertIn("stamp = null;", catch)
        self.assertNotIn("THIS_DOCUMENT", catch)

    def test_a_restore_invalidates_every_earlier_snapshot(self):
        """The two halves keep different company, and that is the bug this
        closes: the snapshots live in `sessionStorage` and outlive the
        document, while the counter lives on `window` and does not.

        So a document coming back out of the back/forward cache reads as
        "nothing has happened" against snapshots written before it went
        away — and an arbitrary amount of reading happened in between.
        Measured, home → series (boosted) → reader → Back → Back with no
        pause: the second Back was served the home page from before the
        reading, with **zero requests**. With the bump: one `GET /`, and
        the strip matching a full load.
        """
        # Bounded to the handler's own body. The counter is bumped in TWO
        # places -- here and once per event frame at the foot of the file
        # -- so an unbounded search finds the other one and this test
        # passes with the restore rule deleted.
        self.assertIn(BUMP, self._restore_body())

    def test_an_ordinary_load_does_not_bump_it(self):
        # Control: `pageshow` fires on every load, restore or not. Bumping
        # unconditionally would re-sync the very first snapshot a fresh
        # document takes, which is the cache never being used at all.
        handler = self.js.split('window.addEventListener("pageshow"', 1)[1]
        self.assertLess(handler.index("if (!event.persisted) return;"),
                        handler.index(BUMP),
                        "the bump runs before the persisted check")

    def _restore_body(self):
        """The `pageshow` handler, from its guard to the line that reads
        the stream -- and no further, so the per-event bump at the foot of
        the file cannot stand in for the one that belongs here."""
        handler = self.js.split('window.addEventListener("pageshow"', 1)[1]
        after_guard = handler.split("if (!event.persisted) return;", 1)[1]
        return after_guard.split("const stream =", 1)[0]

    def test_it_is_wired_once_per_document(self):
        # This file runs again after every boosted swap while `document`
        # does not, so without the guard a fifth navigation would add a
        # fifth listener and the counter would be reset under them.
        guard = self.js.index("window.libraryEventsRestoreBound")
        self.assertLess(guard, self.js.index('"htmx:history:cache:hit"'))
        self.assertLess(guard, self.js.index("window.libraryEventsSeen = 0;"))
