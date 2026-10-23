"""A series' sort and Reorder controls, and a novel's Reorder, live behind an
icon.

They took the right of the title row on every visit -- a line of their own
on a phone -- for something done now and then. They are now in a panel
under the title, opened by an icon among the others that act on the series
(edit, trackers, mark all read/unread). What the panel holds is decided by
the rules that decided the row: the sort control not while dragging nor
for a single item, the select only with more than one order, Reorder
always, Reset only once there is a manual order (tests/test_series_sort.py
has the select's options).

The panel starts closed, except while dragging: "Done" is how that mode is
left, and its label is how you can tell you are in it. A novel's panel holds
Reorder and Reset under the same rules.

With its controls out of sight, the series icon is what says the grid is in
an order of its own: it is lit, like the filters icon, for any order but the
default, and the pre-paint block and series-sort.js decide that alike.
"""
import unittest
from html.parser import HTMLParser

from app.novels.router import novel_detail
from tests.test_novel_page import NovelPageCase, _request
from tests.test_series_sort import ROOT, SERIES_HTML, SORT_JS, _Case

DISCLOSURE_JS = ROOT / "app/static/js/disclosure.js"
NOVEL_HTML = ROOT / "app/templates/novels/detail.html"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}


class _Element(HTMLParser):
    """Every start tag inside the element with this id (its own first), and
    the text in it -- or every start tag on the page, with no id."""

    def __init__(self, html, element_id=None):
        super().__init__()
        self.element_id = element_id
        self.depth = 0
        self.tags = []
        self.words = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.element_id and not self.depth and attrs.get("id") != self.element_id:
            return
        self.tags.append((tag, attrs))
        if tag not in VOID:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.depth or not self.element_id:
            self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if self.depth and tag not in VOID:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.words.extend(data.split())

    @property
    def attrs(self):
        return self.tags[0][1]

    @property
    def ids(self):
        return [a["id"] for _, a in self.tags if "id" in a]

    @property
    def forms(self):
        return [a.get("action") for t, a in self.tags if t == "form"]


class TheIcon(_Case):
    async def test_it_opens_the_panel_and_says_so(self):
        await self._series(1, "epub")
        icon = _Element(await self._html(1), "series-order-toggle")
        self.assertEqual(icon.tags[0][0], "button")
        self.assertEqual(icon.attrs["type"], "button")
        self.assertIn("data-disclosure", icon.attrs)
        self.assertEqual(icon.attrs["aria-controls"], "series-order-panel")
        self.assertEqual(icon.attrs["aria-expanded"], "false")

    async def test_it_is_an_icon_that_still_says_its_name(self):
        # An icon with no accessible name is the silent half of turning
        # labelled buttons into a glyph.
        await self._series(1, "epub")
        icon = _Element(await self._html(1), "series-order-toggle")
        self.assertEqual(icon.attrs["aria-label"], "Sort and reorder")
        self.assertEqual(icon.attrs["title"], "Sort and reorder")
        self.assertIn("svg", [t for t, _ in icon.tags])
        self.assertEqual(icon.words, [])

    def test_it_sits_with_the_other_icons_beside_the_title(self):
        text = SERIES_HTML.read_text()
        icon = text.index('id="series-order-toggle"')
        self.assertLess(text.index('title="Mark all unread"'), icon)
        self.assertLess(icon, text.index("badges.tracking_select("))
        self.assertLess(icon, text.index('<div id="series-order-panel"'))

    def test_the_page_loads_what_makes_it_work(self):
        self.assertIn("asset_url('js/disclosure.js')", SERIES_HTML.read_text())
        # Delegated, so the button a boosted visit brings is the one that
        # works, and the panel is found from the button that was pressed.
        js = DISCLOSURE_JS.read_text()
        self.assertIn('closest("[data-disclosure]")', js)
        self.assertIn('getAttribute("aria-controls")', js)


class ThePanel(_Case):
    async def test_it_starts_closed(self):
        await self._series(1, "epub")
        panel = _Element(await self._html(1), "series-order-panel")
        self.assertIn("hidden", panel.attrs["class"].split())

    async def test_everything_that_orders_the_grid_is_inside_it(self):
        await self._series(1, "epub", manual=True)
        html = await self._html(1)
        panel = _Element(html, "series-order-panel")
        for element_id in ("series-sort-control", "series-sort-select", "series-sort-direction"):
            with self.subTest(element=element_id):
                self.assertIn(element_id, panel.ids)
        self.assertEqual(panel.forms, ["/series/1/manual-sort/toggle", "/series/1/manual-sort/reset"])
        self.assertIn("Reorder", panel.words)
        # ...and nothing of it anywhere else on the page.
        everywhere = _Element(html).forms
        self.assertEqual([f for f in everywhere if "manual-sort" in (f or "")], panel.forms)
        self.assertEqual(html.count('id="series-sort-control"'), 1)

    async def test_reset_only_once_there_is_a_manual_order(self):
        await self._series(1, "epub")
        panel = _Element(await self._html(1), "series-order-panel")
        self.assertEqual(panel.forms, ["/series/1/manual-sort/toggle"])

    async def test_a_single_item_keeps_reorder_and_loses_the_sort(self):
        # The rules the row had: no order to choose between one item's,
        # and Reorder regardless.
        await self._series(1, "epub", count=1)
        html = await self._html(1)
        panel = _Element(html, "series-order-panel")
        self.assertNotIn("series-sort-control", panel.ids)
        self.assertEqual(panel.forms, ["/series/1/manual-sort/toggle"])
        self.assertIn('id="series-order-toggle"', html)


