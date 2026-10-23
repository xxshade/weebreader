"""The SSE stream's ids, and what a (re)connecting page is told
(app/library/events.py, the stream in app/library/router.py).

What matters here is the difference between "you missed nothing" and
"you are behind" -- the second is what makes the page re-render itself,
and getting it wrong in either direction is silent: too eager and every
return to a tab costs a whole page, too lax and it sits on a stale screen
believing it is current.
"""
import asyncio
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.library import events

ROOT = Path(__file__).resolve().parent.parent


def _markup_of(path: Path) -> str:
    """The file with its comments blanked out.

    These are scans for MARKUP, and the comment beside the htmx-config meta
    tag explains the rule by naming the very attributes it forbids -- prose
    that reads as a breach of the rule it documents. Jinja `{# #}`, HTML
    `<!-- -->` and JS `//` all hide the same way.
    """
    source = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    return "\n".join("" if line.lstrip().startswith("//") else line
                      for line in source.splitlines())


class EventsCase(unittest.TestCase):
    def setUp(self):
        # Module-level state, deliberately: it is one broadcaster for one
        # process. Reset around each test rather than reaching for a class.
        self._saved = (events._counter, events._last_id, events.EPOCH)
        events._counter = __import__("itertools").count(1)
        events._last_id = 0
        self.addCleanup(self._restore)

    def _restore(self):
        events._counter, events._last_id, events.EPOCH = self._saved

    def publish(self, n, kind="item_added"):
        for i in range(n):
            events.publish({"type": kind, "series_id": i})


class Ids(EventsCase):
    def test_each_event_gets_the_next_id(self):
        self.publish(3)
        self.assertEqual(events.current_id(), 3)

    def test_the_id_carries_the_epoch(self):
        self.assertTrue(events.event_id(7).endswith("-7"))
        self.assertTrue(events.event_id(7).startswith(events.EPOCH))

    def test_a_transient_event_is_not_numbered(self):
        # Otherwise a scan's progress would tell every page it was behind.
        self.publish(1)
        events.publish({"type": "job_progress", "job_key": "scan:1"})
        self.assertEqual(events.current_id(), 1)

    def test_a_sequence_that_has_published_nothing_still_has_a_position(self):
        # Zero is an answer, not a missing one.
        self.assertEqual(events.current_id(), 0)
        self.assertTrue(events.is_current(events.event_id(0)))


class IsCurrent(EventsCase):
    def test_the_last_id_is_current(self):
        self.publish(3)
        self.assertTrue(events.is_current(events.event_id(3)))

    def test_one_behind_is_not(self):
        self.publish(3)
        self.assertFalse(events.is_current(events.event_id(2)))

    def test_ahead_is_not(self):
        # Ahead of anything this process published: another process's id
        # under a coincidentally equal epoch, or a forged one. Placing it
        # is impossible either way.
        self.publish(3)
        self.assertFalse(events.is_current(events.event_id(4)))

    def test_another_epoch_is_not(self):
        # The restart case. Without the epoch, a page that saw 3 would read
        # as current once a new process had published 3 events.
        self.publish(3)
        old = events.event_id(3)
        events.EPOCH = "another"
        self.assertFalse(events.is_current(old))
        self.assertTrue(events.is_current(events.event_id(3)))

    def test_nonsense_is_not(self):
        for seen in ("", "garbage", "-", f"{events.EPOCH}-x"):
            with self.subTest(seen=seen):
                self.assertFalse(events.is_current(seen))


