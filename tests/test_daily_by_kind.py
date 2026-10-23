"""The daily charts, stacked by what was read.

"Time read per day" and "Pages per day" were one colour each. They are a
column per day now with one segment per kind -- comics, ebooks, web novels,
and "Other" for time spent on something since deleted -- and a tooltip
listing each kind and the total. What these pin:

* the split is exact: the kinds of a day add up to the day, so a column and
  its segments cannot disagree about how much was read;
* a kind keeps its colour whatever else the window holds, because the slot
  is fixed per kind and never handed out by rank;
* the tooltip's text is rendered by the server, into the column's own
  aria-label as well, so nothing it says is reachable only by hovering;
* the legend appears when there is more than one kind to tell apart.

Measured in both browsers against the real build: the segments' heights
follow their numbers (35m / 21m / 26m / 5m drew 42 / 25 / 32 / 6 px), the
tooltip lists every kind and the total and follows the keyboard, and it
survives the window being swapped.
"""
import html
import json
import re
from datetime import date

from starlette.requests import Request

from app.stats import queries
from app.template_filters import format_duration
from app.stats.router import DAILY_KIND_SLOTS, daily_activity_partial
from tests.test_stats import StatsCase


def _request():
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/stats/daily-activity", "raw_path": b"/stats/daily-activity",
        "query_string": b"", "root_path": "", "headers": [],
        "client": ("test", 1), "server": ("test", 80),
    })


