"""Which orders a series page offers, and when it offers none.

The sort control lists three: the manual order, the original one, and
reading time. Two of those are conditional, and until now only one of them
said so -- "Manual order" appeared once the series had one, while "Reading
time" was always on the list, so a comic series nobody has dragged into
shape was offered a select holding one usable option and one that could
never do anything.

What makes a comic conditional is not its format but its HISTORY. A comic
is timed by the page at a rate learned inside its own series (see
stats/estimates.py's COMIC_FORMAT), so a series the reader has spent time
on can be ordered by reading time and one they have not cannot. The
question the page asks is can_estimate, never the estimate itself: that is
absent for an item already finished, so keyed off it the control would
come and go as the series is read.

The DIRECTION button is not conditional and must not become so: reversing
the one order there is still answers a real question -- start at the newest
volume -- and with the select gone it is the only thing that can.
"""
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import series_detail

ROOT = Path(__file__).resolve().parents[1]
SERIES_HTML = ROOT / "app/templates/library/series.html"
SORT_JS = ROOT / "app/static/js/series-sort.js"


def _request():
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/series/1", "raw_path": b"/series/1", "query_string": b"", "root_path": "",
        "headers": [], "client": ("test", 1), "server": ("test", 80),
    })


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        self.user = {"id": 1}
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'M','/m','mixed')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def _series(self, series_id, fmt, count=3, manual=False):
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path, manual_sort)"
            " VALUES (?, 1, ?, ?, ?, ?)",
            (series_id, f"S{series_id}", f"s{series_id}", f"/f{series_id}", 1 if manual else 0))
        for i in range(1, count + 1):
            await self.db.execute(
                "INSERT INTO items (library_id, series_id, title, sort_title, file_path,"
                " file_format, file_hash, file_size, text_size, number_in_series)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (series_id, f"v{i}", f"v{i}", f"/f{series_id}/v{i}.{fmt}", fmt,
                 f"h{series_id}{i}", 50000 if fmt == "epub" else None, i))
        await self.db.commit()

    async def _read_a_volume(self, series_id):
        """One volume of this series read through, with its time recorded
        -- the sample its seconds-per-page rate is learned from."""
        row = await (await self.db.execute(
            "SELECT id FROM items WHERE series_id = ? ORDER BY id LIMIT 1",
            (series_id,))).fetchone()
        item_id = row["id"]
        await self.db.execute("UPDATE items SET page_count = 100 WHERE id = ?", (item_id,))
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent,"
            " completed_at, completed_from_percent)"
            " VALUES (1, ?, 'completed', 100, datetime('now'), 100)", (item_id,))
        for _ in range(10):
            await self.db.execute(
                "INSERT INTO page_reads (user_id, item_id, kind, page_index, started_at,"
                " seconds, cap_seconds) VALUES (1, ?, 'cbz', 0, datetime('now'), 180, 180)",
                (item_id,))
        await self.db.commit()

    async def _time_a_book(self):
        """A book read through elsewhere in the library: the ebook rate an
        unstarted book's "Reading time" is ordered by. Without one there is
        nothing to order, and the control is not offered (can_estimate)."""
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (99, 1, 'Timed', 'timed', '/timed')")
        cursor = await self.db.execute(
            "INSERT INTO items (library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size, text_size)"
            " VALUES (1, 99, 't', 't', '/timed/t.epub', 'epub', 'ht', 1, 50000)")
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent,"
            " completed_at, completed_from_percent)"
            " VALUES (1, ?, 'completed', 100, datetime('now'), 100)", (cursor.lastrowid,))
        for _ in range(10):
            await self.db.execute(
                "INSERT INTO page_reads (user_id, item_id, kind, page_index, started_at,"
                " seconds, cap_seconds) VALUES (1, ?, 'epub', 0, datetime('now'), 180, 180)",
                (cursor.lastrowid,))
        await self.db.execute("""INSERT INTO page_reads
            (user_id,item_id,kind,started_at,seconds,cap_seconds,visit_id,pace_words,pace_seconds,pace_samples)
            VALUES(1,?,'epub',datetime('now'),0,0,'valid-word-history',1200,360,3)""", (cursor.lastrowid,))
        await self.db.commit()

    async def _html(self, series_id):
        response = await series_detail(_request(), series_id, None, self.user, self.db)
        return response.template.render(response.context)

    def _options(self, html):
        if 'id="series-sort-select"' not in html:
            return None
        block = html.split('id="series-sort-select"', 1)[1].split("</select>", 1)[0]
        import re
        return re.findall(r'<option value="([a-z]+)"', block)


