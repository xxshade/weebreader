"""Reaching the bottom of a comic finishes it, however short the last page.

Continuous scroll names the current page by which one crosses the TOP of
the viewport. A final page SHORTER than the screen -- the credits or
afterword page of a great many volumes -- can never satisfy that: scrolled
as far as it goes, it sits at the BOTTOM and the top of the screen is
still inside the page before it. So the counter stopped one short and the
volume was never marked read.

Measured in a browser at 390x844, on a CBZ of four 585px pages and a 341px
credits page (58% of a normal one): at fromBottom = 0 the indicator read
"4 / 5" and the row saved was `reading`, 80%. After the fix, "5 / 5" and
`completed`, 100%.

An earlier round fixed half of this -- markChapterFinished, on the same
bottom test -- but left the measurement wrong, so the next scroll event or
the pagehide beacon posted `reading` back over it from the page the top
rule still named. Both halves are pinned here, and the browser controls
that separate them: with only the bottom-mark, the indicator still read
"3 / 4" at the end; with only the measurement, scrolling back up a notch
dropped the volume to `reading`, 25%.

None of this is visible from Python -- it is browser layout -- so what is
pinned is that the two rules still exist and still read the same geometry.
"""
import re
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "app/static/js/reader-comic.js"


def body_of(source, name):
    start = re.search(r"^    (?:async )?function %s\(" % re.escape(name), source, re.M)
    assert start, f"no function {name} in reader-comic.js"
    rest = source[start.end():]
    end = re.search(r"^    (?:async )?function ", rest, re.M)
    return rest[: end.start()] if end else rest


class TheEndOfTheScroll(unittest.TestCase):
    def setUp(self):
        self.source = ENGINE.read_text()

    def test_the_last_page_is_current_once_there_is_nowhere_left_to_scroll(self):
        body = body_of(self.source, "measureCurrentScrollPage")
        self.assertIn("container.scrollHeight - container.scrollTop - container.clientHeight", body)
        self.assertIn("children[container.children.length - 1]", body)

    def test_the_end_is_tested_before_the_top_of_the_viewport(self):
        # Order is the whole point: the top rule always returns something,
        # so asking it first means never reaching the end rule.
        body = body_of(self.source, "measureCurrentScrollPage")
        self.assertLess(body.index("container.clientHeight"), body.index("referencePoint"))

    def test_both_ends_of_the_fix_measure_the_same_geometry(self):
        # markChapterFinished fires off the same "am I at the bottom"
        # question in onScroll. Two different slacks would mean the chapter
        # could be marked finished from a position the measurement does not
        # call the last page, or the other way round.
        self.assertEqual(self.source.count("BOTTOM_SLACK_PX"), 3)  # the const, and both readers of it
        self.assertIn("< BOTTOM_SLACK_PX", body_of(self.source, "measureCurrentScrollPage"))

    def test_having_reached_the_end_outranks_the_page_on_screen(self):
        # Otherwise scrolling back up a notch on a short final page -- or
        # just leaving the reader, which posts from wherever the top rule
        # points -- writes `reading` over a volume that was read through.
        body = body_of(self.source, "postProgress")
        self.assertIn("finishedChapters.has(targetItemId)", body)
        self.assertIn('status: reachedEnd ? "completed" : "reading"', body)
        # The percentage is computed off the same flag, in a name of its
        # own because reader-resume.js is told it too -- see postProgress.
        self.assertIn("const progressPercent = reachedEnd", body)
        self.assertIn("progress_percent: progressPercent", body)

    def test_the_finished_set_is_per_session_and_not_persisted(self):
        # In memory on purpose: reopening the book and starting over has to
        # be able to lower it again.
        self.assertIn("const finishedChapters = new Set();", self.source)
        self.assertNotIn("finishedChapters", body_of(self.source, "loadPrefs"))


if __name__ == "__main__":
    unittest.main()