class ByKind(StatsCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.item(1, fmt="cbz")
        await self.item(2, library_id=2, fmt="epub")
        await self.novel_chapter(10)

    async def read(self, *, seconds, item_id=None, chapter_id=None, ago_days=0, kind=None):
        """One page_read, `ago_days` back. Written directly so a test can
        place it on an earlier day and give it a kind with no content --
        what a deleted item leaves behind."""
        if kind is None:
            kind = "novel" if chapter_id else (
                "cbz" if item_id == 1 else "epub")
        await self.db.execute(
            "INSERT INTO page_reads (user_id, item_id, novel_id, chapter_id, kind, page_index,"
            " started_at, seconds, cap_seconds)"
            " VALUES (1, ?, ?, ?, ?, 0, datetime('now', 'localtime', ?), ?, 180)",
            (item_id, 1 if chapter_id else None, chapter_id, kind,
             f"-{ago_days} days", seconds))
        await self.db.commit()

    async def today(self):
        return (await queries.daily_activity(self.db, 1, days=1))[-1]


class TheSplitAddsUp(ByKind):
    async def test_each_kind_is_counted_on_its_own(self):
        await self.read(seconds=60, item_id=1)
        await self.read(seconds=30, item_id=1)
        await self.read(seconds=45, item_id=2)
        await self.read(seconds=20, chapter_id=10)
        day = await self.today()
        self.assertEqual(day["kinds"], {
            "cbz": {"seconds": 90, "pages": 2},
            "epub": {"seconds": 45, "pages": 1},
            "novel": {"seconds": 20, "pages": 1},
        })

    async def test_the_day_is_the_sum_of_its_kinds(self):
        await self.read(seconds=60, item_id=1)
        await self.read(seconds=45, item_id=2)
        await self.read(seconds=20, chapter_id=10)
        day = await self.today()
        self.assertEqual(day["seconds"], 125)
        self.assertEqual(day["pages"], 3)

    async def test_the_cap_applies_per_page_before_the_split(self):
        # 400s on one page counts as the 180s cap, as it does everywhere
        # else on the page -- the split must not bring the raw time back.
        await self.read(seconds=400, item_id=1)
        day = await self.today()
        self.assertEqual(day["kinds"]["cbz"]["seconds"], 180)
        self.assertEqual(day["seconds"], 180)

    async def test_something_since_deleted_is_still_its_own_kind(self):
        await self.read(seconds=50, kind="unknown")
        self.assertEqual((await self.today())["kinds"], {"unknown": {"seconds": 50, "pages": 1}})

    async def test_an_empty_day_has_no_kinds_and_zeros(self):
        await self.read(seconds=60, item_id=1, ago_days=1)
        day = await self.today()
        self.assertEqual((day["kinds"], day["seconds"], day["pages"]), ({}, 0, 0))

    async def test_each_day_keeps_its_own_kinds(self):
        await self.read(seconds=60, item_id=1, ago_days=1)
        await self.read(seconds=40, item_id=2)
        daily = await queries.daily_activity(self.db, 1, days=2)
        self.assertEqual([sorted(d["kinds"]) for d in daily], [["cbz"], ["epub"]])


class TheChart(ByKind):
    async def _html(self):
        response = await daily_activity_partial(
            _request(), days=30, from_=None, to=None, user={"id": 1}, db=self.db)
        return response.body.decode()

    def _columns(self, html_text, chart):
        block = re.findall(r"<div class=\"viz-columns[^\"]*\"[^>]*data-daily-chart>(.*?)\n    </div>",
                           html_text, re.S)[chart]
        return re.findall(r"<div tabindex=\"(-?\d)\" role=\"img\"\s*aria-label=\"([^\"]*)\"\s*"
                          r"data-tip='([^']*)'>(.*?)</div>", block, re.S)

    def _segments(self, body):
        return re.findall(r'<span class="viz-(\w+)"\s*style="flex-grow: ([\d.]+);"', body)

    async def test_one_segment_per_kind_in_baseline_order_and_fixed_slots(self):
        await self.read(seconds=20, chapter_id=10)
        await self.read(seconds=45, item_id=2)
        await self.read(seconds=90, item_id=1)
        today = self._columns(await self._html(), 0)[-1]
        self.assertEqual(self._segments(today[3]),
                         [("1", "90.0"), ("2", "45.0"), ("3", "20.0")])

    async def test_comic_pages_chart_contains_only_comics(self):
        await self.read(seconds=90, item_id=1)
        await self.read(seconds=30, item_id=1)
        await self.read(seconds=45, item_id=2)
        today = self._columns(await self._html(), 1)[-1]
        self.assertEqual(self._segments(today[3]), [("1", "2")])
        data = json.loads(html.unescape(today[2]))
        self.assertEqual(data['rows'], [['Comics', '2 pages', 1]])
        self.assertEqual(data['total'], '2 pages')
        self.assertNotIn('Ebooks', html.unescape(today[1]))

    async def test_text_only_days_keep_a_zero_comic_pages_chart(self):
        await self.read(seconds=45, item_id=2)
        await self.read(seconds=20, chapter_id=10)
        page = await self._html()
        self.assertIn('Comic pages per day', page)
        for _, label, tip, body in self._columns(page, 1):
            data = json.loads(html.unescape(tip))
            self.assertEqual(data['rows'], [['Comics', '0 pages', 1]])
            self.assertEqual(data['total'], '0 pages')
            self.assertIn('Comics 0 pages', label)
            self.assertIn('viz-empty', body)
        time_data = json.loads(html.unescape(self._columns(page, 0)[-1][2]))
        self.assertEqual([row[0] for row in time_data['rows']], ['Ebooks', 'Web novels'])

    async def test_a_kind_keeps_its_colour_when_it_is_alone(self):
        # Color follows the thing, not its rank: a window of nothing but
        # web novels still draws them in the web-novel slot, not the first.
        await self.read(seconds=20, chapter_id=10)
        today = self._columns(await self._html(), 0)[-1]
        self.assertEqual(self._segments(today[3]), [("3", "20.0")])
        self.assertEqual(DAILY_KIND_SLOTS["novel"], 3)

    async def test_the_tooltip_lists_each_kind_and_the_total(self):
        await self.read(seconds=90, item_id=1)
        await self.read(seconds=45, item_id=2)
        _, label, tip, _ = self._columns(await self._html(), 0)[-1]
        data = json.loads(html.unescape(tip))
        # Through the page's own `duration` filter, whatever it rounds to.
        self.assertEqual(data["rows"], [["Comics", format_duration(90), 1],
                                        ["Ebooks", format_duration(45), 2]])
        self.assertEqual(data["total"], format_duration(135))
        self.assertIn(str(date.today().year), data["day"])

    async def test_the_same_text_is_the_columns_accessible_name(self):
        await self.read(seconds=90, item_id=1)
        await self.read(seconds=45, item_id=2)
        _, label, _, _ = self._columns(await self._html(), 0)[-1]
        self.assertTrue(html.unescape(label).endswith(
            f": Comics {format_duration(90)}, Ebooks {format_duration(45)},"
            f" total {format_duration(135)}"))

    async def test_an_empty_day_says_so(self):
        await self.read(seconds=60, item_id=1)
        columns = self._columns(await self._html(), 0)
        _, label, tip, body = columns[0]
        self.assertTrue(label.endswith("total nothing read"))
        self.assertEqual(json.loads(html.unescape(tip))["rows"], [])
        self.assertIn("viz-empty", body)

    async def test_one_tab_stop_per_chart_the_latest_day(self):
        await self.read(seconds=60, item_id=1)
        for chart in (0, 1):
            with self.subTest(chart=chart):
                stops = [c[0] for c in self._columns(await self._html(), chart)]
                self.assertEqual(stops.count("0"), 1)
                self.assertEqual(stops[-1], "0")

    async def test_no_native_title_to_open_over_the_tooltip(self):
        await self.read(seconds=60, item_id=1)
        page = await self._html()
        charts = re.findall(r"data-daily-chart>(.*?)\n    </div>", page, re.S)
        self.assertEqual(len(charts), 2)
        for chart in charts:
            self.assertNotIn("title=", chart)


class TheLegend(ByKind):
    _html = TheChart._html

    def _legend(self, page):
        found = re.search(r'aria-label="Legend">(.*?)</ul>', page, re.S)
        return re.findall(r"viz-key viz-(\w+)\"[^>]*></span>\s*([^<]+?)\s*</li>",
                          found.group(1)) if found else None

    async def test_it_names_each_kind_in_the_window_in_stack_order(self):
        await self.read(seconds=20, chapter_id=10, ago_days=3)
        await self.read(seconds=90, item_id=1)
        self.assertEqual(self._legend(await self._html()),
                         [("1", "Comics"), ("3", "Web novels")])

    async def test_a_single_kind_needs_none(self):
        await self.read(seconds=90, item_id=1)
        await self.read(seconds=30, item_id=1, ago_days=2)
        self.assertIsNone(self._legend(await self._html()))

    async def test_a_kind_outside_the_window_is_not_in_it(self):
        await self.read(seconds=90, item_id=1)
        await self.read(seconds=40, item_id=2)
        await self.read(seconds=20, chapter_id=10, ago_days=45)
        self.assertEqual(self._legend(await self._html()),
                         [("1", "Comics"), ("2", "Ebooks")])


if __name__ == "__main__":
    import unittest
    unittest.main()
