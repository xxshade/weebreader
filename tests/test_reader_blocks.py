"""A chapter is shown whole in Paginated mode, the same as in Continuous.

The paginator used to build its pages out of a flat
`querySelectorAll("p, h1..h6, li, blockquote, pre, td, figure, dd, dt")`,
one outerHTML per match. Measured with that extraction, the same in
Chromium and Firefox: a <th>, a <caption>, an <hr>, text straight inside a
<div>, an image in a <div> and text loose between paragraphs never reached
a page; every <td> left its table, so a character sheet read as one line
of run-together text; a <p> inside a <blockquote>, <li> or <td> was shown
twice; and the book's own stylesheet was not on the page at all.

Driving the real reader through a book built to hold all of that -- every
page forward to the end and back again, 390 and 800 px wide, both browsers
-- the text on the pages now equals continuous scroll's rendering of the
same chapters, with no page overflowing the screen. The same run on main
differed at the first table and had a 15-paragraph quotation as one page
1137 px too tall.

What a block IS lives in reader-blocks.js and is browser behaviour (it
walks a real DOM), so it is covered by those runs. Pinned here is what keeps
the parts agreeing -- the two modes counting the same blocks, the page that
is measured being the page that is shown -- which is exactly what a later
edit would break without anything visibly failing.
"""
import re
import unittest
from pathlib import Path

import httpx

from app.novels.scraper import fetch_chapter_content
from tests.test_reader_pagination import body_of

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "app/static/js/reader-text.js"
BLOCKS = ROOT / "app/static/js/reader-blocks.js"


def _set(source: str, name: str) -> set:
    """The string members of `const NAME = new Set([...])` in reader-blocks.js."""
    literal = re.search(r"const %s = new Set\(\[(.*?)\]\);" % name, source, re.S)
    assert literal, f"no Set {name} in reader-blocks.js"
    return set(re.findall(r'"([a-z0-9]+)"', literal.group(1)))


class BothModesCountTheSameBlocks(unittest.TestCase):
    """A position is `elidx:N`, the Nth block. Continuous scroll and the
    paginator each count them, and a switch between the modes -- or a
    position saved in one and opened in the other -- lands on whatever
    block N is in the OTHER count."""

    def setUp(self):
        self.engine = ENGINE.read_text()

    def test_both_ask_reader_blocks(self):
        self.assertIn("window.readerBlocks.collect(doc.body)", body_of(self.engine, "getBlocks"))
        self.assertIn("window.readerBlocks.collect(doc.body)", body_of(self.engine, "loadChapterBlocks"))

    def test_neither_keeps_a_selector_of_its_own(self):
        # The old flat selector is the bug; one reappearing in either half
        # is a second definition of "block" to disagree with the first.
        for name in ("getBlocks", "loadChapterBlocks"):
            self.assertNotIn("querySelectorAll", body_of(self.engine, name), name)


class ATabOpenedBeforeTheUpdateStillReads(unittest.TestCase):
    """reader-blocks.js is loaded from <head>, which a boosted visit never
    replaces -- so a tab (an installed PWA, typically) opened before this
    shipped keeps a <head> without it, and boosts into a reader whose engine
    needs it. Measured with that <head> simulated, both browsers, both
    readers: the reader never rendered, with no error on the page. With the
    guard it loads once, lands on the reader's URL at the saved position,
    and an ordinary boosted visit still stays in the same document."""

    def test_the_engine_reloads_when_its_dependency_was_never_loaded(self):
        engine = ENGINE.read_text()
        guard = ("if ((!window.readerBlocks && !document.querySelector('script[src*=\"/reader-blocks.js\"]')) ||\n"
                 "            (!window.readerPagination && !document.querySelector('script[src*=\"/reader-pagination.js\"]'))) {\n"
                 "        window.location.reload();\n"
                 "        return;\n"
                 "    }")
        self.assertIn(guard, engine)
        # Before anything is bound or fetched.
        self.assertLess(engine.index(guard), engine.index("function bindOutside("))


class ASwitchToPaginatedStartsWhereContinuousWas(unittest.TestCase):
    """Found driving the round trip above: continuous scroll was hidden
    BEFORE the position was read off it, and with no layout every offset
    reads 0 -- so the paginator opened on the last block of the last
    chapter mounted, every time, both browsers (a page starting at block 1
    of 12 came back as block 76 of 76 in the next chapter)."""

    def test_the_position_is_read_before_the_scroll_is_hidden(self):
        switch = body_of(ENGINE.read_text(), "applyFlow").split("} else {", 1)[1]
        self.assertLess(switch.index("measureCustomPosition()"),
                        switch.index('getElementById("custom-scroll").classList.add("hidden")'))


