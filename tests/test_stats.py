"""Reading-time accounting and the statistics aggregations.

The cases that matter most here are the two worked examples the feature was
specified with, and the property that makes the per-page limit safe to
change: a page already recorded keeps the limit it was measured under.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi import HTTPException

from app.config import settings
from app.database import get_connection, init_db
from app.stats import queries, router
from app.stats.recording import MAX_SPANS_PER_REQUEST, MIN_SPAN_SECONDS, record_spans


class StatsCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'Manga', '/m', 'comic')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (2, 'Books', '/b', 'ebook')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def item(self, item_id, *, library_id=1, series_id=None, fmt="cbz", title=None):
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path, file_format,"
            " file_hash, file_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (item_id, library_id, series_id, title or f"Item {item_id}", f"item {item_id}",
             f"/x/{item_id}", fmt, f"h{item_id}"),
        )
        await self.db.commit()
        return item_id

    async def series(self, series_id, *, library_id=1, status=None):
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path) VALUES (?, ?, ?, ?, ?)",
            (series_id, library_id, f"S{series_id}", f"s{series_id}", f"/f{series_id}"))
        if status:
            await self.db.execute(
                "INSERT INTO series_status (user_id, series_id, status) VALUES (1, ?, ?)",
                (series_id, status))
        await self.db.commit()
        return series_id

    async def novel_chapter(self, chapter_id, *, novel_id=1, volume=1):
        await self.db.execute(
            "INSERT OR IGNORE INTO novels (id, title, source_url) VALUES (?, 'N', ?)",
            (novel_id, f"https://x/{novel_id}"))
        volume_id = novel_id * 100 + volume
        await self.db.execute(
            "INSERT OR IGNORE INTO volumes (id, novel_id, number) VALUES (?, ?, ?)",
            (volume_id, novel_id, volume))
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url) VALUES (?, ?, ?, ?, ?, ?)",
            (chapter_id, novel_id, volume_id, float(chapter_id), f"C{chapter_id}", f"https://x/c{chapter_id}"))
        await self.db.commit()
        return chapter_id

    async def mark_chapter_read(self, *chapter_ids):
        for chapter_id in chapter_ids:
            await self.db.execute(
                "INSERT INTO novel_read_chapters (user_id, chapter_id) VALUES (1, ?)", (chapter_id,))
        await self.db.commit()

    async def record(self, spans, cap=180):
        stored = await record_spans(self.db, 1, cap, spans)
        await self.db.commit()
        return stored

    async def counted(self):
        """Counted time as the STATISTICS PAGE computes it.

        Deliberately goes through the real aggregation rather than issuing
        its own SUM: an earlier version of this helper had its own copy of
        the SQL, and a mutation that dropped the cap from the production
        query left every worked-example test below green. A test that
        asserts against its own reimplementation tests nothing.
        """
        return (await queries.reading_totals(self.db, 1))["seconds"]


class WorkedExamples(StatsCase):
    """The two examples the feature was specified with, end to end."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.item(1)

    async def test_revisiting_a_page_counts_both_visits(self):
        # 60s on page 10, 10s on 11, 30s back on 10, 60s finishing 11.
        # The answer is 160 -- NOT "the clock from first opening 10 to
        # finally leaving 11", and not "the longest visit to each page".
        await self.record([
            {"item_id": 1, "page_index": 10, "seconds": 60, "ago": 160},
            {"item_id": 1, "page_index": 11, "seconds": 10, "ago": 150},
            {"item_id": 1, "page_index": 10, "seconds": 30, "ago": 120},
            {"item_id": 1, "page_index": 11, "seconds": 60, "ago": 60},
        ])
        self.assertEqual(await self.counted(), 160)

    async def test_a_page_over_the_limit_counts_as_the_limit(self):
        # 200s on page 13 (limit 180), then 20s on 14, then 40s back on 13.
        # 180 + 20 + 40 = 240: the limit truncates that ONE visit, and
        # coming back earns a fresh allowance rather than being folded into
        # the visit that was already truncated.
        await self.record([
            {"item_id": 1, "page_index": 13, "seconds": 200, "ago": 60},
            {"item_id": 1, "page_index": 14, "seconds": 20, "ago": 40},
            {"item_id": 1, "page_index": 13, "seconds": 40, "ago": 0},
        ])
        self.assertEqual(await self.counted(), 240)

    async def test_the_raw_duration_is_kept_alongside_the_limit(self):
        # What makes the history recomputable under a different limit later.
        await self.record([{"item_id": 1, "page_index": 1, "seconds": 200, "ago": 0}])
        row = await (await self.db.execute(
            "SELECT seconds, cap_seconds FROM page_reads")).fetchone()
        self.assertEqual(row["seconds"], 200)
        self.assertEqual(row["cap_seconds"], 180)


