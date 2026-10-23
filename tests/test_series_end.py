"""Finishing a series has to SAY so.

A page turn past the last page of the last volume used to do nothing at
all. `advanceToNextChapter()` returned at its first line and the page did
not move, which from the reader's side is indistinguishable from the app
being stuck -- and in Continuous scroll the strip simply stopped, with the
"load the next chapter" step returning in silence.

Where the answer goes differs by mode, and that is the design rather than
an inconsistency: in the paged modes the page TURN is the question, so the
answer covers the page the turn was asked from and a tap takes it away; in
the two continuous modes there is no such gesture, so it sits at the foot
of what was scrolled.

Driven in both browsers on the last volume of a series: hidden on arrival,
shown after turning past the end, gone again on a tap -- and, on a volume
that HAS a next one, never shown, the reader advancing as before. In
Continuous scroll, moved into `#page-scroll` as its last child.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMIC_HTML = ROOT / "app/templates/reader/comic.html"
EPUB_HTML = ROOT / "app/templates/reader/epub.html"
COMIC_JS = ROOT / "app/static/js/reader-comic.js"
TEXT_JS = ROOT / "app/static/js/reader-text.js"


def markup_of(path: Path) -> str:
    """The template with its comments blanked out.

    These are scans for MARKUP, and the comments in these templates
    explain each rule by naming what it forbids -- the note saying why
    `#reader-root` is `relative` contains the word "relative", and a scan
    of the raw file passed on that prose after the class had been removed.
    Same treatment, same reason, as tests/test_reader_shell.py.
    """
    source = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)
    return re.sub(r"<!--.*?-->", "", source, flags=re.S)


def code_of(path: Path) -> str:
    """The script with whole-line `//` comments blanked out.

    Every scan here is for CODE, and the comments below explain each rule
    by naming what it replaced. Same treatment, same reason, as
    tests/test_events.py.
    """
    source = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in source.splitlines())


class TheComicReaderSaysIt(unittest.TestCase):
    def setUp(self):
        self.markup = markup_of(COMIC_HTML)
        self.js = code_of(COMIC_JS)

    def test_the_notice_is_rendered_by_the_template(self):
        # Not built in JS: what the reader can show should be readable in
        # the file that describes the reader.
        self.assertIn('id="series-end"', self.markup)
        block = self.markup.split('id="series-end"', 1)[1].split("</div>", 1)[0]
        self.assertIn("hidden", block.split(">", 1)[0], "the notice is on screen from the start")
        self.assertIn("End of series", block)
        self.assertIn("/series/", block, "there is no way back to the series from it")

    def test_a_turn_past_the_end_shows_it(self):
        # The line this replaced returned here and did nothing.
        advance = self.js.split("async function advanceToNextChapter", 1)[1].split("\n    }", 1)[0]
        self.assertIn("if (!nextItemId) {", advance, "no next item is still answered with silence")
        self.assertIn("showSeriesEndOverPage();", advance)

    def test_the_strip_gets_it_at_the_foot(self):
        load = self.js.split("async function maybeLoadNextChapter", 1)[1].split("\n    }", 1)[0]
        self.assertIn("placeSeriesEndBelow(activeScrollContainer);", load,
                      "scrolling to the end of the last chapter says nothing")

    def test_a_tap_takes_it_away_but_only_over_the_page(self):
        # Over the page it is a curtain, so it has to be dismissable or the
        # reader is trapped behind it. In the strip it is just content.
        self.assertIn("function hideSeriesEnd()", self.js)
        self.assertIn('if (seriesEnd.parentElement !== root) return;', self.js,
                      "a tap in the strip would hide content that belongs there")
        self.assertIn('if (event.target.closest("a")) return;', self.js,
                      "the way back is swallowed by the dismiss handler")

    def test_switching_modes_does_not_delete_it(self):
        # Scroll mode empties its container to rebuild, and the notice may
        # be living inside it by then.
        setup = self.js.split("async function setupScrollMode", 1)[1]
        self.assertLess(setup.index("reclaimSeriesEnd();"), setup.index('container.innerHTML = ""'),
                        "the container is emptied with the notice still in it")

    def test_the_classes_it_is_shown_with_exist(self):
        # The frozen app.css has no rule for a translucent black or for
        # p-6, and a class with no rule is silently nothing.
        over = self.js.split("const OVER_THE_PAGE = ", 1)[1].split("]", 1)[0]
        for name in ("absolute", "inset-0", "z-20", "bg-black", "flex"):
            self.assertIn(f'"{name}"', over)
        css = (ROOT / "app/static/src/app.css").read_text() + (ROOT / "app/static/src/theme.css").read_text()
        for name in ("absolute", "inset-0", "z-20", "bg-black", "flex"):
            self.assertRegex(css, r"\." + re.escape(name) + r"[\s,{:]",
                             f"{name} has no rule, so showing the notice with it does nothing")


class TheTextReaderSaysItToo(unittest.TestCase):
    def setUp(self):
        self.js = code_of(TEXT_JS)

    def test_the_end_of_book_block_is_placed_either_way(self):
        # It used to be offered only when there was something to offer, so
        # finishing a series looked like the reader failing to load.
        place = self.js.split("function maybePlaceEndOfBook", 1)[1].split("\n    }", 1)[0]
        self.assertNotIn("if (!nextItemId ||", place,
                         "the last volume still gets no end-of-book block")
        self.assertIn('nextItemId ? "End of book" : "End of series"', place,
                      "both ends are called the same thing")
        self.assertIn("nextItemId ? nextVolumeButton() : backToSeriesLink()", place)

    def test_a_paged_turn_at_the_very_end_answers(self):
        self.assertIn("if (nextItemId) advanceToNextItem();", self.js)
        self.assertIn("else showEndOfSeriesOverPage();", self.js)

    def test_the_way_back_comes_from_the_page(self):
        # The reader is inside its own theme, so the link is built here --
        # but WHERE it goes is the server's to say.
        self.assertIn('root.dataset.seriesUrl', self.js)
        self.assertIn("data-series-url=", markup_of(EPUB_HTML),
                      "the template never tells the reader which series this is")

    def test_the_overlay_has_something_to_be_positioned_against(self):
        # `position: absolute` with no positioned ancestor escapes to the
        # viewport, which happens to look right and stops doing so the
        # moment anything above it gains a position.
        markup = markup_of(EPUB_HTML)
        root_tag = markup.split('id="reader-root"', 1)[1].split(">", 1)[0]
        self.assertIn('class="relative', root_tag,
                      "#reader-root is not a containing block")
        self.assertIn("position: absolute", self.js)

    def test_the_stripper_is_not_what_makes_that_pass(self):
        # Negative control, both ways: the comment above that class says
        # the word "relative", and a scan of the raw file passed on the
        # prose alone once the class itself was gone.
        self.assertIn("`relative` so the end-of-series notice", EPUB_HTML.read_text())
        self.assertNotIn("`relative` so the end-of-series notice", markup_of(EPUB_HTML))

    def test_both_ends_are_styled_from_the_readers_own_colour(self):
        # A sepia page and a black one have to get a border that can be
        # seen; the app's palette is not in scope inside the reader.
        self.assertIn("const END_CONTROL", self.js)
        control = self.js.split("const END_CONTROL", 1)[1].split(";\n", 1)[0]
        self.assertIn("currentColor", control)
        self.assertIn("color: inherit", control)


if __name__ == "__main__":
    unittest.main()
