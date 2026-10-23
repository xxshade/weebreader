"""The home page's library tabs, which replaced its sidebar.

The sidebar listed "All libraries", each library and "Web Novels" down the
left of the grid, at 13rem (3.25rem collapsed), and the header's logo
column was sized to match it so the search box lined up with the grid.
The same buttons are tabs over the "Libraries" grid now: an icon each, the
name in the tooltip. What these pin:

* the tabs are what the sidebar was -- the same data-library-filter values,
  so a selection saved in localStorage and a ?library= link still land;
* the selected tab is `aria-pressed`, set by BOTH the pre-paint block and
  home.js (a rule in one copy only shows as the underline jumping one
  frame after paint), and that is also what the CSS draws;
* Rescan and New library survive the move -- the sidebar was the only
  place either lived;
* nothing sized from a sidebar is left, which is what gives the search box
  the series page's width;
* the header shows the app's own icon, not an emoji.

Measured in both browsers against the real build: the tabs filter the grid
and survive a reload, Rescan shows only on a library's own tab, the dialog
opens from "+", and the search box starts where the series page's does.
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
HOME_HTML = ROOT / "app/templates/library/index.html"
HOME_JS = ROOT / "app/static/js/home.js"
BASE_HTML = ROOT / "app/templates/base.html"
THEME_CSS = ROOT / "app/static/src/theme.css"


def _request():
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
        "headers": [], "client": ("test", 1), "server": ("test", 80),
    })


def _tag(html, pattern):
    """Every opening tag matching `pattern` -- attributes and all, so a
    test can read one tag's attributes without matching its neighbour's."""
    return re.findall(rf"<[a-z]+\b[^>]*{pattern}[^>]*>", html)


class TheTagScanWorks(unittest.TestCase):
    def test_it_reads_one_tag_and_not_the_next(self):
        sample = '<button data-x="1" title="A"></button><button data-x="2" title="B">'
        self.assertEqual(_tag(sample, 'data-x="2"'), ['<button data-x="2" title="B">'])

    def test_it_finds_nothing_when_nothing_is_there(self):
        self.assertEqual(_tag("<p></p>", 'data-x="1"'), [])


class HomePage(unittest.IsolatedAsyncioTestCase):
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
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES"
            " (1, 'Manga', '/m', 'comic'), (2, 'Comics', '/c', 'comic')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def _html(self):
        response = await home(_request(), {"id": 1}, self.db)
        return response.template.render(response.context)


class TheTabs(HomePage):
    async def test_all_then_each_library_then_web_novels(self):
        # Libraries in name order, the order the sidebar listed them in.
        html = await self._html()
        tabs = _tag(html, r'class="library-tab"')
        self.assertEqual(
            [re.search(r'data-library-filter="([^"]+)"', t).group(1) for t in tabs],
            ["all", "2", "1", "novels"])
        self.assertEqual(
            [re.search(r'title="([^"]+)"', t).group(1) for t in tabs],
            ["All", "Comics", "Manga", "Web Novels"])

    async def test_each_is_an_icon_with_its_name_as_the_accessible_one(self):
        # No visible label: the title is a tooltip, and a screen reader
        # needs the name from somewhere that is not a hover.
        html = await self._html()
        for tab in _tag(html, r'class="library-tab"'):
            with self.subTest(tab=tab):
                self.assertEqual(re.search(r'title="([^"]+)"', tab).group(1),
                                 re.search(r'aria-label="([^"]+)"', tab).group(1))
                self.assertIn('aria-pressed="false"', tab)
        nav = re.search(r'<nav id="library-filter-nav".*?</nav>', html, re.S).group(0)
        self.assertNotIn("<span", nav)
        self.assertEqual(nav.count("<svg"), 4)

    async def test_they_sit_in_the_libraries_row_over_the_grid(self):
        html = await self._html()
        self.assertLess(html.index(">Libraries</h2>"), html.index('id="library-filter-nav"'))
        self.assertLess(html.index('id="library-filter-nav"'), html.index('id="series-grid"'))
        self.assertLess(html.index('id="reading-section"'), html.index('id="library-filter-nav"'))

    async def test_the_sidebar_is_gone(self):
        html = await self._html()
        for trace in ("library-sidebar", "sidebar-toggle", "sidebar-collapsed", "sidebar-label"):
            with self.subTest(trace=trace):
                self.assertNotIn(trace, html)


class WhatTheSidebarAlsoHeld(HomePage):
    async def test_one_rescan_per_library_all_hidden_until_its_tab_is_chosen(self):
        html = await self._html()
        forms = _tag(html, r"data-rescan-for=")
        self.assertEqual(len(forms), 2)
        for library_id, form in zip(("2", "1"), forms):
            with self.subTest(library=library_id):
                self.assertIn(f'action="/libraries/{library_id}/scan"', form)
                self.assertIn(f'data-rescan-for="{library_id}"', form)
                self.assertRegex(form, r"\shidden(\s|>)")
        self.assertIn('aria-label="Rescan Manga"', html)

    async def test_new_library_opens_the_dialog(self):
        html = await self._html()
        (opener,) = _tag(html, r'data-dialog-open="new-library-dialog"')
        self.assertIn('aria-label="New library"', opener)
        self.assertIn('<dialog id="new-library-dialog"', html)


class BothCopiesPressTheSameTab(unittest.TestCase):
    """The pre-paint block and home.js each decide which tab is pressed and
    which Rescan shows. Either one written from the old class lists would
    leave the underline or the button right for one frame and wrong after,
    or the other way round."""

    def _blocks(self):
        home = HOME_HTML.read_text()
        start = home.index("// Keep identical to setLibraryButtonStates() in home.js.")
        prepaint = home[start:home.index("} catch", start)]
        js = HOME_JS.read_text()
        start = js.index("function setLibraryButtonStates()")
        return {"pre-paint": prepaint, "home.js": js[start:js.index("\n    }\n", start)]}

    def test_both_set_aria_pressed_and_the_rescan_forms(self):
        for name, block in self._blocks().items():
            with self.subTest(copy=name):
                self.assertIn('setAttribute("aria-pressed"', block)
                self.assertIn("[data-rescan-for]", block)
                self.assertIn(".hidden = ", block)

    def test_neither_still_paints_with_the_sidebar_classes(self):
        for name, block in self._blocks().items():
            with self.subTest(copy=name):
                self.assertNotIn("bg-neutral-800", block)
                self.assertNotIn("classList.add", block)

    def test_the_css_draws_the_pressed_one(self):
        self.assertRegex(THEME_CSS.read_text(),
                         r'\.library-tab\[aria-pressed="true"\]\s*\{[^}]*border-bottom-color')


class TheHeader(unittest.TestCase):
    def test_nothing_is_sized_from_a_sidebar_any_more(self):
        # The logo column was as wide as the sidebar, which is what put the
        # home page's search box 13rem further in than the series page's.
        css = THEME_CSS.read_text()
        for trace in ("#library-sidebar", "--sidebar-w", ".header-brand-col"):
            with self.subTest(trace=trace):
                self.assertNotIn(trace, css)
        self.assertNotIn("header-brand-col", BASE_HTML.read_text())

    def test_the_logo_is_the_apps_own_icon(self):
        base = BASE_HTML.read_text()
        self.assertNotIn("📚", base)
        (logo,) = re.findall(r'<a href="/"[^>]*>\s*<img[^>]*>', base)
        self.assertIn("asset_url('favicon.svg')", logo)
        self.assertIn('aria-label="Library"', logo)


if __name__ == "__main__":
    unittest.main()