class ChangingTheLimit(StatsCase):
    async def test_an_earlier_row_keeps_the_limit_it_was_measured_under(self):
        # The property the whole per-row cap_seconds column exists for: the
        # statistics must not silently rewrite themselves when the setting
        # moves. The alternative design -- one current limit applied to
        # everything -- would turn the first row below from 180 into 30.
        await self.item(1)
        await self.record([{"item_id": 1, "page_index": 1, "seconds": 200, "ago": 0}], cap=180)
        await self.record([{"item_id": 1, "page_index": 2, "seconds": 200, "ago": 0}], cap=30)
        self.assertEqual(await self.counted(), 210)
        caps = [r[0] for r in await (await self.db.execute(
            "SELECT cap_seconds FROM page_reads ORDER BY id")).fetchall()]
        self.assertEqual(caps, [180, 30])


class SpanValidation(StatsCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.item(1)
        await self.novel_chapter(1)

    async def test_a_span_below_the_floor_is_dropped(self):
        self.assertEqual(
            await self.record([{"item_id": 1, "page_index": 1,
                                "seconds": MIN_SPAN_SECONDS / 2, "ago": 0}]), 0)

    async def test_a_span_exactly_at_the_floor_is_kept(self):
        self.assertEqual(
            await self.record([{"item_id": 1, "page_index": 1,
                                "seconds": MIN_SPAN_SECONDS, "ago": 0}]), 1)

    async def test_an_absurd_duration_is_clamped_not_dropped(self):
        # A suspended laptop or a clock jump. Clamping keeps the raw column
        # meaningful; it changes no statistic, since the limit is far below.
        await self.record([{"item_id": 1, "page_index": 1, "seconds": 10 ** 9, "ago": 0}])
        row = await (await self.db.execute("SELECT seconds FROM page_reads")).fetchone()
        self.assertLess(row["seconds"], 10 ** 9)
        self.assertEqual(await self.counted(), 180)

    async def test_a_span_must_name_exactly_one_kind(self):
        both = {"item_id": 1, "chapter_id": 1, "page_index": 1, "seconds": 10, "ago": 0}
        neither = {"page_index": 1, "seconds": 10, "ago": 0}
        self.assertEqual(await self.record([both, neither]), 0)

    async def test_unparseable_values_are_dropped_not_raised(self):
        self.assertEqual(
            await self.record([{"item_id": 1, "page_index": 1, "seconds": "x", "ago": 0}]), 0)

    async def test_a_request_cannot_store_more_than_the_limit(self):
        spans = [{"item_id": 1, "page_index": i, "seconds": 5, "ago": 0}
                 for i in range(MAX_SPANS_PER_REQUEST + 50)]
        self.assertEqual(await self.record(spans), MAX_SPANS_PER_REQUEST)

    async def test_a_span_claiming_to_be_ancient_is_dropped_not_moved(self):
        # `ago` decides which DAY the row is filed under, so an implausible
        # value cannot be clamped into range the way an implausible duration
        # can -- clamping would silently move a session onto a day it did
        # not happen on, which is precisely what the daily charts would then
        # be wrong about. Losing one span is the cheaper failure.
        self.assertEqual(
            await self.record([{"item_id": 1, "page_index": 1, "seconds": 10,
                                "ago": 40 * 24 * 3600}]), 0)

    async def test_a_negative_ago_is_dropped(self):
        self.assertEqual(
            await self.record([{"item_id": 1, "page_index": 1, "seconds": 10, "ago": -5}]), 0)

    async def test_the_start_time_is_derived_from_how_long_ago_it_ended(self):
        # The client sends an elapsed duration, never a wall-clock time, so
        # a device with a wrong clock cannot file reading under a wrong day.
        await self.record([{"item_id": 1, "page_index": 1, "seconds": 10, "ago": 0},
                           {"item_id": 1, "page_index": 2, "seconds": 10, "ago": 600}])
        rows = [r[0] for r in await (await self.db.execute(
            "SELECT strftime('%s', 'now') - strftime('%s', started_at) FROM page_reads ORDER BY id"
        )).fetchall()]
        self.assertLess(abs(rows[0] - 10), 3)
        self.assertLess(abs(rows[1] - 610), 3)


class TimePerPage(StatsCase):
    async def test_totals_and_the_average(self):
        await self.item(1)
        await self.record([{"item_id": 1, "page_index": i, "seconds": 20, "ago": 0} for i in range(5)])
        totals = await queries.reading_totals(self.db, 1)
        self.assertEqual(totals["pages"], 5)
        self.assertEqual(totals["seconds"], 100)
        self.assertEqual(totals["seconds_per_page"], 20)

    async def test_no_history_divides_by_nothing(self):
        totals = await queries.reading_totals(self.db, 1)
        self.assertEqual(totals["pages"], 0)
        self.assertEqual(totals["seconds_per_page"], 0)

    async def test_split_by_what_was_being_read(self):
        # A comic page is an image and a text page is a paragraph position,
        # so one average over both would describe neither.
        await self.item(1, fmt="cbz")
        await self.item(2, library_id=2, fmt="epub")
        await self.novel_chapter(1)
        await self.record([
            {"item_id": 1, "page_index": 0, "seconds": 10, "ago": 0},
            {"item_id": 2, "page_index": 0, "seconds": 40, "ago": 0},
            {"novel_id": 1, "chapter_id": 1, "page_index": 0, "seconds": 30, "ago": 0},
        ])
        by_kind = {r["kind"]: r["seconds"] for r in
                   await queries.seconds_per_page_by_kind(self.db, 1)}
        self.assertEqual(by_kind, {"cbz": 10, "epub": 40, "novel": 30})


class TimeToFinish(StatsCase):
    async def test_only_finished_things_are_averaged(self):
        # "How long does a volume take" is not a question a half-read volume
        # can answer; including one would drag the average down by however
        # far the reader happened to get.
        await self.item(1)
        await self.item(2)
        await self.record([
            {"item_id": 1, "page_index": 0, "seconds": 100, "ago": 0},
            {"item_id": 2, "page_index": 0, "seconds": 10, "ago": 0},
        ])
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status) VALUES (1, 1, 'completed')")
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status) VALUES (1, 2, 'reading')")
        await self.db.commit()
        finish = await queries.time_to_finish(self.db, 1)
        # `fmt` defaults to cbz in this file's fixture, so these are comics.
        self.assertEqual(finish["per_comic"]["n"], 1)
        self.assertEqual(finish["per_comic"]["avg_seconds"], 100)
        # And the book half is empty rather than missing: the tile has to
        # be able to say "nothing finished yet" without a KeyError.
        self.assertEqual(finish["per_book"]["n"], 0)

    async def test_a_volume_counts_only_once_every_chapter_in_it_is_read(self):
        # The novel-side equivalent of the rule above: a volume with a
        # chapter still unread is a half-read volume, and cannot say how
        # long a volume takes.
        await self.novel_chapter(1, volume=1)
        await self.novel_chapter(2, volume=1)
        await self.record([
            {"novel_id": 1, "chapter_id": 1, "page_index": 0, "seconds": 60, "ago": 0},
            {"novel_id": 1, "chapter_id": 2, "page_index": 0, "seconds": 20, "ago": 0},
        ])
        await self.mark_chapter_read(1)
        finish = await queries.time_to_finish(self.db, 1)
        self.assertEqual(finish["per_volume"]["n"], 0)

        await self.mark_chapter_read(2)
        finish = await queries.time_to_finish(self.db, 1)
        self.assertEqual(finish["per_volume"]["n"], 1)
        # The volume's time is its chapters' time added up, not averaged.
        self.assertEqual(finish["per_volume"]["avg_seconds"], 80)

    async def test_a_volume_read_before_timing_existed_is_not_averaged_as_zero(self):
        # Nothing in page_reads for volume 2, which is exactly what a
        # library read before this feature shipped looks like. Counting it
        # as a zero-second volume would halve the average.
        await self.novel_chapter(1, volume=1)
        await self.novel_chapter(2, volume=2)
        await self.record([
            {"novel_id": 1, "chapter_id": 1, "page_index": 0, "seconds": 60, "ago": 0},
        ])
        await self.mark_chapter_read(1, 2)
        finish = await queries.time_to_finish(self.db, 1)
        self.assertEqual(finish["per_volume"]["n"], 1)
        self.assertEqual(finish["per_volume"]["avg_seconds"], 60)

    async def test_the_result_keys_survive_template_attribute_lookup(self):
        # Jinja resolves `x.items` with getattr BEFORE the mapping, and
        # `dict.items` exists -- so a key named "items" renders as a bound
        # method, silently, and the tile read "nothing finished yet" however
        # much was finished. Caught in a real browser; pinned here so the
        # keys cannot drift back onto a dict method name.
        finish = await queries.time_to_finish(self.db, 1)
        for key in finish:
            self.assertFalse(hasattr({}, key), f"{key!r} collides with a dict attribute")