class TheStreamSaysWhetherAPageIsBehind(EventsCase):
    """The real stream, driven frame by frame.

    Every connection opens with the page's standing: an `events_gap` when
    it is behind, then (always) an id-only position frame.
    """

    async def _frames(self, count, *, last_event_id=None, since="", then=None):
        from app.library.router import library_events_stream

        headers = {"last-event-id": last_event_id} if last_event_id else {}
        response = await library_events_stream(
            SimpleNamespace(headers=headers), since=since, user=None)
        stream = response.body_iterator
        try:
            frames = []
            for i in range(count):
                if then and i == then[0]:
                    then[1]()
                frames.append(await asyncio.wait_for(stream.__anext__(), 2))
            return frames
        finally:
            await stream.aclose()

    def frames(self, count, **kwargs):
        return asyncio.run(self._frames(count, **kwargs))

    def position(self):
        return f"id: {events.event_id(events.current_id())}\n\n"

    def test_a_page_that_missed_nothing_is_told_only_where_it_stands(self):
        self.publish(3)
        [first] = self.frames(1, last_event_id=events.event_id(3))
        self.assertEqual(first, self.position())

    def test_a_page_that_is_behind_is_told_so_first(self):
        self.publish(3)
        first, second = self.frames(2, last_event_id=events.event_id(1))
        self.assertIn(f"event: {events.GAP_EVENT}\n", first)
        self.assertNotIn("id:", first, "a gap is not a point to resume from")
        self.assertEqual(second, self.position(),
                         "the page's next reconnect would be told again")

    def test_since_stands_in_for_a_first_connection(self):
        # The page the browser served from its own cache: no Last-Event-ID,
        # only the render's own stamp.
        self.publish(3)
        first, _ = self.frames(2, since=events.event_id(2))
        self.assertIn(events.GAP_EVENT, first)
        [only] = self.frames(1, since=events.event_id(3))
        self.assertEqual(only, self.position())

    def test_the_browser_id_wins_over_since(self):
        # A reconnect still carries the URL it was opened with, from
        # however long ago the page was rendered; the id is where it is.
        self.publish(3)
        [only] = self.frames(1, last_event_id=events.event_id(3),
                             since=events.event_id(1))
        self.assertEqual(only, self.position())

    def test_a_page_with_no_standing_is_not_told_it_is_behind(self):
        # A client that says nothing (not this app's base.html) has
        # nothing to be compared; it is given a position and no more.
        self.publish(3)
        [only] = self.frames(1)
        self.assertEqual(only, self.position())

    def test_a_restart_makes_every_old_page_behind(self):
        self.publish(3)
        old = events.event_id(3)
        events.EPOCH = "rebooted"
        first, _ = self.frames(2, last_event_id=old)
        self.assertIn(events.GAP_EVENT, first)

    def test_what_comes_next_is_delivered_live(self):
        self.publish(1)
        frames = self.frames(2, last_event_id=events.event_id(1),
                             then=(1, lambda: self.publish(1, "series_updated")))
        self.assertEqual(frames[0], self.position().replace("-2", "-1"))
        self.assertIn(f"id: {events.event_id(2)}\nevent: series_updated\n", frames[1])

    def test_a_stream_too_far_behind_is_told_so_and_moved_on(self):
        # events.publish swaps a full queue for GAP_MARKER; this is the
        # half that reaches the browser. The position after the gap is
        # what keeps the page's next reconnect from being told again.
        self.publish(1)
        overflow = lambda: self.publish(events.MAX_PENDING_PER_STREAM + 5, "series_updated")
        frames = self.frames(3, last_event_id=events.event_id(1), then=(1, overflow))
        self.assertIn(f"event: {events.GAP_EVENT}\n", frames[1])
        self.assertEqual(frames[2], self.position())


class ThePositionFrame(unittest.TestCase):
    """Why the position frame is not the forbidden unnamed one.

    `EveryFrameIsNamed` exists because the extension SWAPS an unnamed frame
    into the page as HTML. This frame has no name and is still safe, for a
    reason worth pinning rather than remembering: the extension only swaps
    a frame that has DATA. Its parser yields `hasId` for an id-only frame
    and the consumer sets the last id and then skips it, having neither
    data nor a name.
    """

    ROUTER = ROOT / "app/library/router.py"

    def helper(self):
        source = self.ROUTER.read_text()
        return source.split("def position(", 1)[1].split("\n    def ", 1)[0]

    def test_the_frame_carries_an_id_and_nothing_else(self):
        body = self.helper().split('"""', 2)[2]
        self.assertIn('return f"id: ', body, "the position frame lost its id")
        self.assertNotIn("data:", body, "a frame with data would be swapped into the page")
        self.assertNotIn("event:", body, "a name here would need a handler, and drift with it")


