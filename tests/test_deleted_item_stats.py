"""Deleting a file does not un-read it.

`page_reads` used to cascade with its item, so removing a volume from the
library deleted the record that it had ever been read -- and not only from
the totals: the daily chart for the day it was read shrank, months later,
with nothing on screen saying why. Measured before the change, on the real
deletion path: deleting one of two items took 3 of 6 rows and half the
counted seconds with it.

The links are ON DELETE SET NULL now and the row carries its own `kind`,
so it survives its content and still knows what it was.

And the item itself is remembered as a REMOVED ITEM (GLOSSARY.md,
docs/adr/0001-remember-removed-items.md): its name and series, its
completion verdict and its size, for "Most recent", "start to finish" and
the reading rate. Its reading status on the library side still goes with
it -- a removed item is not part of the library.
"""
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.library import scanner
from app.stats import estimates, queries
from app.stats.recording import record_spans


class DeletedItemCase(unittest.IsolatedAsyncioTestCase):
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
            "INSERT INTO libraries (id,name,root_path,media_type) VALUES (1,'L','/m','comic')")
        await self.db.execute(
            "INSERT INTO series (id,library_id,name,sort_name,folder_path) VALUES (1,1,'S','s','S')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def item(self, item_id, *, fmt="cbz", series_id=1):
        await self.db.execute(
            "INSERT INTO items (id,library_id,series_id,title,sort_title,file_path,file_format,"
            "file_hash,file_size,text_size) VALUES (?,1,?,?,?,?,?,?,1,5000)",
            (item_id, series_id, f"Item {item_id}", f"item {item_id}",
             f"/x/{item_id}.{fmt}", fmt, f"h{item_id}"))
        await self.db.commit()

    async def read(self, item_id, *, pages=3, seconds=120, days_ago=0):
        for page in range(pages):
            await self.db.execute(
                "INSERT INTO page_reads (user_id,item_id,kind,page_index,started_at,seconds,cap_seconds)"
                " VALUES (1,?,(SELECT file_format FROM items WHERE id = ?),?,"
                " datetime('now','localtime',?),?,300)",
                (item_id, item_id, page, f"-{days_ago} days", seconds))
        await self.db.commit()

    async def finished(self, item_id, *, from_percent=100):
        await self.db.execute(
            "INSERT INTO reading_progress (user_id,item_id,status,progress_percent,completed_at,"
            "completed_from_percent) VALUES (1,?,'completed',100,datetime('now'),?)",
            (item_id, from_percent))
        await self.db.commit()

    async def scalar(self, sql, params=()):
        return (await (await self.db.execute(sql, params)).fetchone())[0]

    # ---------- what survives ----------

    async def test_the_time_survives_the_item(self):
        await self.item(1)
        await self.item(2)
        await self.read(1)
        await self.read(2)
        before = await queries.reading_totals(self.db, 1)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        after = await queries.reading_totals(self.db, 1)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM items"), 1)
        self.assertEqual(after["seconds"], before["seconds"])
        self.assertEqual(after["pages"], before["pages"])

    async def test_the_orphan_row_still_says_what_it_was(self):
        await self.item(1, fmt="cbz")
        await self.item(2, fmt="epub")
        await self.read(1)
        await self.read(2)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        by_kind = {r["kind"]: r for r in await queries.seconds_per_page_by_kind(self.db, 1)}
        # Not lumped into the surviving book's bucket, and not lost.
        self.assertEqual(sorted(by_kind), ["cbz", "epub"])
        self.assertEqual(by_kind["cbz"]["seconds"], 360)
        self.assertIsNone(await self.scalar("SELECT item_id FROM page_reads WHERE kind = 'cbz'"))

    async def test_a_past_day_keeps_what_was_read_on_it(self):
        # The complaint this whole change exists for: a chart of a day
        # months ago must not shrink because a file was tidied up today.
        await self.item(1)
        await self.read(1, days_ago=3)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        daily = await queries.daily_activity(self.db, 1, days=7)
        day = (date.today() - timedelta(days=3)).isoformat()
        self.assertEqual([d["seconds"] for d in daily if d["day"] == day], [360])

    async def test_deleting_a_novel_chapter_keeps_its_time_too(self):
        await self.db.execute(
            "INSERT INTO novels (id,title,source_url) VALUES (1,'N','http://x')")
        await self.db.execute("INSERT INTO volumes (id,novel_id,number) VALUES (1,1,1)")
        await self.db.execute(
            "INSERT INTO chapters (id,novel_id,volume_id,title,url,order_index)"
            " VALUES (1,1,1,'C','http://x/1',0)")
        await self.db.execute(
            "INSERT INTO page_reads (user_id,novel_id,chapter_id,kind,page_index,started_at,"
            "seconds,cap_seconds) VALUES (1,1,1,'novel',0,datetime('now'),90,300)")
        await self.db.commit()
        await self.db.execute("DELETE FROM chapters WHERE id = 1")
        await self.db.commit()
        totals = await queries.reading_totals(self.db, 1)
        self.assertEqual(totals["seconds"], 90)
        by_kind = {r["kind"]: r for r in await queries.seconds_per_page_by_kind(self.db, 1)}
        self.assertEqual(list(by_kind), ["novel"])

    # ---------- what deliberately does not ----------

    async def test_the_reading_status_still_goes_with_the_item(self):
        # Progress is about the item -- where you are IN it -- so it has
        # nothing to be about once the item is gone.
        await self.item(1)
        await self.read(1)
        await self.finished(1)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM reading_progress"), 0)

    async def test_how_long_a_volume_takes_keeps_the_removed_one(self):
        await self.item(1)
        await self.item(2)
        for i in (1, 2):
            await self.read(i)
            await self.finished(i)
        before = await queries.time_to_finish(self.db, 1)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        after = await queries.time_to_finish(self.db, 1)
        self.assertEqual(after["per_comic"], before["per_comic"])
        self.assertEqual(after["per_comic"]["n"], 2)
        self.assertEqual(after["per_book"]["n"], 0)

    async def test_a_declared_finish_stays_out_after_removal_too(self):
        # The same 90% rule as for an item still there: marked read at 12%
        # took the time of 12% of a volume.
        await self.item(1)
        await self.read(1)
        await self.finished(1, from_percent=12)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        self.assertEqual((await queries.time_to_finish(self.db, 1))["per_comic"]["n"], 0)

    async def test_most_recent_keeps_the_removed_item_in_its_series(self):
        await self.item(1)
        await self.item(2)
        await self.read(1, days_ago=1)
        await self.read(2)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([(e["name"], e["seconds"]) for e in entries], [("S", 720)])
        self.assertEqual([(i["title"], i["removed"]) for i in entries[0]["items"]],
                         [("Item 2", False), ("Item 1", True)])

    async def test_a_series_gone_with_its_last_item_keeps_its_name(self):
        await self.item(1)
        await self.read(1)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM series"), 0)
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([e["name"] for e in entries], ["S"])
        self.assertEqual(entries[0]["items"][0]["removed"], True)

    async def test_the_folder_coming_back_is_the_same_entry(self):
        # A series IS its folder: the row that comes back has a new id,
        # and the card is still one card, under the name it has now.
        await self.item(1)
        await self.read(1, days_ago=1)
        await scanner._delete_item(self.db, 1)
        await self.db.execute(
            "INSERT INTO series (id,library_id,name,sort_name,folder_path) VALUES (7,1,'S again','s','S')")
        await self.db.commit()
        await self.item(2, series_id=7)
        await self.read(2)
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([(e["name"], len(e["items"])) for e in entries], [("S again", 2)])

    async def test_put_back_and_read_again_is_listed_twice(self):
        # A file put back is a new item; the reading before is its own
        # line, marked removed.
        await self.item(1)
        await self.item(3)  # keeps the series row through the deletion
        await self.read(1, days_ago=1)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        await self.item(2)
        await self.db.execute("UPDATE items SET title = 'Item 1' WHERE id = 2")
        await self.db.commit()
        await self.read(2)
        entries = await queries.recent_items(self.db, 1)
        self.assertEqual([(i["title"], i["removed"]) for i in entries[0]["items"]],
                         [("Item 1", False), ("Item 1", True)])

    async def test_an_item_nobody_read_leaves_nothing_behind(self):
        await self.item(1)
        await self.finished(1)  # declared, never opened
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM removed_items"), 0)

    async def test_a_removed_item_is_not_part_of_the_library(self):
        await self.item(1)
        await self.item(2)
        await self.read(1)
        await self.finished(1)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        by_status = {r["status"]: r["n"] for r in await queries.items_by_status(self.db, 1)}
        self.assertEqual(by_status, {"unread": 1})
        totals = await queries.library_totals(self.db)
        self.assertEqual(totals[0]["item_count"], 1)

    async def test_the_reading_rate_still_learns_from_it(self):
        # Read v01, delete it: v02, unread, is estimated from v01's pace.
        await self.item(1)
        await self.item(2)
        await self.db.execute("UPDATE items SET page_count = 20")
        await self.db.commit()
        await self.read(1, pages=20, seconds=30)
        await self.finished(1)
        before = await estimates.build_estimator(self.db, 1)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        after = await estimates.build_estimator(self.db, 1)
        self.assertEqual(after.rate_by_series, before.rate_by_series)
        self.assertAlmostEqual(after.rate_by_series[1], 30.0)

    async def test_a_whole_series_read_and_deleted_still_feeds_the_fallback(self):
        # Every unread comic series borrows the 95th percentile of the
        # reader's series rates. Read series S, delete all of it: without
        # its rate in that pool, an unread series B had no estimate at all.
        await self.item(1)
        await self.db.execute("UPDATE items SET page_count = 20 WHERE id = 1")
        await self.db.execute(
            "INSERT INTO series (id,library_id,name,sort_name,folder_path) VALUES (2,1,'B','b','B')")
        await self.db.commit()
        await self.item(2, series_id=2)
        await self.db.execute("UPDATE items SET page_count = 10 WHERE id = 2")
        await self.db.commit()
        await self.read(1, pages=20, seconds=30)
        await self.finished(1)
        before = await estimates.build_estimator(self.db, 1)
        await scanner._delete_item(self.db, 1)
        await self.db.commit()
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM series WHERE id = 1"), 0)
        after = await estimates.build_estimator(self.db, 1)
        self.assertEqual(after.rate_by_series, {})
        self.assertAlmostEqual(after.comic_fallback_rate, before.comic_fallback_rate)
        unread = await (await self.db.execute("SELECT * FROM items WHERE id = 2")).fetchone()
        self.assertAlmostEqual(after.for_item(unread, None).remaining, 300)

    # ---------- the write side ----------

    async def test_recording_stamps_the_kind_from_the_item(self):
        await self.item(1, fmt="epub")
        stored = await record_spans(self.db, 1, 180, [
            {"item_id": 1, "section_index": 0, "page_index": 2, "seconds": 30, "ago": 1},
        ])
        await self.db.commit()
        self.assertEqual(stored, 1)
        self.assertEqual(await self.scalar("SELECT kind FROM page_reads"), "epub")

    async def test_a_row_can_never_name_two_things(self):
        # A REAL chapter, so the foreign key is satisfied and the CHECK is
        # the only thing left that can refuse the row. Without it this
        # test passes on the foreign key instead and would stay green with
        # the constraint gone -- which is what the mutation run caught.
        await self.item(1)
        await self.db.execute(
            "INSERT INTO novels (id,title,source_url) VALUES (1,'N','http://x')")
        await self.db.execute("INSERT INTO volumes (id,novel_id,number) VALUES (1,1,1)")
        await self.db.execute(
            "INSERT INTO chapters (id,novel_id,volume_id,title,url,order_index)"
            " VALUES (1,1,1,'C','http://x/1',0)")
        await self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute(
                "INSERT INTO page_reads (user_id,item_id,chapter_id,kind,started_at,seconds,cap_seconds)"
                " VALUES (1,1,1,'cbz',datetime('now'),10,300)")


class MigrationCase(unittest.IsolatedAsyncioTestCase):
    """The rebuild of page_reads, run against a database of the old shape.

    A migration that cannot be tested against the thing it migrates is a
    hope, so this builds the pre-change table by hand -- cascading foreign
    keys, the old CHECK, no `kind` -- and then lets init_db upgrade it.
    """
    OLD_TABLE = """
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

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))

    def _build_old_database(self):
        db = sqlite3.connect(settings.db_path)
        schema = (Path(__file__).resolve().parent.parent / "app/schema.sql").read_text()
        db.executescript(schema)
        db.execute("DROP TABLE page_reads")
        db.executescript(self.OLD_TABLE)
        db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        db.execute("INSERT INTO libraries (id,name,root_path,media_type) VALUES (1,'L','/m','comic')")
        for item_id, fmt in ((1, "cbz"), (2, "epub")):
            db.execute(
                "INSERT INTO items (id,library_id,title,sort_title,file_path,file_format,"
                "file_hash,file_size) VALUES (?,1,?,?,?,?,?,1)",
                (item_id, f"I{item_id}", f"i{item_id}", f"/x/{item_id}", fmt, f"h{item_id}"))
            db.execute(
                "INSERT INTO page_reads (user_id,item_id,page_index,started_at,seconds,cap_seconds)"
                " VALUES (1,?,0,datetime('now'),60,300)", (item_id,))
        # A row whose item is ALREADY gone: what a database looks like
        # after a delete on a connection with foreign keys off.
        db.execute(
            "INSERT INTO page_reads (user_id,item_id,page_index,started_at,seconds,cap_seconds)"
            " VALUES (1,404,0,datetime('now'),60,300)")
        # The version just BEFORE the rebuild this case is about, pinned
        # rather than written as SCHEMA_VERSION - 1: that spelling means
        # "one before whatever the newest migration is", so the next
        # migration added silently moved this database past the one under
        # test and the rebuild stopped running at all.
        db.execute("PRAGMA user_version = 4")
        db.commit()
        db.close()

    async def test_the_rows_come_through_with_the_kind_they_had(self):
        self._build_old_database()
        await init_db()
        db = await get_connection()
        self.addCleanup(db.close)
        rows = {
            r["id"]: r["kind"]
            for r in await (await db.execute("SELECT id, kind FROM page_reads ORDER BY id")).fetchall()
        }
        self.assertEqual(list(rows.values()), ["cbz", "epub", "unknown"])
        totals = await queries.reading_totals(db, 1)
        self.assertEqual(totals["pages"], 3)

    async def test_the_upgraded_table_no_longer_cascades(self):
        self._build_old_database()
        await init_db()
        db = await get_connection()
        self.addCleanup(db.close)
        await db.execute("DELETE FROM items WHERE id = 1")
        await db.commit()
        self.assertEqual(
            (await (await db.execute("SELECT COUNT(*) FROM page_reads")).fetchone())[0], 3)

    async def test_the_index_survives_the_rebuild(self):
        self._build_old_database()
        await init_db()
        db = await get_connection()
        self.addCleanup(db.close)
        names = [
            r[0] for r in await (await db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='page_reads'"
            )).fetchall()
        ]
        self.assertIn("idx_page_reads_user_time", names)

    async def test_it_does_not_run_twice(self):
        # The second start finds `kind` already there and leaves the table
        # alone -- a rebuild that ran again would be harmless here but the
        # check is what keeps it from being re-derived from a join whose
        # items have since been deleted.
        self._build_old_database()
        await init_db()
        db = await get_connection()
        await db.execute("DELETE FROM items WHERE id = 1")
        await db.commit()
        await db.close()
        await init_db()
        db = await get_connection()
        self.addCleanup(db.close)
        kinds = [r[0] for r in await (await db.execute(
            "SELECT kind FROM page_reads ORDER BY id")).fetchall()]
        self.assertEqual(kinds, ["cbz", "epub", "unknown"])


if __name__ == "__main__":
    unittest.main()