class DailyActivity(StatsCase):
    async def test_days_with_nothing_are_filled_in(self):
        # A bar chart with the empty days missing compresses a week off into
        # nothing, so it reads identically to a week of steady reading.
        await self.item(1)
        await self.record([{"item_id": 1, "page_index": 0, "seconds": 30, "ago": 0}])
        daily = await queries.daily_activity(self.db, 1, days=7)
        self.assertEqual(len(daily), 7)
        self.assertEqual(sum(1 for d in daily if d["pages"]), 1)
        self.assertEqual(sum(d["seconds"] for d in daily), 30)

    async def test_it_is_ordered_oldest_first_and_ends_today(self):
        daily = await queries.daily_activity(self.db, 1, days=5)
        days = [d["day"] for d in daily]
        self.assertEqual(days, sorted(days))
        self.assertEqual(days[-1], date.today().isoformat())
        self.assertEqual(days[0], (date.today() - timedelta(days=4)).isoformat())

    async def test_an_explicit_range_is_inclusive_of_both_ends(self):
        await self.item(1)
        for ago in (0, 2, 4):
            await self.db.execute(
                "INSERT INTO page_reads (user_id, item_id, kind, page_index, started_at, seconds, cap_seconds)"
                " VALUES (1, 1, (SELECT file_format FROM items WHERE id = 1), 0,"
                " datetime('now', 'localtime', ?), 30, 180)", (f"-{ago} days",))
        await self.db.commit()
        today = date.today()
        daily = await queries.daily_activity(
            self.db, 1, start=today - timedelta(days=4), end=today - timedelta(days=2))
        self.assertEqual([d["day"] for d in daily],
                         [(today - timedelta(days=n)).isoformat() for n in (4, 3, 2)])
        # The row four days back and the row two days back are both in --
        # a range that dropped its own endpoints would silently lose the
        # first and last column of every custom window.
        self.assertEqual([d["pages"] for d in daily], [1, 0, 1])

    async def test_a_range_and_the_equivalent_day_count_agree(self):
        # `days=` is the same query with the arithmetic done for it; if the
        # two ever diverged, the presets and a hand-picked range covering
        # the same span would draw different charts.
        await self.item(1)
        await self.record([{"item_id": 1, "page_index": 0, "seconds": 90, "ago": 0}])
        today = date.today()
        self.assertEqual(
            await queries.daily_activity(self.db, 1, days=10),
            await queries.daily_activity(self.db, 1, start=today - timedelta(days=9), end=today))

    async def test_a_range_ending_in_the_past_excludes_what_came_after(self):
        await self.item(1)
        await self.record([{"item_id": 1, "page_index": 0, "seconds": 90, "ago": 0}])
        today = date.today()
        daily = await queries.daily_activity(
            self.db, 1, start=today - timedelta(days=9), end=today - timedelta(days=5))
        self.assertEqual(len(daily), 5)
        self.assertEqual(sum(d["pages"] for d in daily), 0)

    async def test_a_single_day_range_is_one_column(self):
        today = date.today()
        daily = await queries.daily_activity(self.db, 1, start=today, end=today)
        self.assertEqual([d["day"] for d in daily], [today.isoformat()])

    async def test_older_reading_falls_outside_the_window(self):
        # Inserted directly rather than through record_spans: a span is only
        # ever reported moments after it ends (see MAX_AGO_SECONDS), so this
        # is what genuinely old rows look like, not what a client can send.
        await self.item(1)
        await self.db.execute(
            "INSERT INTO page_reads (user_id, item_id, kind, page_index, started_at, seconds, cap_seconds)"
            " VALUES (1, 1, (SELECT file_format FROM items WHERE id = 1), 0,"
            " datetime('now', '-40 days'), 30, 180)")
        await self.db.commit()
        daily = await queries.daily_activity(self.db, 1, days=7)
        self.assertEqual(sum(d["pages"] for d in daily), 0)
        # ...but it is still part of the all-time totals.
        self.assertEqual((await queries.reading_totals(self.db, 1))["pages"], 1)


