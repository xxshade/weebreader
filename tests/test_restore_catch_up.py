"""A page carries where in the event sequence it was rendered.

A session-history navigation may be answered from the browser's HTTP
cache, and `Cache-Control: private, no-cache` does not forbid it -- only
`no-store` would, and that is the back/forward cache traded away. Such a
document never reaches the server, so only the page itself can say how old
it is, and its stream's FIRST connection carries no `Last-Event-ID`.

Measured in the field, Firefox, Back out of a reader:
`type: "back_forward", transferSize: 0, decodedBodySize: 318370`, the
server never saw the request, and the strip showed a percentage and an
order from before the reading.

So the stream's URL carries the render's own position (`?since=`), and
the stream compares it exactly as it compares a reconnect's
`Last-Event-ID` (tests/test_events.py drives that comparison). The same
value is on the element as `data-rendered-at`, for the one reader of it in
the browser: a re-sync, finding out how old the page it fetched is.
"""
import re
import unittest
from pathlib import Path

from app.library import events

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "app/static/js/library-events.js"
BASE = ROOT / "app/templates/base.html"


def _code_of(path: Path) -> str:
    """The script with its comments blanked out.

    These are scans for CODE, and the comments explain each rule by naming
    exactly what it forbids. Same treatment, same reason, as
    tests/test_reader_resume.py.
    """
    source = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in source.splitlines())


class APageCarriesWhenItWasRendered(unittest.TestCase):
    def markup(self):
        return re.sub(r"\{#.*?#\}", "", BASE.read_text(), flags=re.S)

    def test_the_stream_url_carries_it(self):
        self.assertIn('hx-sse:connect="/library/events?since={{ rendered_at | urlencode }}"',
                      self.markup())

    def test_the_element_carries_the_same_value(self):
        # One read of the position for both, so the two cannot disagree by
        # an event published between them.
        markup = self.markup()
        self.assertIn("{% set rendered_at = library_events_rendered_at() %}", markup)
        self.assertIn('data-rendered-at="{{ rendered_at }}"', markup)
        self.assertEqual(markup.count("library_events_rendered_at()"), 1)

    def test_the_stamp_is_the_current_position(self):
        source = (ROOT / "app/library/events.py").read_text()
        body = source.split("def rendered_at()", 1)[1].split("\n\ndef ", 1)[0]
        self.assertIn("return event_id(current_id())", body)

    def test_registering_really_puts_the_global_on_the_env(self):
        class FakeEnv:
            globals = {}

        class FakeTemplates:
            env = FakeEnv()

        templates = FakeTemplates()
        events.register(templates)
        rendered_at = templates.env.globals["library_events_rendered_at"]
        self.assertEqual(rendered_at(), events.event_id(events.current_id()))

    def test_the_rendered_page_says_it(self):
        # Through the real environment, which every router shares.
        from app.templating import templates

        html = templates.env.from_string(
            '{% set rendered_at = library_events_rendered_at() %}'
            '<a href="/library/events?since={{ rendered_at | urlencode }}">'
        ).render()
        self.assertIn(f"since={events.event_id(events.current_id())}", html)


class TheBrowserLeavesTheComparisonToTheServer(unittest.TestCase):
    """What the client no longer does, because the stream says it.

    It used to compare positions itself -- after a back/forward-cache
    restore, and on a document's first connection -- and had to wait out a
    grace for frames it could not see arrive (`htmx:sse:after:connection`
    fires when the `fetch` resolves, before a frame is read). The server
    answers before the first frame now, so there is nothing to wait for.
    """

    def setUp(self):
        self.js = _code_of(SCRIPT)

    def test_no_endpoint_is_asked_where_the_sequence_is(self):
        self.assertNotIn("/library/events/position", self.js)
        self.assertNotIn("/library/events/replay", self.js)

    def test_a_restore_only_waits_for_the_stream_to_come_back(self):
        restore = self.js.split('window.addEventListener("pageshow"', 1)[1]
        self.assertIn("const fallback = setTimeout(resync, RESUME_GRACE_MS);", restore)
        self.assertIn('"htmx:sse:after:connection",\n'
                      '                                    () => clearTimeout(fallback)',
                      restore)

    def test_the_routes_are_gone_too(self):
        from app.library.router import router

        paths = {getattr(route, "path", None) for route in router.routes}
        self.assertNotIn("/library/events/position", paths)
        self.assertNotIn("/library/events/replay", paths)
        self.assertIn("/library/events", paths)