class AStalledStreamCannotGrowWithoutBound(EventsCase):
    """Each open stream has its own queue, and it used to have no limit.

    A scan publishes one event per FILE, so a tab whose connection has
    stopped draining -- a phone that slept, a socket a proxy is no longer
    carrying but has not closed either -- accumulated the whole library in
    memory, for as long as the socket stayed half-open. It is the one
    shape where keeping everything is wrong: that stream is already going
    to need a reload, and what is worth protecting is the process it is
    attached to.

    The answer is the one a reconnect from a page that is behind gets --
    `events_gap`, which the client knows how to handle.
    """

    def test_a_stream_that_keeps_up_gets_every_event(self):
        queue = events.subscribe()
        self.addCleanup(events.unsubscribe, queue)
        for _ in range(5):
            events.publish({"type": "series_updated", "series_id": 1})
        self.assertEqual(queue.qsize(), 5)
        self.assertNotEqual(queue.get_nowait(), events.GAP_MARKER)

    def test_a_stream_that_stops_draining_is_capped(self):
        queue = events.subscribe()
        self.addCleanup(events.unsubscribe, queue)
        for _ in range(events.MAX_PENDING_PER_STREAM + 50):
            events.publish({"type": "series_updated", "series_id": 1})
        self.assertLessEqual(queue.qsize(), events.MAX_PENDING_PER_STREAM)

    def test_what_is_left_tells_the_client_it_is_behind(self):
        # Not the newest events: the stream cannot say how much it missed,
        # and half a history is worse than an honest gap.
        queue = events.subscribe()
        self.addCleanup(events.unsubscribe, queue)
        for _ in range(events.MAX_PENDING_PER_STREAM + 50):
            events.publish({"type": "series_updated", "series_id": 1})
        self.assertEqual(queue.get_nowait(), events.GAP_MARKER)

    def test_one_stalled_stream_does_not_starve_a_healthy_one(self):
        stalled = events.subscribe()
        healthy = events.subscribe()
        self.addCleanup(events.unsubscribe, stalled)
        self.addCleanup(events.unsubscribe, healthy)
        for _ in range(events.MAX_PENDING_PER_STREAM + 10):
            events.publish({"type": "series_updated", "series_id": 1})
            healthy.get_nowait()
        events.publish({"type": "series_updated", "series_id": 2})
        # The healthy one is still being handed real events...
        self.assertNotEqual(healthy.get_nowait(), events.GAP_MARKER)
        # ...and the ids kept moving, so a reconnect is still compared
        # against where the sequence really is.
        self.assertGreater(events.current_id(), events.MAX_PENDING_PER_STREAM)

    def test_the_stream_turns_the_marker_into_a_gap(self):
        # The queue side is above; this is the half that reaches the
        # browser, and the two have to agree about the same tuple.
        source = (Path(__file__).resolve().parent.parent
                  / "app/library/router.py").read_text()
        self.assertIn("events.GAP_MARKER", source)
        self.assertIn("events.GAP_EVENT", source)
        # And the browser has to be listening under that same name, now
        # that the name is what reaches it rather than a field it reads.
        js = (Path(__file__).resolve().parent.parent
              / "app/static/js/library-events.js").read_text()
        self.assertIn(f"{events.GAP_EVENT}:", js)