class LibraryComposition(StatsCase):
    async def test_counts_per_library_and_rolled_up_by_media_type(self):
        await self.series(1)
        await self.series(2, library_id=2)
        await self.item(1, series_id=1)
        await self.item(2, series_id=1)
        await self.item(3, library_id=2, series_id=2)
        per_library = {lib["name"]: lib for lib in await queries.library_totals(self.db)}
        self.assertEqual(per_library["Manga"]["series_count"], 1)
        self.assertEqual(per_library["Manga"]["item_count"], 2)
        self.assertEqual(per_library["Books"]["item_count"], 1)
        rolled = {t["media_type"]: t for t in queries.totals_by_media_type(await queries.library_totals(self.db))}
        self.assertEqual(rolled["comic"]["item_count"], 2)
        self.assertEqual(rolled["ebook"]["item_count"], 1)

    async def test_a_series_with_no_tracking_status_is_reported_as_none(self):
        # On a library that has never been curated that is every row; a
        # chart that silently showed nothing there would read as a bug.
        await self.series(1, status="reading")
        await self.series(2)
        # A series counts here while it is on the shelf, which means
        # holding something -- see library/series_scope.py.
        await self.item(10, series_id=1)
        await self.item(20, series_id=2)
        rows = {r["status"]: r["n"] for r in await queries.series_by_status(self.db, 1)}
        self.assertEqual(rows, {"reading": 1, "none": 1})

    async def test_an_item_with_no_progress_row_is_unread(self):
        # Same convention the library grid renders by, so the two agree.
        await self.item(1)
        await self.item(2)
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status) VALUES (1, 1, 'completed')")
        await self.db.commit()
        rows = {r["status"]: r["n"] for r in await queries.items_by_status(self.db, 1)}
        self.assertEqual(rows, {"completed": 1, "unread": 1})

    async def test_another_users_status_does_not_leak_in(self):
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (2, 'v', 'x')")
        await self.series(1)
        await self.item(10, series_id=1)
        await self.db.execute(
            "INSERT INTO series_status (user_id, series_id, status) VALUES (2, 1, 'completed')")
        await self.db.commit()
        rows = {r["status"]: r["n"] for r in await queries.series_by_status(self.db, 1)}
        self.assertEqual(rows, {"none": 1})


