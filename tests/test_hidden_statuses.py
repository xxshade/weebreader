"""Seeing everything, including what /settings hides.

A status checked in /settings takes its series out of the home grid
entirely, and until now the only way to look at one of them again was to
go back to settings and uncheck it. The switch in the header suspends that
setting without changing it.

Two halves have to agree for it to work, and they are in different files:
the inline pre-paint block in library/index.html applies the saved state
before the first frame, and home.js owns it from then on. That duplication
is deliberate (see AGENTS.md) and is exactly the thing that goes stale, so
it is pinned here -- a disagreement between them is visible as cards
appearing on the first frame and then vanishing.
"""
import re
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import home

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "app/templates/library/index.html"
# The controls themselves moved into the filters popover; the pre-paint
# block that restores them stayed in the page. The pair being in two
# files now is precisely why the assertions below name both.
FILTERS = ROOT / "app/templates/library/_filters_control.html"
HOME_JS = ROOT / "app/static/js/home.js"
THEME_CSS = ROOT / "app/static/src/theme.css"
STORAGE_KEY = "library-show-hidden"


def _attributes(fragment: str) -> set:
    """The attribute NAMES in what followed an <option value="...">."""
    return set(re.findall(r'([a-z-]+)(?:="[^"]*")?', fragment)) - {""}


def _request():
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
        "headers": [], "client": ("test", 1), "server": ("test", 80),
    }
    return Request(scope)


class TheStatusFilterMarkup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        await self.db.execute(
            "INSERT INTO users (id, username, password_hash, hidden_statuses) "
            "VALUES (1, 'u', 'x', '[\"completed\"]')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def _options(self):
        response = await home(_request(), {"id": 1}, self.db)
        html = response.template.render(response.context)
        # The tracking filter is the first select on the page; the
        # publication one below it offers the same word "completed" for a
        # different vocabulary, so this stops at the closing tag.
        block = html.split('id="status-filter"', 1)[1].split("</select>", 1)[0]
        return dict(re.findall(r'<option value="([a-z_]+)"([^>]*)>', block))

    async def test_a_hidden_status_is_still_an_option(self):
        # It used to be left out of the markup altogether, which left the
        # switch nothing to bring back: with hidden statuses on screen,
        # filtering BY one of them is the obvious next thing to want.
        options = await self._options()
        self.assertIn("completed", options)

    async def test_it_is_marked_and_starts_hidden(self):
        # Marked server-side, because the browser is what un-hides it and
        # it must not flash on screen before that decision is made.
        #
        # Read as ATTRIBUTES rather than as a substring: "hidden" is inside
        # "data-hidden-status" too, so a plain `in` check passes on markup
        # that carries the marker and never hides anything (found by
        # breaking exactly that).
        options = await self._options()
        attributes = _attributes(options["completed"])
        self.assertIn("data-hidden-status", attributes)
        self.assertIn("hidden", attributes)

    async def test_every_other_status_is_offered_plainly(self):
        options = await self._options()
        for value, attributes in options.items():
            if value in ("all", "completed"):
                continue
            self.assertEqual(_attributes(attributes), set(), value)


class BothHalvesOfThePrePaintRule(unittest.TestCase):
    """The rule lives twice on purpose. What must never happen is one copy
    knowing about the switch and the other not."""

    def test_the_switch_is_read_before_paint_and_owned_afterwards(self):
        self.assertIn(STORAGE_KEY, INDEX.read_text())
        self.assertIn(STORAGE_KEY, HOME_JS.read_text())

    def test_both_copies_let_it_override_the_hidden_statuses(self):
        # The clause itself, not just the key: reading the switch and then
        # not applying it is the same bug with more steps.
        clause = "showHidden || !hiddenStatuses.has(cardStatus)"
        self.assertIn(clause, INDEX.read_text())
        self.assertIn(clause, HOME_JS.read_text())

    def test_both_copies_drop_a_selection_that_is_going_away(self):
        # A hidden status left selected while the switch goes off filters
        # every card away with nothing on screen saying why.
        self.assertIn("hiddenStatuses.has(status)", INDEX.read_text())
        self.assertIn("hiddenStatuses.has(statusFilter.value)", HOME_JS.read_text())


class TheEyeThatSaysWhichWayItIs(unittest.TestCase):
    """The switch is drawn as an eye, and the eye is decided in CSS.

    Two things about that are load-bearing. The CHECKBOX is still the
    state -- same id, same `.checked`, so the pre-paint block, home.js and
    every test above are untouched by how it looks. And which glyph shows
    is `:checked` in the stylesheet rather than a line of script: an icon
    corrected after the first paint is a visible flip, which is the whole
    reason the rest of this state is restored before paint.
    """

    def test_the_state_is_still_a_checkbox_with_the_name_both_scripts_use(self):
        markup = FILTERS.read_text()
        self.assertIn('<input autocomplete="off" type="checkbox" id="show-hidden-filter"', markup)
        self.assertIn('getElementById("show-hidden-filter")', HOME_JS.read_text())

    def test_it_draws_both_eyes_and_a_name_for_the_one_on_screen(self):
        markup = FILTERS.read_text()
        for cls in ("eye-open", "eye-closed", "eye-label"):
            self.assertIn(f'class="{cls}"', markup)

    def test_checked_is_what_swaps_them_and_no_script_is_involved(self):
        css = THEME_CSS.read_text()
        # Closed by default, open only while the box is ticked -- stated
        # in that order, since the default is what the first frame draws.
        self.assertIn(".eye-toggle .eye-open {\n    display: none;\n}", css)
        self.assertIn(".eye-toggle .eye-input:checked ~ .eye-open {\n    display: block;\n}", css)
        self.assertIn(".eye-toggle .eye-input:checked ~ .eye-closed {\n    display: none;\n}", css)
        # And nothing in the page's own script decides it, or the two
        # would be free to disagree about which eye is showing.
        self.assertNotIn("eye-open", HOME_JS.read_text())

    def test_the_hidden_input_stays_reachable_by_keyboard(self):
        # display:none would take the switch away from the keyboard
        # entirely; it is moved out of sight instead, and the label is
        # what shows the focus ring, since the input itself cannot.
        css = THEME_CSS.read_text()
        rule = css.split(".eye-toggle .eye-input {")[1].split("}")[0]
        self.assertNotIn("display: none", rule)
        self.assertIn(".eye-toggle:has(.eye-input:focus-visible)", css)


if __name__ == "__main__":
    unittest.main()
