"""A long description is cut to three lines, with the rest behind a button.

What a catalogue answers with is often several paragraphs, and that sits
between the series title and everything you came to the page to do.

Two halves, and which half does what is the point: the CLAMP is CSS, so
the short version is what the first frame draws; the BUTTON is JavaScript,
because whether three lines were enough depends on the width the text is
laid out at, and CSS cannot ask that question.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACRO = ROOT / "app/templates/library/_series_headline.html"
THEME = ROOT / "app/static/src/theme.css"
JS = ROOT / "app/static/js/read-more.js"
SERIES = ROOT / "app/templates/library/series.html"


def code_of(path: Path) -> str:
    """The script with whole-line `//` comments blanked out.

    The scans below are looking for CODE. Leaving the prose in means a
    comment explaining a rule reads as a breach of it -- which is exactly
    what happened here: the note saying why this file does NOT listen for
    htmx:after:init is a line containing "htmx:after:init".
    """
    return "\n".join("" if line.lstrip().startswith("//") else line
                      for line in path.read_text().splitlines())


class TheMarkupTheMacroRenders(unittest.TestCase):
    def setUp(self):
        self.markup = MACRO.read_text()

    def test_the_text_is_clamped_by_the_stylesheet(self):
        # Not by a script: a wall of text shown and then taken away is a
        # visible flash, the same reason every filter here is applied
        # before paint.
        self.assertIn('class="clamp-text clamp-3', self.markup)

    def test_the_button_comes_from_the_shared_macro(self):
        # Two places render this pair (a series' description and a novel's
        # synopsis) and the threshold below has to be the same number in
        # both. One macro is what makes that true rather than hoped.
        self.assertIn("clamp.toggle(series.summary)", self.markup)
        self.assertIn('{% import "_clamp.html" as clamp %}', self.markup)
        novel = (ROOT / "app/templates/novels/_novel_headline.html").read_text()
        self.assertIn("clamp.toggle(novel.synopsis)", novel)
        self.assertIn('{% import "_clamp.html" as clamp %}', novel)

    def test_the_swapped_element_holds_both_halves(self):
        # #series-summary is what the edit panel and a tracker link
        # replace out of band. With the id on the paragraph alone, an
        # expanded description would come back clamped with its button
        # still reading "Show less".
        wrapper = self.markup.split('id="series-summary"', 1)[1].split(">", 1)[0]
        self.assertIn("data-clamp", wrapper)
        after = self.markup.split('id="series-summary"', 1)[1]
        self.assertLess(after.index("clamp-text"), after.index("</div>"))
        self.assertLess(after.index("clamp.toggle"), after.index("</div>"))

    def test_the_page_loads_the_script(self):
        self.assertIn("js/read-more.js", SERIES.read_text())


class TheServerMakesRoomSoNothingMoves(unittest.TestCase):
    """The button used to arrive after the first paint and push the page.

    Whether it is NEEDED depends on how the text wraps at this width, so
    only the browser can answer -- but the server can stop that answer
    moving anything. It renders the button already in place whenever the
    text is long enough to possibly need one, and read-more.js then hides
    an unneeded one with `visibility` rather than `display`, which keeps
    the slot.

    Measured in Chromium, which reports layout shifts: **CLS 0.0062 at
    1200px and 0.0159 at 390px** on a long description, 0 where no button
    appeared -- against **0 everywhere** after, in both browsers. The
    states, at both widths and in both browsers: a long description
    reserved and visible; a medium one reserved, visible at 390px and
    `visibility: hidden` at 1200px; a short one `display: none` with
    nothing reserved. Clicking still expands it ("Show less", unclamped).

    The threshold is the narrowest column the layout has. Three lines hold
    120 characters at 288px, 124 at 320px, 145 at 358px (a 390px phone)
    and 270 at 672px (`max-w-2xl`) -- binary search, both browsers, which
    agreed exactly.
    """

    def render(self, text):
        import jinja2
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(ROOT / "app/templates")),
            autoescape=True)
        macro = env.get_template("_clamp.html").module.toggle
        return str(macro(text))

    def test_text_no_width_can_clamp_reserves_nothing(self):
        # A one-line description must not carry a blank row under it.
        html = self.render("Curta.")
        self.assertIn("hidden", html, "a short description reserves a slot it cannot use")
        self.assertNotIn("data-reserved", html)

    def test_text_that_might_clamp_is_rendered_in_place(self):
        # THE case: at 390px this needs the button, and it is there in the
        # first byte rather than arriving after the paint.
        html = self.render("x" * 200)
        self.assertIn("data-reserved", html, "nothing reserves the slot")
        self.assertNotIn("hidden", html, "the reserved button is still hidden on arrival")

    def test_the_threshold_is_the_narrowest_column(self):
        # 120 is what three lines hold at 288px. Anything larger would
        # leave the narrow case shifting, which is the case that matters.
        import jinja2
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(ROOT / "app/templates")), autoescape=True)
        self.assertEqual(env.get_template("_clamp.html").module.POSSIBLY_CLAMPED, 120)
        self.assertNotIn("data-reserved", self.render("x" * 120))
        self.assertIn("data-reserved", self.render("x" * 121))

    def test_no_text_at_all_reserves_nothing(self):
        for empty in (None, ""):
            self.assertNotIn("data-reserved", self.render(empty), repr(empty))

    def test_the_script_keeps_the_slot_it_was_given(self):
        js = code_of(JS)
        self.assertIn('toggle.hasAttribute("data-reserved")', js,
                      "the script cannot tell a reserved button from a bare one")
        self.assertIn('toggle.classList.toggle("clamp-toggle-unneeded", fits)', js,
                      "an unneeded reserved button still collapses its slot")
        self.assertIn('toggle.classList.toggle("hidden", fits)', js,
                      "an unreserved button can no longer appear at all")

    def test_the_rule_that_keeps_it_is_visibility_not_display(self):
        css = THEME.read_text()
        rule = css.split(".clamp-toggle[data-reserved].clamp-toggle-unneeded {", 1)[1].split("}", 1)[0]
        self.assertIn("visibility: hidden", rule)
        self.assertNotIn("display", rule, "display would give the slot back and move the page")

    def test_the_rule_only_applies_to_a_reserved_button(self):
        # Control: without the attribute in the selector it would also
        # catch the short case, which is meant to collapse entirely.
        self.assertIn(".clamp-toggle[data-reserved].clamp-toggle-unneeded", THEME.read_text())


class TheRuleThatCuts(unittest.TestCase):
    def setUp(self):
        self.css = THEME.read_text()
        self.rule = self.css.split(".clamp-3 {", 1)[1].split("}", 1)[0]

    def test_it_clamps_by_lines_and_not_by_height(self):
        # A max-height in em guesses the line-height and cuts a fourth
        # line in half, which reads as a rendering fault rather than as a
        # deliberate crop.
        self.assertIn("-webkit-line-clamp: 3;", self.rule)
        self.assertNotIn("max-height", self.rule)

    def test_no_unprefixed_line_clamp_for_the_console_to_drop(self):
        # No engine parses it (CSS.supports false in Chrome 141 and
        # Firefox 155), so it clamped nothing and Firefox logged it as an
        # unknown property on every page load.
        self.assertNotRegex(self.rule, r"(?<!-webkit-)line-clamp:")

    def test_it_keeps_the_parts_that_make_the_clamp_work(self):
        # -webkit-box and box-orient are the mechanism, not decoration:
        # without either, the rule silently does nothing.
        self.assertIn("display: -webkit-box;", self.rule)
        self.assertIn("-webkit-box-orient: vertical;", self.rule)
        self.assertIn("overflow: hidden;", self.rule)


class WhatTheScriptIsFor(unittest.TestCase):
    def setUp(self):
        self.js = code_of(JS)

    def test_it_offers_the_button_only_when_the_text_overflows(self):
        self.assertIn("scrollHeight - text.clientHeight", self.js)

    def test_it_does_not_measure_expanded_text(self):
        # Expanded, the two heights are equal by definition -- measuring
        # then would hide the button mid-read.
        self.assertIn("if (!text.classList.contains(CLAMP)) return;", self.js)

    def test_the_click_survives_the_element_being_replaced(self):
        # The description is swapped out of band by the edit panel and by
        # a tracker link. A bound listener would go with the old node.
        self.assertIn('event.target.closest(".clamp-toggle")', self.js)
        self.assertNotIn("getElementById", self.js)

    def test_it_re_measures_when_htmx_settles_and_on_resize(self):
        # A swapped-in copy arrives with the button hidden, and a
        # narrower window fits fewer words per line.
        self.assertIn('"htmx:after:settle"', self.js)
        self.assertIn('"resize"', self.js)

    def test_the_process_sweep_is_per_root_and_not_per_element(self):
        # htmx:after:process fires once for each subtree htmx processes;
        # htmx:after:init -- which is what the upgrade guide's table maps
        # htmx:load to -- fires once per ELEMENT initialised inside it, so
        # one arrival carrying eight buttons would sweep the document
        # eight times.
        self.assertIn('"htmx:after:process"', self.js)
        self.assertNotIn("htmx:after:init", self.js)

    def test_the_scan_is_looking_at_code(self):
        # The guard above is a source scan, and the comment right beside
        # the line it guards NAMES the event it forbids. So this checks
        # both directions against the file as it is on disk: the name IS
        # in it, and is NOT in what the scans read. A blanking rule that
        # quietly stopped working would otherwise leave every assertion in
        # this class green and guarding nothing.
        raw = JS.read_text()
        self.assertIn("htmx:after:init", raw)
        self.assertNotIn("htmx:after:init", code_of(JS))

    def test_no_htmx_2_event_names_are_left(self):
        # camelCase is the htmx 2 spelling; htmx 4 separates with colons,
        # and a listener for a name nothing fires is silently dead.
        for gone in ("htmx:afterSettle", "htmx:load", "htmx:afterSwap",
                     "htmx:afterRequest"):
            self.assertNotIn(gone, self.js)

    def test_the_htmx_sweep_is_not_scoped_to_the_swap_target(self):
        # An out-of-band swap lands OUTSIDE what htmx was pointed at,
        # which is this element's whole situation -- scoping the sweep to
        # event.target would skip exactly the case it exists for.
        for line in self.js.splitlines():
            if "htmx:" in line:
                self.assertNotIn("measureAll(event.target)", line)

    def test_the_button_says_which_way_it_goes(self):
        self.assertIn('"Show less" : "Show more"', self.js)
        self.assertIn('toggle.setAttribute("aria-expanded"', self.js)


if __name__ == "__main__":
    unittest.main()