class WebNovelsBesideTheLibraries(StatsCase):
    """Web novels are drawn in the Libraries section in the libraries' own
    shape: novels by tracking status, CHAPTERS by reading status."""

    async def test_chapters_by_reading_status(self):
        for chapter_id in (1, 2, 3):
            await self.novel_chapter(chapter_id)
        await self.mark_chapter_read(1)
        await self.db.execute(
            "INSERT INTO novel_reading_progress (user_id, novel_id, chapter_id) VALUES (1, 1, 2)")
        await self.db.commit()
        novels = await queries.novel_totals(self.db, 1)
        self.assertEqual({r["status"]: r["n"] for r in novels["by_chapter_status"]},
                         {"completed": 1, "reading": 1, "unread": 1})

    async def test_a_position_on_a_read_chapter_is_not_reading_too(self):
        for chapter_id in (1, 2):
            await self.novel_chapter(chapter_id)
        await self.mark_chapter_read(1)
        await self.db.execute(
            "INSERT INTO novel_reading_progress (user_id, novel_id, chapter_id) VALUES (1, 1, 1)")
        await self.db.commit()
        novels = await queries.novel_totals(self.db, 1)
        self.assertEqual({r["status"]: r["n"] for r in novels["by_chapter_status"]},
                         {"completed": 1, "reading": 0, "unread": 1})

    async def test_they_are_one_more_block_with_their_own_nouns(self):
        from app.stats.router import _with_novels
        await self.novel_chapter(1)
        await self.novel_chapter(2)
        await self.mark_chapter_read(1)
        series_status, item_status = {}, {}
        blocks = _with_novels([{"media_type": "comic"}], series_status, item_status,
                              await queries.novel_totals(self.db, 1))
        self.assertEqual([(b["media_type"], b["series_noun"], b["item_noun"]) for b in blocks],
                         [("comic", "Series", "Items"), ("novel", "Novels", "Chapters")])
        self.assertEqual([(s["label"], s["value"]) for s in item_status["novel"]["statuses"]],
                         [("Read", 1), ("Unread", 1)])
        self.assertEqual([(s["label"], s["value"]) for s in series_status["novel"]["statuses"]],
                         [("No status", 1)])

    async def test_no_novels_no_block(self):
        from app.stats.router import _with_novels
        blocks = _with_novels([], {}, {}, await queries.novel_totals(self.db, 1))
        self.assertEqual(blocks, [])