class EveryFrameIsNamed(unittest.TestCase):
    """The failure mode this protocol has, and nothing else does.

    The browser reads this stream through htmx's hx-sse extension, which
    DISPATCHES a named message as a DOM event and SWAPS an unnamed one into
    the page as HTML. So a frame that reaches a client without an `event:`
    line does not go unnoticed -- it paints its own JSON onto the screen.

    Two halves have to hold for that to be impossible: every frame the
    server writes carries a name, and every name the server can write has
    somebody listening for it.
    """

    ROUTER = ROOT / "app/library/router.py"
    JS = ROOT / "app/static/js/library-events.js"

    def test_the_router_writes_no_frame_by_hand(self):
        # One helper writes every frame, which is what makes "always named"
        # checkable at all -- a second f-string with a `data:` in it would
        # be a second place to forget.
        source = self.ROUTER.read_text()
        stream = source.split("async def library_events_stream", 1)[1].split("\n@router", 1)[0]
        self.assertEqual(stream.count('f"{head}event: {name}\\ndata: {payload}'), 1)
        for line in stream.splitlines():
            if "data:" in line and "yield" in line:
                self.fail(f"a frame written outside the helper: {line.strip()}")

    def test_the_helper_always_emits_an_event_line(self):
        source = self.ROUTER.read_text()
        helper = source.split("def frame(", 1)[1].split("\n    def ", 1)[0]
        # The id line is conditional; the event line must not be.
        self.assertIn('head = f"id: ', helper)
        self.assertIn("if number else \"\"", helper)
        self.assertNotIn("if name", helper)

    def published_types(self):
        """Every `type` an events.publish() call site can send.

        Scanned rather than imported because the publishers are spread over
        five routers and there is no registry -- which is exactly why the
        two sides can drift.
        """
        found = set()
        for path in (ROOT / "app").rglob("*.py"):
            source = path.read_text()
            for match in re.finditer(r"events\.publish\(", source):
                # The call's own text, to the line that closes it. Types are
                # literals in every one of them; a computed one would show
                # up here as a miss and should.
                window = source[match.end():match.end() + 400]
                name = re.search(r'"type":\s*"([a-z_]+)"', window)
                if name:
                    found.add(name.group(1))
        return found

    def test_the_scan_finds_the_publishers(self):
        # A regex that quietly stopped matching would make the contract
        # test below vacuous, so pin the count and a known member.
        types = self.published_types()
        self.assertGreaterEqual(len(types), 10, f"only found {sorted(types)}")
        self.assertIn("progress_updated", types)
        self.assertIn("job_progress", types)

    def handler_names(self):
        """The keys of the `handlers` object, and only those.

        Scoped to that object rather than to any 8-space-indented key: the
        same file holds two colour maps whose keys are status words, and a
        looser pattern read `ongoing` and `plan_to_read` as event names.
        """
        js = self.JS.read_text()
        block = js.split("const handlers = {", 1)[1].split("\n    };", 1)[0]
        return set(re.findall(r"^        ([a-z_]+):", block, re.M))

    def test_the_handler_scan_reads_the_right_object(self):
        names = self.handler_names()
        self.assertIn("progress_updated", names)
        self.assertNotIn("ongoing", names)      # a badge colour, not an event
        self.assertGreaterEqual(len(names), 12)

    def test_the_browser_listens_for_every_type_the_server_sends(self):
        missing = self.published_types() - self.handler_names()
        self.assertEqual(missing, set(),
                         "published with no handler -- the event arrives and nothing happens")

    def test_the_browser_listens_for_nothing_the_server_cannot_send(self):
        extra = self.handler_names() - self.published_types() - {events.GAP_EVENT}
        self.assertEqual(extra, set(), "a handler for an event nothing publishes -- dead code")

    def test_an_event_with_no_type_still_gets_a_name(self):
        # The one case that would reach the browser unnamed. It must not be
        # the empty string, whatever else it is.
        self.assertTrue(events._name_of({}))
        self.assertTrue(events._name_of({"type": ""}))
        self.assertEqual(events._name_of({"type": "x"}), "x")