class WhileDragging(_Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self._series(1, "epub", manual=True)
        await self.db.execute("UPDATE series SET manual_sort_editing = 1 WHERE id = 1")
        await self.db.commit()
        self.html = await self._html(1)

    def test_the_panel_is_open_and_says_done(self):
        panel = _Element(self.html, "series-order-panel")
        self.assertNotIn("hidden", panel.attrs["class"].split())
        self.assertIn("Done", panel.words)
        self.assertNotIn("Reorder", panel.words)

    def test_the_icon_says_it_is_open(self):
        icon = _Element(self.html, "series-order-toggle")
        self.assertEqual(icon.attrs["aria-expanded"], "true")

    def test_there_is_nothing_to_sort_with(self):
        # Dragging edits the manual order; a grid shown in another order
        # would save nonsense.
        panel = _Element(self.html, "series-order-panel")
        self.assertNotIn("series-sort-control", panel.ids)
        self.assertEqual(panel.forms, ["/series/1/manual-sort/toggle", "/series/1/manual-sort/reset"])


class TheSeriesIconSaysTheOrderWasChanged(_Case):
    async def test_the_server_renders_it_unlit(self):
        # The default order is the one the server rendered, so a page
        # nobody has re-sorted has nothing to say.
        await self._series(1, "epub")
        classes = _Element(await self._html(1), "series-order-toggle").attrs["class"].split()
        self.assertIn("text-neutral-500", classes)
        self.assertNotIn("text-indigo-400", classes)

    def test_both_copies_light_it_by_the_same_rule(self):
        # The pre-paint block lights it before the first frame and
        # series-sort.js keeps it current; two rules would flip it on load.
        default = 'const defaultField = hasManual ? "manual" : "original";'
        prepaint = SERIES_HTML.read_text()
        js = SORT_JS.read_text()
        for source in (prepaint, js):
            self.assertIn(default, source)
            self.assertIn('getElementById("series-order-toggle")', source)
        self.assertIn('const lit = field !== defaultField || dir === "desc";', prepaint)
        self.assertIn('const lit = currentField() !== defaultField || direction === "desc";', js)
        self.assertIn('toggle.classList.add(lit ? "text-indigo-400" : "text-neutral-500");', prepaint)
        self.assertIn('toggle.classList.toggle("text-indigo-400", lit);', js)

    def test_it_is_repainted_wherever_the_arrows_are(self):
        # Every change of field or direction redraws the arrows, and so
        # does the re-sync after a morph put back the server's classes.
        js = SORT_JS.read_text()
        body = js.split("function renderDirection() {", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn("renderToggle();", body)


class TheNovelPanel(NovelPageCase):
    async def html(self):
        response = await novel_detail(1, _request(), None, self.user, self.db)
        return response.template.render(response.context)

    async def test_the_icon_opens_it_and_says_so(self):
        html = await self.html()
        icon = _Element(html, "novel-order-toggle")
        self.assertEqual(icon.attrs["type"], "button")
        self.assertIn("data-disclosure", icon.attrs)
        self.assertEqual(icon.attrs["aria-controls"], "novel-order-panel")
        self.assertEqual(icon.attrs["aria-expanded"], "false")
        self.assertEqual(icon.attrs["aria-label"], "Reorder chapters")
        self.assertEqual(icon.words, [])

    def test_the_icon_sits_with_the_others_beside_the_title(self):
        text = NOVEL_HTML.read_text()
        icon = text.index('id="novel-order-toggle"')
        self.assertLess(text.index("tracker_icon.icon("), icon)
        self.assertLess(icon, text.index("badges.tracking_select("))
        self.assertLess(icon, text.index('<div id="novel-order-panel"'))
        self.assertIn("asset_url('js/disclosure.js')", text)

    async def test_it_starts_closed_and_holds_reorder(self):
        html = await self.html()
        panel = _Element(html, "novel-order-panel")
        self.assertIn("hidden", panel.attrs["class"].split())
        self.assertEqual(panel.forms, ["/novels/1/manual-sort/toggle"])
        self.assertIn("Reorder", panel.words)
        everywhere = _Element(html).forms
        self.assertEqual([f for f in everywhere if "manual-sort" in (f or "")], panel.forms)

    async def test_reset_once_there_is_a_manual_order(self):
        await self.db.execute("UPDATE novels SET manual_sort = 1 WHERE id = 1")
        await self.db.commit()
        panel = _Element(await self.html(), "novel-order-panel")
        self.assertEqual(panel.forms, ["/novels/1/manual-sort/toggle", "/novels/1/manual-sort/reset"])

    async def test_while_dragging_it_is_open_and_says_done(self):
        await self.db.execute("UPDATE novels SET manual_sort = 1, manual_sort_editing = 1 WHERE id = 1")
        await self.db.commit()
        html = await self.html()
        panel = _Element(html, "novel-order-panel")
        self.assertNotIn("hidden", panel.attrs["class"].split())
        self.assertIn("Done", panel.words)
        self.assertEqual(_Element(html, "novel-order-toggle").attrs["aria-expanded"], "true")


if __name__ == "__main__":
    unittest.main()