class RecentItems(StatsCase):
    """One entry per series, opening onto its items -- see recent_items."""

    async def test_it_names_what_the_time_was_spent_on(self):
        await self.series(1)
        await self.item(1, series_id=1, title="Serie Alfa v01")
        await self.record([{"item_id": 1, "page_index": 0, "seconds": 90, "ago": 0}])
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual((entries[0]["name"], entries[0]["seconds"]), ("S1", 90))
        self.assertEqual([i["title"] for i in entries[0]["items"]], ["Serie Alfa v01"])
        self.assertTrue(entries[0]["opens"])

    async def test_short_items_of_one_series_are_one_entry(self):
        # THE case: an evening of short chapters filled every row with one
        # series and pushed everything else off the list.
        await self.series(1)
        await self.series(2)
        await self.item(99, series_id=2, title="Other v01")
        spans = [{"item_id": 99, "page_index": 0, "seconds": 120, "ago": 2000}]
        for n in range(1, 13):
            await self.item(n, series_id=1, title=f"Chapter {n}")
            spans.append({"item_id": n, "page_index": 0, "seconds": 70, "ago": 1000 - n})
        await self.record(spans)
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([e["name"] for e in entries], ["S1", "S2"])
        self.assertEqual(entries[0]["seconds"], 12 * 70)
        # Newest first inside the series, and every item, adding up.
        self.assertEqual([i["title"] for i in entries[0]["items"]][:3],
                         ["Chapter 12", "Chapter 11", "Chapter 10"])
        self.assertEqual(len(entries[0]["items"]), 12)
        self.assertEqual(sum(i["seconds"] for i in entries[0]["items"]), entries[0]["seconds"])

    async def test_a_novel_is_one_entry_opening_onto_its_volumes(self):
        # Chapters are short and numerous: listing them one by one filled
        # this section with the same novel over and over.
        await self.novel_chapter(1, volume=1)
        await self.novel_chapter(2, volume=1)
        await self.novel_chapter(3, volume=2)
        await self.record([
            {"novel_id": 1, "chapter_id": 1, "page_index": 0, "seconds": 40, "ago": 600},
            {"novel_id": 1, "chapter_id": 2, "page_index": 0, "seconds": 30, "ago": 300},
            {"novel_id": 1, "chapter_id": 3, "page_index": 0, "seconds": 90, "ago": 0},
        ])
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([(e["name"], e["kind"], e["seconds"]) for e in entries], [("N", "novel", 160)])
        self.assertEqual([(i["title"], i["seconds"]) for i in entries[0]["items"]],
                         [("Volume 2", 90), ("Volume 1", 70)])

    async def test_an_item_in_no_series_is_an_entry_of_its_own(self):
        await self.item(1, title="Loose one")
        await self.record([{"item_id": 1, "page_index": 0, "seconds": 90, "ago": 0}])
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([(e["name"], e["opens"]) for e in entries], [("Loose one", False)])

    async def test_a_glance_is_not_listed_as_something_read(self):
        # Opened to look at, or tapped by accident: under a minute in that
        # series, and being the most recent thing is exactly what would
        # otherwise put it at the top of this list.
        await self.series(1)
        await self.series(2)
        await self.item(1, series_id=2, title="Glanced at")
        await self.item(2, series_id=1, title="Actually read")
        await self.record([
            {"item_id": 2, "page_index": 0, "seconds": 300, "ago": 600},
            {"item_id": 1, "page_index": 0, "seconds": 55, "ago": 0},
        ])
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([e["name"] for e in entries], ["S1"])

    async def test_the_floor_is_on_the_series_not_the_item(self):
        # A 20-second chapter in a series being read WAS read: it is
        # listed inside the series and counted in its total.
        await self.series(1)
        await self.item(1, series_id=1, title="Long")
        await self.item(2, series_id=1, title="Short")
        await self.record([
            {"item_id": 1, "page_index": 0, "seconds": 90, "ago": 600},
            {"item_id": 2, "page_index": 0, "seconds": 20, "ago": 0},
        ])
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual(entries[0]["seconds"], 110)
        self.assertEqual([i["title"] for i in entries[0]["items"]], ["Short", "Long"])

    async def test_several_short_visits_to_one_book_still_count(self):
        # A paginated reader posts a span per page turn.
        await self.series(1)
        await self.item(1, series_id=1, title="Read in bursts")
        await self.record([{"item_id": 1, "page_index": i, "seconds": 25, "ago": 0}
                           for i in range(3)])
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([(e["seconds"], e["pages"]) for e in entries], [(75, 3)])

    async def test_a_volume_read_for_under_a_minute_is_left_out_too(self):
        await self.novel_chapter(1, volume=1)
        await self.record([{"novel_id": 1, "chapter_id": 1, "page_index": 0, "seconds": 50, "ago": 0}])
        self.assertEqual(await queries.recent_items(self.db, 1), [])

    async def test_the_floor_is_on_counted_time_not_raw_time(self):
        # A tab left open all night is one long span the cap cuts down to
        # almost nothing -- the same thing the rest of the page counts.
        await self.series(1)
        await self.item(1, series_id=1, title="Left open")
        await self.record([{"item_id": 1, "page_index": 0, "seconds": 3600, "ago": 0}], cap=30)
        self.assertEqual(await queries.recent_items(self.db, 1), [])

    async def test_one_folder_of_two_kinds_is_one_entry_per_kind(self):
        # The item's own format, not its library's media_type: library 3 is
        # 'mixed' and holds both -- and the kind filter has to keep meaning
        # one kind, so the folder is split rather than labelled "either".
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type)"
            " VALUES (3, 'Both', '/x', 'mixed')")
        await self.series(1, library_id=3)
        await self.item(1, library_id=3, series_id=1, fmt="cbz")
        await self.item(2, library_id=3, series_id=1, fmt="epub")
        await self.novel_chapter(1, volume=1)
        await self.record([
            {"item_id": 1, "page_index": 0, "seconds": 90, "ago": 900},
            {"item_id": 2, "page_index": 0, "seconds": 90, "ago": 600},
            {"novel_id": 1, "chapter_id": 1, "page_index": 0, "seconds": 90, "ago": 300},
        ])
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([(e["name"], e["kind"]) for e in entries],
                         [("N", "novel"), ("S1", "epub"), ("S1", "cbz")])

    async def test_an_item_deleted_without_a_record_leaves_no_entry(self):
        # A span whose item went before removed items were kept -- or by a
        # raw DELETE, as here -- has no name left, and every such span
        # shares one NULL item_id: no nameless entry holding their sum.
        await self.series(1)
        await self.item(1, series_id=1)
        await self.record([{"item_id": 1, "page_index": 0, "seconds": 90, "ago": 0}])
        await self.db.execute("DELETE FROM items WHERE id = 1")
        await self.db.commit()
        self.assertEqual(await queries.recent_items(self.db, 1), [])

    async def test_the_limit_is_per_kind_so_a_filter_can_still_fill_the_list(self):
        # The whole reason the section's filter needs no refetch. Ten
        # comic series read more recently than any ebook would, under a
        # plain global LIMIT 8, leave the list with zero ebook entries --
        # so "Ebooks" would come up empty on a library that has them.
        limit = queries.RECENT_LIMIT
        spans = []
        for n in range(1, limit + 3):
            await self.series(n)
            await self.item(n, series_id=n, fmt="cbz", title=f"Comic {n}")
            spans.append({"item_id": n, "page_index": 0, "seconds": 90, "ago": 100 + n})
        for n in range(100, 100 + limit + 2):
            await self.series(n, library_id=2)
            await self.item(n, library_id=2, series_id=n, fmt="epub", title=f"Book {n}")
            spans.append({"item_id": n, "page_index": 0, "seconds": 90, "ago": 1000 + n})
        await self.record(spans)
        entries = await queries.recent_items(self.db, 1)

        by_kind = {}
        for entry in entries:
            by_kind.setdefault(entry["kind"], []).append(entry)
        self.assertEqual(len(by_kind["cbz"]), limit)
        self.assertEqual(len(by_kind["epub"]), limit)

        # And the unfiltered view -- the first `limit` entries of what is
        # sent -- is still the true global top `limit`, all comics here
        # since every comic was read after every book.
        self.assertTrue(all(e["kind"] == "cbz" for e in entries[:limit]))
        self.assertEqual([e["last_at"] for e in entries],
                         sorted((e["last_at"] for e in entries), reverse=True))

    async def test_a_kind_read_only_once_keeps_its_single_entry(self):
        # The partition is a cap, not a quota: nothing is padded.
        await self.series(1)
        await self.series(2, library_id=2)
        await self.item(1, series_id=1, fmt="cbz")
        await self.item(2, library_id=2, series_id=2, fmt="epub")
        await self.record([
            {"item_id": 1, "page_index": 0, "seconds": 90, "ago": 600},
            {"item_id": 2, "page_index": 0, "seconds": 90, "ago": 300},
        ])
        self.assertEqual(len(await queries.recent_items(self.db, 1)), 2)