class TheStreamIsWiredToTheExtension(unittest.TestCase):
    BASE = ROOT / "app/templates/base.html"

    def test_the_element_carries_the_connect_attribute(self):
        html = self.BASE.read_text()
        self.assertIn('id="library-events" hx-sse:connect="/library/events?since=', html)

    def test_the_extension_ships_in_the_same_file_as_htmx(self):
        """The race that made this connect only sometimes, and then twice.

        Two files put the extension's registration in a different deferred
        execution from htmx's, and htmx 4 with `defer` schedules its first
        pass with setTimeout rather than waiting for DOMContentLoaded -- so
        a timer can run between them. Measured with the extension delayed
        350ms on a cold cache: 0 of 10 loads connected, `_htmx` on the
        element left an empty object. Script order did not help; deferred
        scripts execute in document order and the browser waits for each,
        so a slow response loses wherever the tag sits. Processing the
        element again from app code fixed that and opened 2 streams on a
        warm load, because the extension's duplicate guard is only set once
        the response arrives.

        One file has no window: the extension is registered before htmx's
        first pass can run, and that pass connects exactly once.
        """
        html = self.BASE.read_text()
        self.assertIn("vendor/htmax.min.js", html)
        self.assertNotIn("vendor/hx-sse.min.js", html)
        self.assertNotIn("vendor/htmx.min.js", html)
        # And nothing may re-process the element, which is what opened the
        # second stream.
        js = (ROOT / "app/static/js/library-events.js").read_text()
        self.assertNotIn("htmx.process(stream)", js)

    def test_the_whitelist_still_lets_the_stream_register(self):
        """The bundle carries eleven extensions and registers all of them
        unless told otherwise. Two are named now -- `sse` and
        `history-cache`, which hx-boost made live (see
        tests/test_boost.py); the other nine stay out.

        The whitelist is a COST decision, not a safety one: every one of
        the nine is gated on an attribute this app does not use, so
        registering them changes no behaviour. What it buys is the
        per-swap work of nine idle hooks -- `hx-live` XPath-scans every
        processed subtree. A 40-element `htmx.swap()`, median of 50 warm
        rounds over three runs each: 5.60 / 5.70 / 5.70 ms against
        6.50 / 6.60 / 6.70 ms.

        This test is the STREAM's stake in it: htmx splits the value on
        /,\\s*/, so a separator typed as a space is one name matching
        nothing and the stream never opens at all. Measured with a space:
        /library/events was never requested.
        """
        html = self.BASE.read_text()
        content = re.search(r'name="htmx-config"\s+content=\'([^\']+)\'', html)
        self.assertIsNotNone(content)
        names = re.split(r",\s*", json.loads(content.group(1))["extensions"])
        self.assertIn("sse", names)

    def test_htmx_owns_the_history_entries_now(self):
        """The inversion this file used to pin the other way round.

        `history-cache` only ever runs inside htmx's history lifecycle,
        and until hx-boost this app never entered it -- navigation was
        document-to-document, for which the Navigation API reports
        `canIntercept=false`, so htmx's interceptor returned without
        acting (measured, back from /series/1 to /: a full document load
        of 15 requests, no `HX-History-Restore-Request`, no
        `htmx:before:history:restore`).

        `hx-boost` on `<body>` is what inverted it, and it is the ONLY
        one of the four that did. Nothing pushes a URL by hand, and the
        day something does, the history entries stop being one per
        navigation and this needs re-deciding rather than re-wording.
        """
        by_hand = ("hx-push-url", "hx-replace-url", "hx-history-elt")
        sources = list((ROOT / "app/templates").rglob("*.html"))
        sources += list((ROOT / "app/static/js").glob("*.js"))
        self.assertTrue(sources, "found no templates or scripts to scan")
        for path in sources:
            markup = _markup_of(path)
            for attr in by_hand:
                self.assertNotIn(attr, markup, f"{path.name} pushes a URL by hand ({attr})")
        # The one that IS there, and where. Boosting from anywhere but
        # <body> would leave whole pages loading documents.
        self.assertIn('hx-boost:inherited="true"', _markup_of(self.BASE))
        # A negative control, both ways round: the stripper has to leave
        # real attributes alone (or this test passes by seeing nothing at
        # all), and it has to actually strip (or the scan above is only
        # reporting that nobody wrote the names in prose).
        # `hx-browser-indicator` is named only in a comment in base.html,
        # which is exactly the shape the scan has to see through.
        self.assertIn("hx-sse:connect", _markup_of(self.BASE))
        self.assertIn("hx-browser-indicator", self.BASE.read_text(),
                      "the comment this stripper exists for is gone; simplify the test")
        self.assertNotIn("hx-browser-indicator", _markup_of(self.BASE))

    def test_nothing_is_dispatched_before_the_handlers_exist(self):
        # htmx initializes on a timer once its deferred script has run, so
        # the stream can connect -- and deliver -- before the deferred
        # library-events.js has bound anything. Measured in Chromium on a
        # document that arrived at once: `events_gap` at 84ms, DOM ready at
        # 86, no handler, a stale page. So an inline script right after the
        # element holds every message, and the handlers release it.
        html = _markup_of(self.BASE)
        element = html.index('<div id="library-events"')
        hold = html.index('"htmx:sse:before:message"', element)
        self.assertLess(hold, html.index("js/library-events.js"),
                        "the hold is not in place before the handlers' file")
        inline = html[element:hold]
        self.assertIn("<script>\n", inline, "the hold is not an inline, un-deferred script")
        self.assertIn("event.detail.waitUntil(stream.libraryEventsHeld)", html)
        js = _markup_of(ROOT / "app/static/js/library-events.js")
        bound = js.split("stream.libraryEventsBound = true;", 1)[1]
        self.assertIn("stream.libraryEventsRelease?.();", bound,
                      "nothing releases the held messages once the handlers exist")
        self.assertLess(bound.index("stream.addEventListener(name,"),
                        bound.index("stream.libraryEventsRelease?.();"))

    def test_the_bundle_still_offers_the_hold(self):
        # A message waits on whatever `waitUntil` was handed, before it is
        # dispatched by name.
        bundle = (ROOT / "app/static/src/vendor/htmax.min.js").read_text()
        message = bundle.split('"htmx:sse:before:message"', 1)[1][:200]
        self.assertIn("await Promise.all(i)", message)
        before = bundle.split('"htmx:sse:before:message"', 1)[0][-200:]
        self.assertIn("waitUntil:e=>i.push(Promise.resolve(e))", before)

    def test_the_bundle_really_carries_the_sse_extension(self):
        # Vendoring the wrong build (plain htmx.min.js under this name)
        # would leave every assertion above green and the stream dead.
        bundle = (ROOT / "app/static/src/vendor/htmax.min.js").read_text()
        self.assertIn("hx-sse", bundle)
        self.assertIn("text/event-stream", bundle)