class ThePageMeasuredIsThePageShown(unittest.TestCase):
    """The paginator used to predict a page in a hidden iframe and draw it
    in another; built differently -- other stylesheets, containers merged
    in one and not the other -- the prediction was about a different page.
    It now lays the page out in the document it shows."""

    def setUp(self):
        self.engine = ENGINE.read_text()

    def test_the_page_carries_the_chapters_shell(self):
        render = body_of(self.engine, "renderPage")
        self.assertIn("buildSectionDoc(chapter.shell, adapter.baseHrefFor(entry))", render)
        self.assertIn("pagination.fill(doc.body, blocks", render)


class WhatCountsAsOneBlock(unittest.TestCase):
    def setUp(self):
        self.blocks = BLOCKS.read_text()

    def test_a_table_is_one_block_and_its_cells_are_not_blocks(self):
        whole = _set(self.blocks, "WHOLE")
        counted = _set(self.blocks, "COUNTED")
        self.assertIn("table", whole)
        for cell in ("td", "th", "tr", "caption"):
            self.assertNotIn(cell, whole | counted, cell)

    def test_a_chapter_of_plain_paragraphs_is_numbered_as_it_was(self):
        # Saved positions in ordinary chapters must not move: these were
        # blocks before and are still blocks whatever they hold.
        whole = _set(self.blocks, "WHOLE")
        for name in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "pre"):
            self.assertIn(name, whole, name)

    def test_a_container_is_never_counted_along_with_what_it_holds(self):
        # The duplication: a <blockquote> counted AND its <p>s. What holds
        # blocks is descended into before anything counts it as a leaf.
        walk = self.blocks.split("function walk(", 1)[1].split("function collect(", 1)[0]
        self.assertLess(walk.index("holdsBlocks(node)"), walk.index("COUNTED.has(name)"))

    def test_what_the_two_parsers_read_differently_is_not_counted(self):
        # DOMParser runs no scripts and the iframe does, so a <noscript>
        # holds markup in one and text in the other.
        self.assertIn("noscript", _set(self.blocks, "SKIPPED"))


class ThePageCarriesTheBooksStylesheetsOnly(unittest.TestCase):
    def test_stylesheets_and_no_scripts(self):
        shell = BLOCKS.read_text().split("function pageShell(", 1)[1]
        self.assertIn("\"style, link[rel~='stylesheet' i]\"", shell)
        self.assertNotIn("script", shell.split("window.readerBlocks", 1)[0])

    def test_no_event_handler_is_copied_onto_the_page(self):
        attributes = BLOCKS.read_text().split("function attributes(", 1)[1].split("function pageShell(", 1)[0]
        self.assertIn(".filter((attr) => !/^on/i.test(attr.name))", attributes)


class ANovelsTableSurvivesTheSanitizer(unittest.IsolatedAsyncioTestCase):
    """Through the real fetch, from a page served by a mock transport."""

    PAGE = """<html><body><div class="post">
    <p>Before.</p>
    <table class="stats" style="width:100%" onclick="alert(1)"><caption>Status</caption>
    <thead><tr><th colspan="2" onmouseover="x()">Kazuhisa</th></tr></thead>
    <tbody><tr><td>Level: 19</td><td rowspan="2">Support Magic: 5</td></tr>
    <tr><td>Skill Points: 2</td></tr></tbody></table>
    <script>alert(2)</script>
    </div></body></html>"""

    async def _fetch(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=self.PAGE))
        async with httpx.AsyncClient(transport=transport) as client:
            return await fetch_chapter_content("https://example.invalid/c1", client)

    async def test_the_table_keeps_its_shape(self):
        html = await self._fetch()
        for tag in ("<table>", "<caption>Status</caption>", "<thead>", "<tbody>",
                    '<th colspan="2">Kazuhisa</th>', '<td rowspan="2">'):
            self.assertIn(tag, html)

    async def test_it_is_still_sanitized(self):
        html = await self._fetch()
        for banned in ("onclick", "onmouseover", "style=", "class=", "<script", "alert("):
            self.assertNotIn(banned, html)


if __name__ == "__main__":
    unittest.main()