class WindowResolution(unittest.TestCase):
    """What the query string means, before it reaches the database.

    Plain functions over dates, so they are tested without a connection --
    the same reasoning as queries.py living apart from the router.
    """

    def test_a_preset_is_the_last_n_days_ending_today(self):
        window = router._resolve_window(60, None, None)
        self.assertEqual(window["end"], date.today())
        self.assertEqual((window["end"] - window["start"]).days, 59)
        self.assertEqual(window["preset"], 60)
        self.assertEqual(window["label"], "Last 60 days")

    def test_no_parameters_is_the_default_window(self):
        window = router._resolve_window(None, None, None)
        self.assertEqual((window["end"] - window["start"]).days,
                         queries.DAILY_WINDOW_DAYS - 1)
        self.assertEqual(window["preset"], queries.DAILY_WINDOW_DAYS)

    def test_a_day_count_that_is_not_a_preset_highlights_nothing(self):
        # A hand-typed ?days=45 is answered, but no button claims it --
        # a highlighted "30 days" over a 45-day chart would be a lie.
        window = router._resolve_window(45, None, None)
        self.assertEqual((window["end"] - window["start"]).days, 44)
        self.assertIsNone(window["preset"])

    def test_an_explicit_range_wins_and_is_labelled_by_its_dates(self):
        window = router._resolve_window(90, "2026-01-05", "2026-02-03")
        self.assertEqual(window["start"], date(2026, 1, 5))
        self.assertEqual(window["end"], date(2026, 2, 3))
        self.assertIsNone(window["preset"])
        self.assertEqual(window["label"], "Jan 5, 2026 – Feb 3, 2026")

    def test_a_backwards_range_is_read_the_other_way_round(self):
        window = router._resolve_window(None, "2026-02-03", "2026-01-05")
        self.assertEqual((window["start"], window["end"]),
                         (date(2026, 1, 5), date(2026, 2, 3)))

    def test_a_range_running_into_the_future_ends_today(self):
        window = router._resolve_window(None, "2026-01-05", "2999-01-01")
        self.assertEqual(window["end"], date.today())

    def test_a_range_entirely_in_the_future_collapses_onto_today(self):
        window = router._resolve_window(None, "2998-01-01", "2999-01-01")
        self.assertEqual((window["start"], window["end"]), (date.today(), date.today()))

    def test_an_over_long_range_keeps_the_recent_end(self):
        # Cut at the start, not the end: the reader asked to see up to that
        # day, and dropping the recent half would answer a question about a
        # period they did not ask about at all.
        window = router._resolve_window(None, "1970-01-01", "2026-02-03")
        self.assertEqual(window["end"], date(2026, 2, 3))
        self.assertEqual((window["end"] - window["start"]).days,
                         queries.MAX_WINDOW_DAYS - 1)

    def test_a_day_count_is_capped_too(self):
        window = router._resolve_window(99999, None, None)
        self.assertEqual((window["end"] - window["start"]).days,
                         queries.MAX_WINDOW_DAYS - 1)

    def test_an_unparseable_date_is_refused_rather_than_answered(self):
        # The one input that is NOT clamped: there is no nearest sensible
        # range for "tomorrow" or "05/02/2026", so answering with some
        # other window would put a chart on screen that quietly disagrees
        # with the URL that asked for it.
        with self.assertRaises(HTTPException) as caught:
            router._resolve_window(None, "05/02/2026", "2026-02-03")
        self.assertEqual(caught.exception.status_code, 400)

    def test_one_open_end_defaults_to_today(self):
        window = router._resolve_window(None, "2026-01-05", None)
        self.assertEqual(window["end"], date.today())
        self.assertEqual(window["start"], date(2026, 1, 5))