class LeavingThePageIsNotAnError(unittest.TestCase):
    """A console line on every reload and every navigation, in Firefox.

        htmx: htmx:sse:error: Error in input stream TypeError: ...

    Leaving the page kills the open stream, and the extension's reader loop
    keeps quiet about a break only when

        h.abortController?.signal?.aborted || "AbortError" === t.name

    Chrome tears a fetch down with an `AbortError` and satisfies the second
    half, which is why nothing shows there -- measured, a Chromium run of
    reload, navigate-away and back logs nothing at all. Firefox raises a
    plain `TypeError` and satisfies neither. And htmx `console.error`s any
    event whose detail carries an `error` BEFORE dispatching it, so no
    listener and no config can silence one afterwards; the only way out is
    to stop the event being triggered.

    Two things make that work, and the first is WHEN. Firefox kills the
    stream BETWEEN `beforeunload` and `pagehide` -- measured on a link
    navigation: beforeunload 1888ms, the error 1897ms, pagehide 1918ms --
    so doing this at `pagehide` was too late and changed nothing.

    The second is WHAT. A sentinel, not a real `abort()`: all the extension
    reads is `signal.aborted`, and an abort landing inside a reconnect's
    own `fetch` is read by the loop as `break`, which closes the stream for
    good. At `beforeunload` there is no telling whether the page is going
    away or into the back/forward cache, where that stream is exactly what
    catches it up (measured with bfcache on: restored, reconnected, and a
    series removed while the page was frozen was gone from the grid).

    Measured in both browsers, same script, reload + navigate + back:
    3 logged errors in Firefox before, 0 after; 0 in Chromium either way.
    """

    JS = ROOT / "app/static/js/library-events.js"
    BUNDLE = ROOT / "app/static/src/vendor/htmax.min.js"

    def handler(self):
        code = self.JS.read_text()
        self.assertIn('window.addEventListener("beforeunload"', code,
                      "nothing marks the break before Firefox makes it")
        return code.split('window.addEventListener("beforeunload"', 1)[1].split("});", 1)[0]

    def test_it_says_the_break_is_expected(self):
        self.assertIn("sse.abortController = EXPECTED_BREAK", self.handler(),
                      "the break is still the browser's, so the extension reports it")
        self.assertIn("EXPECTED_BREAK = { signal: { aborted: true }", self.JS.read_text(),
                      "the sentinel no longer says what the extension reads")

    def test_it_is_not_an_abort(self):
        """The one this must NOT do.

        `abort()` inside a reconnect's own fetch is read as `break` and the
        stream never comes back -- and a page going into the back/forward
        cache needs exactly that reconnect.
        """
        self.assertNotIn("abortController.abort()", self.handler(),
                         "this aborts the connection it is only meant to label")
        self.assertNotIn("abortController?.abort", self.handler())

    def test_it_is_early_enough_to_matter(self):
        # `pagehide` is where this started and it was measurably too late.
        code = self.JS.read_text()
        self.assertNotIn('window.addEventListener("pagehide"', code,
                         "back to a hook Firefox fires after it has already logged")

    def test_the_bundle_still_judges_the_break_by_our_signal(self):
        # THE assumption. If an upgrade changes this check, the handler
        # above becomes a no-op and the console line is back -- silently,
        # because nothing else depends on it. The file is byte-pinned
        # (test_htmx_version.py), so this can only break on a deliberate
        # upgrade, which is exactly when it should.
        self.assertIn('h.abortController?.signal?.aborted||"AbortError"===t.name',
                      self.BUNDLE.read_text(),
                      "the sse extension no longer suppresses our own abort")

    def test_and_still_logs_before_anyone_can_stop_it(self):
        # The other half of why this is a sentinel and not a listener.
        self.assertIn("console.error(i,r.error,{elt:e,detail:r})", self.BUNDLE.read_text(),
                      "htmx may no longer log errors itself; a listener might do now")

    def test_the_connection_is_still_where_this_reaches_for_it(self):
        # `_htmx.sse` is a private of the bundle. Pinned for the same
        # reason: an upgrade that moves it makes the handler a no-op.
        self.assertIn(".htmxProp(o).sse=", self.BUNDLE.read_text(),
                      "the extension no longer stores its connection on the element")

    def test_the_loop_replaces_the_sentinel_on_its_next_attempt(self):
        # Why this clears itself rather than muting the stream for good.
        self.assertIn("h.abortController=c", self.BUNDLE.read_text(),
                      "a reconnect no longer installs its own controller")


