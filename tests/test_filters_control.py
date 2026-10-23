"""The filters panel, on the three pages that have one.

Two selects and an eye are 317px of a 390px content row, so the home
header wrapped at every phone width and the search box -- the control used
most -- was left with whatever was over. The controls moved into a popover
opened by one icon, and the series and web-novel pages now carry the same
panel with the one control each of them filters by.

What these tests are about is what the move COSTS. The controls are no
longer on screen, so the ICON is the only thing saying the list is
narrowed, and that answer is computed twice on every page: once in the
page's pre-paint block, once in the script that owns the controls
afterwards. AGENTS.md calls that pair out by name -- a rule added to one
copy and not the other is visible only as the icon flipping colour one
frame after the page paints.
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
PANEL = ROOT / "app/templates/_filters_panel.html"
PANEL_JS = ROOT / "app/static/js/filters-panel.js"
HOME_HTML = ROOT / "app/templates/library/index.html"
HOME_CONTROLS = ROOT / "app/templates/library/_filters_control.html"
HOME_JS = ROOT / "app/static/js/home.js"
SERIES_HTML = ROOT / "app/templates/library/series.html"
SERIES_JS = ROOT / "app/static/js/series-search.js"
NOVEL_HTML = ROOT / "app/templates/novels/detail.html"
NOVEL_JS = ROOT / "app/static/js/novel-chapters.js"
THEME_CSS = ROOT / "app/static/src/theme.css"

LIT = "text-indigo-400"
IDLE = "text-neutral-500"


def _request():
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
        "headers": [], "client": ("test", 1), "server": ("test", 80),
    })


class OneShellForThreePages(unittest.TestCase):
    """The button and the popover are a macro, not three copies.

    The geometry is the hard part -- spanning the header's filters row on
    a phone, anchored to its own button from md up -- and it is solved
    once. Three hand-written copies is precisely how one page ends up with
    a popover that runs off the screen.
    """

    def test_every_page_calls_the_macro_rather_than_writing_the_button(self):
        for page in (HOME_CONTROLS, SERIES_HTML, NOVEL_HTML):
            with self.subTest(page=page.name):
                text = page.read_text()
                self.assertIn("filters.panel()", text)
                self.assertNotIn('id="filters-btn"', text)

    def test_the_two_colours_are_named_in_one_runtime_place(self):
        # Each page decides WHEN the icon is lit; none of them decides
        # what "lit" looks like, or a fourth page would invent a third
        # colour.
        js = PANEL_JS.read_text()
        self.assertIn(LIT, js)
        self.assertIn(IDLE, js)
        for page_js in (HOME_JS, SERIES_JS, NOVEL_JS):
            with self.subTest(script=page_js.name):
                self.assertNotIn(LIT, page_js.read_text())
                self.assertIn("filtersPanel?.setActive", page_js.read_text())

    def test_the_button_ships_idle_and_says_what_it_opens(self):
        # Shipping it lit would light it for a page with nothing filtered
        # until a script said otherwise.
        button = PANEL.read_text().split('id="filters-btn"', 1)[1].split(">", 1)[0]
        self.assertIn(IDLE, button)
        self.assertNotIn(LIT, button)
        self.assertIn('aria-haspopup="true"', button)
        self.assertIn('aria-expanded="false"', button)
        self.assertIn('aria-label="Filters"', button)


class BothHalvesOfEveryIndicator(unittest.TestCase):
    """One page, two copies of "is anything filtering", and they must agree.

    The rules differ BETWEEN pages on purpose: on the home page a search
    sets the filters aside, so a query puts the icon out; on the other two
    the search box sits in the row in plain sight and only the panel's own
    control is hidden, so the query is not the icon's business.
    """

    def test_the_home_pages_two_copies_light_it_for_the_same_four_controls(self):
        clause = '(status !== "all" || publication !== "all" || showHidden || unreadOnly)'
        for source in (HOME_HTML.read_text(), HOME_JS.read_text()):
            self.assertIn(clause, source)

    def test_the_home_pages_two_copies_put_it_out_during_a_search(self):
        clause = '(status !== "all" || publication !== "all" || showHidden || unreadOnly)'
        for source in (HOME_HTML.read_text(), HOME_JS.read_text()):
            before = source.split(clause)[0]
            self.assertRegex(before[-60:], r"!query\s*\n?\s*&&\s*$")

    def test_the_series_pages_two_copies_light_it_for_unread_only(self):
        # And for nothing else: its search is in the row, not the panel.
        self.assertIn("setActive(wantUnread)", SERIES_JS.read_text())
        prepaint = SERIES_HTML.read_text()
        self.assertIn(
            'filtersBtn.classList.add(wantUnread ? "text-indigo-400" : "text-neutral-500");',
            prepaint)

    def test_the_novel_pages_two_copies_light_it_for_unread_only(self):
        self.assertIn("setActive(wantUnread)", NOVEL_JS.read_text())
        # Its foot-of-page block only runs its body when the filter was on,
        # so there is nothing to branch on -- but it must still be there.
        #
        # Anchored on the gate rather than on the storage key: the key used
        # to be read here out of localStorage and is now a cookie the
        # SERVER reads, so the block asks the page what the server decided
        # (see ui_prefs.py) -- off <main>, which is the half a body swap
        # replaces. The anchor moved twice; what it pins did not.
        gate = 'const wanted = main.dataset.unreadOnly === "1";'
        source = NOVEL_HTML.read_text()
        self.assertIn(gate, source)
        block = source.split(gate, 1)[1]
        self.assertIn('filtersBtn.classList.add("text-indigo-400")', block)


class TheHomePanel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def _html(self):
        response = await home(_request(), {"id": 1}, self.db)
        return response.template.render(response.context)

    async def test_the_controls_are_in_the_page_not_rendered_on_open(self):
        # This is what lets applyFilters() and the pre-paint block go on
        # finding them by id with no idea a popover exists. Rendering them
        # when the panel opens would mean the saved state had nothing to
        # be restored into on the first frame.
        html = await self._html()
        for element_id in ("status-filter", "publication-filter", "show-hidden-filter"):
            self.assertIn(f'id="{element_id}"', html)

    async def test_they_are_inside_the_panel_and_the_panel_starts_shut(self):
        html = await self._html()
        panel = html.split('id="filters-popover"', 1)[1].split(">", 1)[0]
        self.assertIn("hidden", panel)
        after_button = html.split('id="filters-btn"', 1)[1]
        opening = after_button.index('id="filters-popover"')
        for element_id in ("status-filter", "publication-filter", "show-hidden-filter"):
            self.assertGreater(after_button.index(f'id="{element_id}"'), opening, element_id)


class TheRowsLineUp(unittest.TestCase):
    """The names are what decide where the controls start.

    Left to their own widths "Your status" and "Publication" differ, so
    the two selects ended at the same right edge and began at different
    left ones -- which is what reads as crooked.
    """

    def test_the_name_column_has_one_width_for_every_row(self):
        css = THEME_CSS.read_text()
        rule = css.split("#filters-popover > label:has(select) > span:first-child {", 1)[1].split("}", 1)[0]
        self.assertIn("width:", rule)
        self.assertIn("flex: none", rule)

    def test_the_controls_take_the_rest_so_the_selects_match(self):
        css = THEME_CSS.read_text()
        rule = css.split("#filters-popover select {", 1)[1].split("}", 1)[0]
        self.assertIn("flex: 1 1 0", rule)

    def test_a_row_with_no_select_puts_its_control_on_the_right_edge(self):
        # The series and web-novel panels hold one checkbox and nothing
        # else; without this it sits after the name column with the rest
        # of the panel empty beside it.
        css = THEME_CSS.read_text()
        rule = css.split("#filters-popover > label:not(:has(select)) {", 1)[1].split("}", 1)[0]
        self.assertIn("justify-content: space-between", rule)

    def test_every_row_has_the_shape_that_rule_targets(self):
        # `label > span:first-child` only reaches a name written as the
        # label's own first child; a row that nested it, or dropped the
        # span, would silently opt out of the alignment.
        for page in (HOME_CONTROLS, SERIES_HTML, NOVEL_HTML):
            with self.subTest(page=page.name):
                body = page.read_text().split("{% call filters.panel() %}", 1)[1]
                body = body.split("{% endcall %}", 1)[0]
                rows = re.findall(r"<label\b[^>]*>\s*(?:\{#.*?#\}\s*)?<span", body, re.S)
                self.assertTrue(rows, "no rows found")
                self.assertEqual(len(rows), body.count("<label"))


if __name__ == "__main__":
    unittest.main()
