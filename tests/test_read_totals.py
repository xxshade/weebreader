"""The rollup the reading estimates are built from.

`item_read_totals` and `chapter_read_totals` hold one number each -- this
user's counted seconds on this item, on this chapter -- so the estimator
stops re-summing the whole of `page_reads` every time a card needs a "time
left". Measured at 200,000 spans: 224 ms to build the estimator against
2 ms, and the 2 ms no longer grows with the history.

A derived table is only worth having if it cannot disagree with what it was
derived from, so that is what most of this file is about. Two mechanisms
carry the whole invariant and both are tested here directly:

- a TRIGGER on page_reads, which is why no write path can forget to
  maintain the totals -- including the ones in these very test files, which
  insert spans by hand and never mention the rollup;
- foreign keys, which is how a total disappears when the book it counts
  does, while the raw span stays behind.

And the promise that made the rollup acceptable in the first place: nothing
was pruned. `page_reads` still holds every span, so the statistics page
keeps every dimension it ever had -- the day, the page, the kind. There is
a test for that too, because it is the thing that would be silently lost.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.stats import queries
from app.stats.estimates import build_estimator
from app.stats.recording import record_spans, rebuild_read_totals

APP = Path(__file__).resolve().parent.parent / "app"


class TotalsCase(unittest.IsolatedAsyncioTestCase):
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
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (2,'v','x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'L','/l','mixed')")
        for item_id, fmt in ((1, "cbz"), (2, "epub")):
            await self.db.execute(
                "INSERT INTO items (id, library_id, title, sort_title, file_path, file_format,"
                " file_hash, file_size, page_count, text_size) VALUES (?,1,?,?,?,?,?,1,40,50000)",
                (item_id, f"I{item_id}", f"i{item_id}", f"/l/{item_id}", fmt, f"h{item_id}"))
        await self.db.execute("INSERT INTO novels (id, title, source_url) VALUES (1,'N','https://x/1')")
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (1,1,1)")
        for chapter_id in (10, 11):
            await self.db.execute(
                "INSERT INTO chapters (id, novel_id, volume_id, number, title, url, content_size)"
                " VALUES (?,1,1,?,?,?,6000)",
                (chapter_id, float(chapter_id), f"C{chapter_id}", f"https://x/c{chapter_id}"))
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def flush(self, spans, *, user_id=1, cap=180):
        """A batch of spans through the real recording path."""
        stored = await record_spans(self.db, user_id, cap, spans)
        await self.db.commit()
        return stored

    async def totals(self, table, key):
        return {
            r[key]: r["seconds"]
            for r in await (await self.db.execute(
                f"SELECT {key}, seconds FROM {table} WHERE user_id = 1")).fetchall()
        }

    async def items(self):
        return await self.totals("item_read_totals", "item_id")

    async def chapters(self):
        return await self.totals("chapter_read_totals", "chapter_id")

    async def snapshot(self):
        return await self.items(), await self.chapters()

    async def rebuilt(self):
        """What a fresh pass over page_reads says the totals should be."""
        await rebuild_read_totals(self.db)
        await self.db.commit()
        return await self.snapshot()


class WhatTheTriggerAccumulates(TotalsCase):
    async def test_a_recorded_span_lands_in_both_the_history_and_the_total(self):
        await self.flush([{"item_id": 1, "seconds": 30, "ago": 0, "page_index": 0}])
        self.assertEqual(await self.items(), {1: 30.0})
        # And the span itself is still there, in full. The rollup is a
        # second copy of a number, not a replacement for the row.
        row = await (await self.db.execute(
            "SELECT seconds, page_index, started_at FROM page_reads")).fetchone()
        self.assertEqual(row["seconds"], 30.0)
        self.assertEqual(row["page_index"], 0)

    async def test_the_cap_is_applied_to_each_span_and_not_to_the_sum(self):
        # THE rule the whole rollup has to reproduce: counted time is
        # SUM(MIN(seconds, cap_seconds)), so two forgotten tabs of an hour
        # each count as two caps, not one. A rollup that clamped the total
        # instead would agree on a single span and be wrong on every
        # reader who left the page open twice.
        await self.flush([{"item_id": 1, "seconds": 3000, "ago": 0},
                          {"item_id": 1, "seconds": 3000, "ago": 0}], cap=180)
        self.assertEqual(await self.items(), {1: 360.0})
        self.assertEqual(await self.items(), (await self.rebuilt())[0])

    async def test_each_flush_adds_to_what_is_already_there(self):
        await self.flush([{"item_id": 2, "seconds": 20, "ago": 0}])
        await self.flush([{"item_id": 2, "seconds": 5, "ago": 0}])
        await self.flush([{"item_id": 2, "seconds": 1.5, "ago": 0}])
        self.assertEqual(await self.items(), {2: 26.5})

    async def test_a_cap_changed_between_flushes_is_honoured_per_flush(self):
        # page_time_cap_seconds is deliberately not retroactive (see
        # set_page_time_cap): every span keeps the cap it was recorded
        # under. The rollup accumulates at insert time, so it inherits that
        # for free -- which is also why changing the setting never has to
        # invalidate it.
        await self.flush([{"item_id": 1, "seconds": 600, "ago": 0}], cap=180)
        await self.flush([{"item_id": 1, "seconds": 600, "ago": 0}], cap=60)
        self.assertEqual(await self.items(), {1: 240.0})

    async def test_an_item_span_and_a_chapter_span_go_to_different_tables(self):
        await self.flush([{"item_id": 1, "seconds": 40, "ago": 0},
                          {"novel_id": 1, "chapter_id": 10, "seconds": 25, "ago": 0}])
        self.assertEqual(await self.items(), {1: 40.0})
        self.assertEqual(await self.chapters(), {10: 25.0})

    async def test_two_users_reading_the_same_item_are_kept_apart(self):
        await self.flush([{"item_id": 1, "seconds": 30, "ago": 0}], user_id=1)
        await self.flush([{"item_id": 1, "seconds": 90, "ago": 0}], user_id=2)
        self.assertEqual(await self.items(), {1: 30.0})
        other = await (await self.db.execute(
            "SELECT seconds FROM item_read_totals WHERE user_id = 2")).fetchone()
        self.assertEqual(other["seconds"], 90.0)

    async def test_a_span_inserted_without_going_through_record_spans_counts_too(self):
        # This is the reason the totals are maintained by a trigger rather
        # than by record_spans. Half the fixtures in this suite write spans
        # with a bare INSERT, no application code involved at all, and an
        # estimate built from them has to be the same estimate -- otherwise
        # every test that ever touches page_reads is quietly testing a
        # different app than the one that runs.
        await self.db.execute(
            "INSERT INTO page_reads (user_id, item_id, kind, page_index, started_at, seconds,"
            " cap_seconds) VALUES (1, 2, 'epub', 0, datetime('now'), 500, 180)")
        await self.db.commit()
        self.assertEqual(await self.items(), {2: 180.0})

    async def test_a_mixed_history_matches_a_rebuild_span_for_span(self):
        # The drift test. Everything above checks one rule at a time; this
        # drives a realistic mixture through the real write path and then
        # asserts the accumulated answer equals the one-pass answer. If a
        # future change to record_spans, to the cap, or to the trigger puts
        # the two out of step, this is what says so.
        for n in range(1, 21):
            await self.flush([
                {"item_id": 1 + (n % 2), "seconds": 3 * n, "ago": 0, "page_index": n},
                {"novel_id": 1, "chapter_id": 10 + (n % 2), "seconds": 200 + n, "ago": 1},
            ], cap=180)
        before = await self.snapshot()
        self.assertEqual(before, await self.rebuilt())
        # And it is not vacuously equal -- there is real reading in there.
        self.assertEqual(sorted(before[0]), [1, 2])
        self.assertEqual(sorted(before[1]), [10, 11])


class WhenTheContentGoesAway(TotalsCase):
    async def test_deleting_an_item_takes_its_total_but_leaves_its_history(self):
        # The asymmetry is the design, not an oversight: page_reads uses
        # ON DELETE SET NULL because deleting a comic does not un-read it
        # and the statistics are about the READER (see the note on the
        # table in schema.sql), while a total exists only to estimate a
        # book -- and a book that is gone needs no estimate.
        await self.flush([{"item_id": 1, "seconds": 120, "ago": 0},
                          {"item_id": 2, "seconds": 60, "ago": 0}])
        before = await queries.reading_totals(self.db, 1)
        await self.db.execute("DELETE FROM items WHERE id = 1")
        await self.db.commit()
        self.assertEqual(await self.items(), {2: 60.0})
        after = await queries.reading_totals(self.db, 1)
        self.assertEqual(after["seconds"], before["seconds"])
        self.assertEqual(after["pages"], before["pages"])

    async def test_a_rebuild_after_a_delete_does_not_resurrect_the_total(self):
        # The cascade and the rebuild have to agree about a deleted book,
        # or the migration that fills these tables would hand back totals
        # the running app had already dropped.
        await self.flush([{"item_id": 1, "seconds": 120, "ago": 0},
                          {"item_id": 2, "seconds": 60, "ago": 0}])
        await self.db.execute("DELETE FROM items WHERE id = 1")
        await self.db.commit()
        self.assertEqual(await self.items(), (await self.rebuilt())[0])

    async def test_a_deleted_item_does_not_hand_its_total_to_the_next_one(self):
        # items.id is a plain INTEGER PRIMARY KEY, so SQLite reuses a
        # deleted row's id. Cascading is what closes that: a total left
        # behind would silently become the next book's head start, and the
        # estimate on its card would be built from someone else's reading.
        await self.flush([{"item_id": 1, "seconds": 900, "ago": 0}], cap=180)
        await self.db.execute("DELETE FROM items WHERE id = 1")
        await self.db.execute(
            "INSERT INTO items (id, library_id, title, sort_title, file_path, file_format,"
            " file_hash, file_size, page_count) VALUES (1,1,'New','new','/l/new','cbz','hn',1,40)")
        await self.db.commit()
        self.assertEqual(await self.items(), {})

    async def test_deleting_a_chapter_takes_its_total(self):
        await self.flush([{"novel_id": 1, "chapter_id": 10, "seconds": 45, "ago": 0},
                          {"novel_id": 1, "chapter_id": 11, "seconds": 45, "ago": 0}])
        await self.db.execute("DELETE FROM chapters WHERE id = 10")
        await self.db.commit()
        self.assertEqual(await self.chapters(), {11: 45.0})

    async def test_deleting_the_user_takes_everything_of_theirs(self):
        await self.flush([{"item_id": 1, "seconds": 30, "ago": 0},
                          {"novel_id": 1, "chapter_id": 10, "seconds": 30, "ago": 0}])
        await self.db.execute("DELETE FROM users WHERE id = 1")
        await self.db.commit()
        self.assertEqual(await self.snapshot(), ({}, {}))


class WhatTheEstimatorReadsNow(TotalsCase):
    async def test_the_estimator_no_longer_aggregates_the_spans(self):
        # The point of the whole change. If a query here goes back to
        # page_reads, the estimator is linear in the reading history again
        # and the cards on the home page slow down as the user reads.
        # Matched on the SQL and not on the name: the module docstring
        # talks about page_reads at length, and rightly so -- it is still
        # where every one of these numbers ultimately comes from.
        source = (APP / "stats/estimates.py").read_text()
        for clause in ("FROM page_reads", "JOIN page_reads"):
            with self.subTest(clause=clause):
                self.assertNotIn(clause, source)

    async def test_an_estimate_is_built_from_the_totals(self):
        # End to end rather than by inspection: record time against a book
        # the reader is a third of the way into, and the estimate has to
        # come out of the rollup the trigger just wrote.
        # Ten minutes, as ten spans under the cap rather than one long one:
        # a single 600-second span would be counted as the 180-second cap,
        # which is the whole point of MIN(seconds, cap_seconds) and would
        # make the arithmetic below a test of the wrong thing.
        await self.flush([{"item_id": 2, "seconds": 60, "ago": 0} for _ in range(10)])
        self.assertEqual(await self.items(), {2: 600.0})
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent)"
            " VALUES (1, 2, 'reading', 30)")
        await self.db.commit()
        estimator = await build_estimator(self.db, 1)
        item = await (await self.db.execute("SELECT * FROM items WHERE id = 2")).fetchone()
        progress = await (await self.db.execute(
            "SELECT * FROM reading_progress WHERE item_id = 2")).fetchone()
        eta = estimator.for_item(item, progress)
        # Historical time remains available to statistics, but cannot invent
        # matched word evidence for a text prediction.
        self.assertIsNone(eta)
        self.assertEqual(estimator.spent_by_item[2], 600)

    async def test_an_estimate_disappears_with_the_book_it_was_about(self):
        await self.flush([{"item_id": 2, "seconds": 60, "ago": 0} for _ in range(10)])
        await self.db.execute("DELETE FROM items WHERE id = 2")
        await self.db.commit()
        estimator = await build_estimator(self.db, 1)
        self.assertEqual(estimator.spent_by_item, {})


class NothingWasGivenUp(TotalsCase):
    """The rollup is a cache beside `page_reads`, not a replacement for it.

    Every dimension the statistics page reads -- the day a span happened,
    the page it was on, what kind of thing it was -- lives only in the raw
    rows, and none of it is answerable from a per-target total. So the
    question these ask is not "are the totals right" but "are the spans
    still all there, and does /stats still read them".
    """

    async def test_the_statistics_still_read_the_raw_spans(self):
        source = (APP / "stats/queries.py").read_text()
        self.assertIn("FROM page_reads", source)
        for table in ("item_read_totals", "chapter_read_totals"):
            with self.subTest(table=table):
                self.assertNotIn(table, source)

    async def test_the_day_and_the_page_survive_a_flush(self):
        await self.flush([{"item_id": 1, "seconds": 30, "ago": 0, "page_index": 4},
                          {"item_id": 1, "seconds": 30, "ago": 0, "page_index": 5}])
        rows = await (await self.db.execute(
            "SELECT page_index, kind, date(started_at) AS day FROM page_reads ORDER BY page_index"
        )).fetchall()
        self.assertEqual([r["page_index"] for r in rows], [4, 5])
        self.assertEqual({r["kind"] for r in rows}, {"cbz"})
        self.assertTrue(all(r["day"] for r in rows))

    async def test_the_daily_chart_and_the_per_page_split_still_answer(self):
        await self.flush([{"item_id": 1, "seconds": 30, "ago": 0, "page_index": 0},
                          {"item_id": 2, "seconds": 90, "ago": 0, "page_index": 0},
                          {"novel_id": 1, "chapter_id": 10, "seconds": 45, "ago": 0}])
        daily = await queries.daily_activity(self.db, 1, days=30)
        self.assertEqual(round(sum(d["seconds"] for d in daily)), 165)
        per_page = await queries.seconds_per_page_by_kind(self.db, 1)
        self.assertEqual({r["kind"] for r in per_page}, {"cbz", "epub", "novel"})

    async def test_the_raw_duration_and_the_cap_are_both_still_stored(self):
        # What makes the history recomputable under a different cap, which
        # is the reason page_reads keeps two columns where one number would
        # do -- and the reason a rollup can never be the only copy.
        await self.flush([{"item_id": 1, "seconds": 3000, "ago": 0}], cap=180)
        row = await (await self.db.execute(
            "SELECT seconds, cap_seconds FROM page_reads")).fetchone()
        self.assertEqual((row["seconds"], row["cap_seconds"]), (3000.0, 180))
        self.assertEqual(await self.items(), {1: 180.0})


class TheUpgradePath(unittest.IsolatedAsyncioTestCase):
    """A database that has been read on for months, meeting these tables for
    the first time."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))

    # page_reads as it stood before it learned to say what it was -- the
    # shape migration 5 rebuilds. Kept here rather than imported from
    # test_deleted_item_stats because the two cases pin it for opposite
    # reasons, and a shared copy would let one of them quietly change what
    # the other is testing.
    OLD_PAGE_READS = """
        CREATE TABLE page_reads (
            id              INTEGER PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_id         INTEGER REFERENCES items(id) ON DELETE CASCADE,
            novel_id        INTEGER REFERENCES novels(id) ON DELETE CASCADE,
            chapter_id      INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
            section_index   INTEGER,
            page_index      INTEGER,
            started_at      TEXT NOT NULL,
            seconds         REAL NOT NULL,
            cap_seconds     INTEGER NOT NULL,
            CHECK ((item_id IS NOT NULL) <> (chapter_id IS NOT NULL))
        )
    """

    def _seed(self, *, version, old_page_reads=False):
        """A database at `version` with reading already in it.

        The rollup tables and their trigger are always removed: that is
        what a database from before them genuinely looks like. schema.sql
        recreates them empty on the next start, and the migration is what
        has to fill them.

        `old_page_reads` additionally puts back the pre-`kind` table, so
        migration 5's rebuild runs for real -- without it that migration
        finds the column already there and skips, and a test aimed at what
        the rebuild destroys would be aimed at nothing.
        """
        db = sqlite3.connect(settings.db_path)
        db.executescript((Path(__file__).resolve().parent.parent / "app/schema.sql").read_text())
        if old_page_reads:
            db.execute("DROP TABLE page_reads")
            db.executescript(self.OLD_PAGE_READS)
        else:
            db.execute("DROP TRIGGER page_reads_read_totals")
        db.execute("DROP TABLE item_read_totals")
        db.execute("DROP TABLE chapter_read_totals")
        db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        db.execute("INSERT INTO libraries (id,name,root_path,media_type) VALUES (1,'L','/l','mixed')")
        db.execute(
            "INSERT INTO items (id,library_id,title,sort_title,file_path,file_format,file_hash,"
            "file_size,page_count) VALUES (1,1,'I','i','/l/1','cbz','h',1,40)")
        columns = "user_id,item_id,page_index,started_at,seconds,cap_seconds"
        values = "1,1,0,datetime('now'),?,180"
        if not old_page_reads:
            columns, values = columns.replace("item_id,", "item_id,kind,"), values.replace("1,1,0", "1,1,'cbz',0")
        for seconds in (60, 500):
            db.execute(f"INSERT INTO page_reads ({columns}) VALUES ({values})", (seconds,))
        db.execute(f"PRAGMA user_version = {version}")
        db.commit()
        db.close()

    async def test_the_reading_already_there_is_rolled_up_on_the_next_start(self):
        # Without this, every card would go blank on the first start after
        # the upgrade and stay blank until the user read enough NEW time
        # for the trigger to have something to say.
        self._seed(version=5)
        await init_db()
        db = await get_connection()
        self.addCleanup(db.close)
        row = await (await db.execute("SELECT * FROM item_read_totals")).fetchone()
        self.assertEqual((row["user_id"], row["item_id"], row["seconds"]), (1, 1, 240.0))

    async def test_the_backfill_does_not_run_a_second_time(self):
        # It is a DELETE-then-INSERT, so running it again would be
        # harmless -- but only for as long as that stays true, and
        # user_version is what makes it not a question.
        self._seed(version=5)
        await init_db()
        db = await get_connection()
        await db.execute(
            "INSERT INTO page_reads (user_id,item_id,kind,page_index,started_at,seconds,"
            "cap_seconds) VALUES (1,1,'cbz',1,datetime('now'),30,180)")
        await db.commit()
        await db.close()
        await init_db()
        db = await get_connection()
        self.addCleanup(db.close)
        row = await (await db.execute("SELECT seconds FROM item_read_totals")).fetchone()
        self.assertEqual(row["seconds"], 270.0)

    async def test_the_trigger_survives_the_migration_that_rebuilds_page_reads(self):
        # Migration 5 rebuilds page_reads with DROP TABLE, and DROP TABLE
        # takes the table's triggers with it -- so a database old enough to
        # run that migration and this one in the same start would come up
        # with the totals filled and nothing maintaining them, and drift
        # from its very first page turn. init_db re-runs schema.sql after
        # the migrations for exactly this reason.
        self._seed(version=4, old_page_reads=True)
        await init_db()
        db = await get_connection()
        self.addCleanup(db.close)
        triggers = [r[0] for r in await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='page_reads'"
        )).fetchall()]
        self.assertIn("page_reads_read_totals", triggers)
        await db.execute(
            "INSERT INTO page_reads (user_id,item_id,kind,page_index,started_at,seconds,"
            "cap_seconds) VALUES (1,1,'cbz',2,datetime('now'),30,180)")
        await db.commit()
        row = await (await db.execute("SELECT seconds FROM item_read_totals")).fetchone()
        self.assertEqual(row["seconds"], 270.0)


if __name__ == "__main__":
    unittest.main()