class WhatEachKindIsOffered(_Case):
    async def test_a_comic_with_no_manual_order_gets_no_select(self):
        # One usable option is not a choice -- the same reason the whole
        # row is skipped for a single item.
        await self._series(1, "cbz")
        self.assertIsNone(self._options(await self._html(1)))

    async def test_but_it_keeps_the_direction_button(self):
        # Reversing the one order there is still answers a real question,
        # and with the select gone it is the only thing that can.
        await self._series(1, "cbz")
        html = await self._html(1)
        self.assertIn('id="series-sort-direction"', html)
        self.assertIn('id="series-sort-control"', html)

    async def test_a_comic_someone_dragged_into_shape_gets_two(self):
        await self._series(1, "cbz", manual=True)
        self.assertEqual(self._options(await self._html(1)), ["manual", "original"])

    async def test_and_still_is_not_offered_reading_time(self):
        # It was, before: an option that could never reorder anything,
        # because no card carries data-eta.
        await self._series(1, "cbz", manual=True)
        self.assertNotIn("eta", self._options(await self._html(1)))

    async def test_a_comic_series_the_reader_has_timed_is_offered_it(self):
        # Once one volume has been read, the series has a seconds-per-page
        # rate and every other volume in it carries an estimate -- so the
        # order means something and is offered. This is the half the
        # format-only rule got wrong.
        await self._series(1, "cbz")
        await self._read_a_volume(1)
        self.assertIn("eta", self._options(await self._html(1)))

    async def test_a_different_series_being_timed_offers_it_here_too(self):
        # Series 1 has no rate of its own, but it borrows the slow end of
        # the others' (see COMIC_FALLBACK_PERCENTILE) -- so every volume
        # carries an estimate, and the order means something.
        await self._series(1, "cbz")
        await self._series(2, "cbz")
        await self._read_a_volume(2)
        self.assertIn("eta", self._options(await self._html(1)))

    async def test_an_ebook_series_gets_original_and_reading_time(self):
        await self._time_a_book()
        await self._series(1, "epub")
        self.assertEqual(self._options(await self._html(1)), ["original", "eta"])

    async def test_a_mixed_series_is_offered_it_too(self):
        await self._time_a_book()
        # One estimable item is enough: the order it puts the grid in is
        # meaningful for that item, and the rest go last in both
        # directions, which is what series-sort.js already does with a
        # card that has no estimate.
        await self._series(1, "cbz", count=2)
        await self.db.execute(
            "INSERT INTO items (library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size, text_size, number_in_series)"
            " VALUES (1, 1, 'v3', 'v3', '/f1/v3.epub', 'epub', 'h13', 1, 50000, 3)")
        await self.db.commit()
        self.assertIn("eta", self._options(await self._html(1)))

    async def test_a_single_item_still_gets_no_control_at_all(self):
        # The rule that was already there, and the reason this one reads
        # the same way.
        await self._series(1, "epub", count=1)
        self.assertNotIn('id="series-sort-control"', await self._html(1))


class ItAsksTheFormatNotTheEstimate(_Case):
    async def test_a_finished_ebook_series_still_offers_reading_time(self):
        await self._time_a_book()
        # eta_map is empty here -- for_item returns None for a completed
        # row -- so keying the option off it would make the control appear
        # and disappear as the series is read.
        await self._series(1, "epub")
        for item_id, in await (await self.db.execute(
                "SELECT id FROM items WHERE series_id = 1")).fetchall():
            await self.db.execute(
                "INSERT INTO reading_progress (user_id, item_id, status, progress_percent,"
                " completed_at, completed_from_percent)"
                " VALUES (1, ?, 'completed', 100, datetime('now'), 100)", (item_id,))
        await self.db.commit()

        response = await series_detail(_request(), 1, None, self.user, self.db)
        self.assertEqual(response.context["eta_map"], {})
        self.assertTrue(response.context["can_sort_by_eta"])
        self.assertIn("eta", self._options(response.template.render(response.context)))


class BothCopiesSurviveAMissingSelect(unittest.TestCase):
    """The order is decided twice -- once before paint, once on load."""

    def test_neither_gives_up_when_there_is_no_select(self):
        # The pre-paint block returning early here left the deferred script
        # to reverse a grid that had already painted. Measured on a
        # 200-item series, with that early return restored as the control:
        # frame 2 held all 200 in the server's order and frame 3 reversed
        # them; with it gone there is no such frame.
        prepaint = SERIES_HTML.read_text().split("const control = document.getElementById"
                                                 '("series-sort-control")', 1)[1]
        prepaint = prepaint.split("</script>", 1)[0]
        self.assertNotIn("if (!control || !select) return", prepaint)
        self.assertNotIn("if (!select) return", prepaint)
        self.assertNotIn("if (!select) return;", SORT_JS.read_text())

    def test_both_read_the_field_the_same_way_when_it_is_absent(self):
        # "original" is the only order a page with no select can be in, and
        # the two copies have to agree on that or they reorder differently.
        self.assertIn('select ? select.value : "original"', SORT_JS.read_text())
        self.assertIn('select ? select.value : "original"', SERIES_HTML.read_text())


if __name__ == "__main__":
    unittest.main()
