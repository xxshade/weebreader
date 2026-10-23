"""A page is never laid out in one font and drawn in another.

The paginator decides how many blocks fit by placing them, one at a time,
in the (still hidden) iframe that will show the page, and reading its
scrollHeight. Two things make that measurement lie,
and both are silent -- the page looks fine, it is just taller than the
screen, and the only symptom is that the last lines are gone:

  - A WEB FONT is fetched only once a document has text to draw with it,
    and `font-display: swap` means the browser lays that text out in a
    fallback face meanwhile. Measured on a 390px viewport with a cold
    cache: the first page fitted 7 blocks and rendered 899px tall in an
    807px viewport -- 92px below the fold -- while every later page, with
    the font warm, fitted exactly. That is the bug whose workaround was
    switching fonts back and forth to force a repagination.
  - Anything else that arrives late (an EPUB's own embedded face, a
    stylesheet the book loads itself) does the same thing, which is why
    the page is measured in the very document that is then shown: a
    prediction made in another one is about a different page.

Neither mechanism can be seen from Python -- they are browser layout --
so what is pinned here is that the call sites still exist. Removing one
costs nothing a test suite or a reviewer would notice.
"""
import re
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "app/static/js/reader-text.js"


def body_of(source, name):
    """One top-level function in the engine's IIFE, up to the next one."""
    start = re.search(r"^    (?:async )?function %s\(" % re.escape(name), source, re.M)
    assert start, f"no function {name} in reader-text.js"
    rest = source[start.end():]
    end = re.search(r"^    (?:async )?function ", rest, re.M)
    return rest[: end.start()] if end else rest


class PaginationMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.source = ENGINE.read_text()

    def test_every_height_measurement_waits_for_the_font_it_measures_with(self):
        # Where a page ends, and continuous scroll's one-shot read of a
        # whole chapter's height -- which has no page turn to correct it.
        for name in ("renderPage", "mountSection"):
            self.assertIn("waitForFonts(", body_of(self.source, name),
                          f"{name} measures a layout without waiting for its fonts")

    def test_the_font_wait_is_bounded(self):
        # An unreachable font must not hang pagination, the same rule
        # waitForImages already follows.
        self.assertIn("setTimeout(resolve, timeoutMs)", body_of(self.source, "waitForFonts"))

    def test_the_page_is_built_from_the_edge_the_direction_anchors(self):
        # Paging forward the page starts where the last one ended, so the
        # overflow comes off the END; paging backward it ends where the
        # current one starts, so it comes off the FRONT. Cut the wrong
        # edge and the reader loses a paragraph one way and re-reads one
        # the other.
        self.assertIn('renderPage(spineIndex, endExclusive, "backward", normal)',
                      body_of(self.source, "gotoPaginatedBackwardEnd"))
        self.assertIn('renderPage(spineIndex, start, "forward", normal)',
                      body_of(self.source, "gotoPaginatedForward"))
        paginator = ENGINE.with_name("reader-pagination.js").read_text()
        self.assertIn('page.prepend(unit) : page.append(unit)', paginator)

    def test_a_page_is_only_shown_once_it_has_been_measured(self):
        # Hidden while it is laid out, or the frame the reader sees is the
        # one that overflowed.
        render = body_of(self.source, "renderPage")
        self.assertIn("visibility:hidden", render)
        self.assertLess(render.index("visibility:hidden"), render.index("pagination.fill("))
        self.assertLess(render.index("pagination.fill("), render.index('visibility = "visible"'))

    def test_one_iframe_per_page(self):
        # The measuring iframe and its second layout are gone (20 iframes
        # per 10 turns before).
        self.assertNotIn("createMeasureIframe", self.source)
        self.assertEqual(body_of(self.source, "renderPage").count("createChapterIframe()"), 1)

    def test_an_overtaken_page_touches_nothing(self):
        # Two layouts at once each cleared the viewer under the other:
        # a page left with one paragraph and the rest blank, and the
        # indicator rewritten by the loser seconds later. Every await is
        # followed by the check, and the old page goes only after the last.
        render = body_of(self.source, "renderPage")
        self.assertIn("const gen = ++pgGeneration;", render)
        lines = [line.strip() for line in render.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            if line.startswith("await waitForImages("):
                continue  # checked once after its font wait, the line below
            if line.startswith("const chapter = await") or line.startswith("await "):
                self.assertIn("stale()", lines[i + 1], f"no staleness check after: {line}")
        self.assertLess(render.rindex("if (stale())"), render.index("child.remove()"))

    def test_a_key_for_a_control_does_not_turn_the_page(self):
        self.assertIn('closest("input, select, textarea, button, [contenteditable]")',
                      body_of(self.source, "handleKeydown"))


if __name__ == "__main__":
    unittest.main()