class TheStreamSurvivesAWholeBodySwap(unittest.TestCase):
    """`hx-preserve`, and the two things it makes this file responsible for.

    Nothing swaps `<body>` today. The one navigation that would -- an
    `hx-boost`-style one -- replaces every child of `<body>`, and
    `#library-events` is one of them. Without `hx-preserve` htmx destroys
    the element, which tears the connection down: measured over 5 boosted
    navigations, 5 closes, 5 reconnects, and 5 uncaught `AbortError:
    BodyStreamBuffer was aborted` (the vendored bundle's sse cleanup calls
    `abortController.abort()` and then `reader.cancel()`, discarding the
    promise the second returns -- which rejects, because the abort has just
    errored that stream). With it: 0, 0 and 0, one connection held across
    all five, and the server's subscriber count 1 throughout either way.

    What `hx-preserve` changes is that the ELEMENT now outlives the file
    that wires it: library-events.js is executed again for every swap while
    the element is not replaced. Two things follow, and both were measured
    rather than reasoned:

    1. Binding per RUN instead of per ELEMENT accumulates. 14 listeners on
       the first page, then 27, 40, 53 -- one event handled once per page
       visited. Same for the `pageshow` reload: 1, then 2, 3, 4.
    2. Anything the handlers captured at load time goes stale. The banner
       is the only such capture, and with the guard above and no fix it
       never appears at all: the handler un-hides the banner belonging to
       the page this file FIRST ran on, which the swap detached. Measured
       with exactly that combination: the live banner stayed hidden and its
       text stayed at the template default.

    Why the bug hid: the duplication masks the staleness. With five copies
    bound, the newest holds the live banner, so it shows. Fix only the
    duplication and the first run's stale copy is the one left.
    """

    BASE = ROOT / "app/templates/base.html"
    JS = ROOT / "app/static/js/library-events.js"

    def code(self):
        return _markup_of(self.JS)

    def at(self, code, needle, why):
        """Where `needle` is, failing with `why` rather than a ValueError."""
        self.assertIn(needle, code, why)
        return code.index(needle)

    def test_the_stream_element_is_preserved(self):
        html = _markup_of(self.BASE)
        tag = re.search(r"<div id=\"library-events\"[^>]*>", html)
        self.assertIsNotNone(tag, "the stream element is gone from base.html")
        self.assertIn("hx-preserve", tag.group(0),
                      "a <body> swap would destroy the stream element and its connection")

    def test_whatever_is_preserved_is_addressable_by_id(self):
        # htmx finds the live copy with document.getElementById(e.id) --
        # an hx-preserve element without an id is silently not preserved,
        # which looks exactly like not having written the attribute.
        for path in (ROOT / "app/templates").rglob("*.html"):
            for tag in re.findall(r"<[^>]*hx-preserve[^>]*>", _markup_of(path)):
                self.assertIn("id=", tag, f"{path.name} preserves an element with no id: {tag}")

    def test_the_handlers_bind_once_per_element(self):
        code = self.code()
        guard = self.at(code, "!stream.libraryEventsBound",
                        "nothing stops this file binding the handlers a second time")
        claim = self.at(code, "stream.libraryEventsBound = true",
                        "the guard is never satisfied, so the handlers never bind")
        binding = self.at(code, "for (const [name, handle] of Object.entries(handlers))",
                          "the handler loop is gone")
        self.assertLess(guard, claim, "the flag is set before it is tested")
        self.assertLess(claim, binding, "the handlers are bound outside the guard")

    def test_the_restore_reload_binds_once_per_document(self):
        # The window outlives the swap even though the element does not.
        code = self.code()
        guard = self.at(code, "!window.libraryEventsRestoreBound",
                        "nothing stops a second pageshow listener being added")
        listener = self.at(code, 'window.addEventListener("pageshow"',
                           "the restore reload is gone")
        self.assertLess(guard, listener, "pageshow is registered outside the guard")

    def test_a_restore_lets_the_stream_catch_up_before_reloading(self):
        """The reload is the fallback now, not the answer.

        A restored page has its DOM back and a stream that was down, and
        the stream knows where it stopped: the extension sends its last id
        on reconnect and the server says whether anything was published
        meanwhile (`events_gap`, which re-syncs). Throwing the page away
        was more than that needs.

        Measured with the pause/resume the extension already runs for a
        backgrounded tab -- the same reconnect a restore needs -- against a
        synthetic restore in the same browser: with the stream back, the
        document survived, reconnected in 482ms and the series removed
        while it was away was gone from the grid; with no stream coming
        back, the page was still there at 1.5s and reloaded by 4.5s.
        """
        code = self.code()
        restore = code.split('window.addEventListener("pageshow"', 1)[1]
        self.assertNotIn("if (event.persisted) window.location.reload();", code,
                         "a restore still reloads before the stream can answer")
        self.assertIn('"htmx:sse:after:connection"', restore,
                      "nothing waits for the stream to come back")
        self.assertIn("clearTimeout(fallback)", restore,
                      "the reconnect does not call the reload off")
        self.assertIn("RESUME_GRACE_MS", restore, "the wait has no bound")

    def test_a_restore_with_no_stream_to_wait_for_still_reloads(self):
        # Waiting for a reconnect that cannot happen would leave the page
        # stale for good. A reader page is exactly this: it opts out of the
        # stream entirely (see reader/comic.html).
        restore = self.code().split('window.addEventListener("pageshow"', 1)[1]
        self.assertIn("if (!stream)", restore, "nothing checks there is a stream at all")
        self.assertLess(restore.index("if (!stream)"), restore.index("setTimeout"),
                        "a page with no stream waits for one anyway")

    def test_the_grace_is_long_enough_for_the_reconnect_it_waits_for(self):
        # The extension's own backoff is 500ms plus up to 30% jitter, and
        # the resume measured here was 482-558ms. A bound under a second
        # would reload the pages this exists to stop reloading.
        code = self.code()
        grace = int(re.search(r"RESUME_GRACE_MS = (\d+)", code).group(1))
        self.assertGreaterEqual(grace, 1000, "the fallback fires before a normal reconnect")

    def test_the_banner_is_found_when_it_is_shown_not_when_this_file_runs(self):
        code = self.code()
        self.assertNotIn("const banner = document.getElementById", code,
                         "the banner is captured at load time and goes stale across a swap")
        self.assertNotIn("const bannerText = document.getElementById", code)
        lookups = code.count('getElementById("library-update-banner")')
        self.assertEqual(lookups, 1, "the banner is looked up in more than one place")
        body = code.split("function bannerElement()", 1)[1].split("}", 1)[0]
        self.assertIn('getElementById("library-update-banner")', body,
                      "the one lookup is not the one inside bannerElement()")

    def test_the_stripper_is_not_what_makes_those_pass(self):
        # Negative control, both ways. These comments explain the rules by
        # naming what they forbid, and a stripper that ate too much would
        # let a real capture through unnoticed.
        self.assertIn("hx-preserve", self.JS.read_text(),
                      "the comment this stripper exists for is gone; simplify the test")
        self.assertNotIn("hx-preserve", self.code())
        self.assertIn("function bannerElement()", self.code())
        self.assertIn('id="library-events"', _markup_of(self.BASE))


if __name__ == "__main__":
    unittest.main()