class TheTwoKindsOfReading(unittest.TestCase):
    """A comic page is an image and a book page is prose. The tiles that
    would average them together are split, and the grouping is the one
    place that decides which side a format lands on."""

    def test_comics_and_text_are_separate_groups(self):
        rows = [
            {"kind": "cbz", "seconds": 100, "pages": 10},
            {"kind": "epub", "seconds": 600, "pages": 6},
            {"kind": "novel", "seconds": 300, "pages": 6},
        ]
        comics, text = router._grouped_per_page(rows)
        self.assertEqual(comics["label"], "Comics")
        self.assertEqual(comics["seconds_per_page"], 10)
        # Books and web novels together: both are text, and what differs
        # between them is markup overhead per word, not reading.
        self.assertEqual(text["pages"], 12)
        self.assertEqual(text["seconds_per_page"], 75)

    def test_a_group_with_nothing_in_it_still_gets_a_cell(self):
        # Rendered as "—" rather than vanishing, or the row goes lopsided
        # and stops saying which half is empty.
        comics, text = router._grouped_per_page([{"kind": "epub", "seconds": 60, "pages": 2}])
        self.assertEqual(comics["pages"], 0)
        self.assertEqual(comics["seconds_per_page"], 0)
        self.assertEqual(text["seconds_per_page"], 30)

    def test_a_deleted_items_time_lands_in_neither(self):
        # 'unknown' is a page_read whose item is gone. It still counts in
        # "Time read" and still shows in the per-kind chart; it is not
        # quietly folded into one of these two.
        comics, text = router._grouped_per_page([
            {"kind": "unknown", "seconds": 999, "pages": 9},
            {"kind": "cbz", "seconds": 100, "pages": 10},
        ])
        self.assertEqual(comics["pages"], 10)
        self.assertEqual(text["pages"], 0)


if __name__ == "__main__":
    unittest.main()
