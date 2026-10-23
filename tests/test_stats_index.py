"""The statistics page reads the whole history from one covering index.

With one user, `user_id` filters nothing, so an index that does not carry a
query's columns is worse than none: every row is a seek back into the table.
The per-kind split, the totals and "Most recent" did exactly that -- the
planner even chose the chapter index for the per-kind split -- and /stats
took 1493 ms on 200,000 spans; 534 ms with idx_page_reads_user_reads.
"""
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.stats import queries
from tests import test_read_totals



class Recording:
    """A connection that remembers every statement it ran, so the plans
    checked below are the ones stats/queries.py actually issues."""

    def __init__(self, db):
        self.db, self.statements = db, []

    async def execute(self, sql, params=()):
        self.statements.append((sql, params))
        return await self.db.execute(sql, params)


class IndexCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addAsyncCleanup(self.db.close)


class TheStatisticsQueries(IndexCase):
    async def test_each_reads_page_reads_from_an_index_alone(self):
        for name, run in (("per kind", queries.seconds_per_page_by_kind),
                          ("totals", queries.reading_totals),
                          ("most recent", queries.recent_items)):
            recording = Recording(self.db)
            await run(recording, 1)
            spans = [(sql, params) for sql, params in recording.statements
                     if "FROM page_reads" in sql]
            self.assertTrue(spans, name)
            for sql, params in spans:
                steps = [row[3] for row in await (await self.db.execute(
                    "EXPLAIN QUERY PLAN " + sql, params)).fetchall()]
                reads = [step for step in steps if " page_reads " in f"{step} "]
                self.assertTrue(reads, f"{name}: {steps}")
                for step in reads:
                    self.assertIn("USING COVERING INDEX idx_page_reads_user_", step, f"{name}: {steps}")


class AnExistingDatabase(IndexCase):
    async def test_loses_the_indexes_these_replaced(self):
        await self.db.execute(
            "CREATE INDEX idx_page_reads_user_item ON page_reads(user_id, item_id, seconds, cap_seconds)")
        await self.db.execute(
            "CREATE INDEX idx_page_reads_user_chapter ON page_reads(user_id, chapter_id, seconds, cap_seconds)")
        await self.db.commit()
        await init_db()
        names = {row[0] for row in await (await self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'page_reads'")).fetchall()}
        self.assertNotIn("idx_page_reads_user_item", names)
        self.assertNotIn("idx_page_reads_user_chapter", names)
        self.assertIn("idx_page_reads_user_reads", names)
        self.assertIn("idx_page_reads_user_chapter_reads", names)


class ADatabaseOlderThanKind(unittest.IsolatedAsyncioTestCase):
    """page_reads learned `kind` in migration 5, and schema.sql runs once
    BEFORE the migrations -- with the index in it, that first pass stopped
    at `no such column: kind` and an old database never started."""

    # test_read_totals' old database, not its tests.
    asyncSetUp = test_read_totals.TheUpgradePath.asyncSetUp
    OLD_PAGE_READS = test_read_totals.TheUpgradePath.OLD_PAGE_READS
    _seed = test_read_totals.TheUpgradePath._seed

    async def test_starts_and_gets_the_index(self):
        self._seed(version=4, old_page_reads=True)
        await init_db()
        db = await get_connection()
        self.addAsyncCleanup(db.close)
        names = {row[0] for row in await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'page_reads'")).fetchall()}
        self.assertIn("idx_page_reads_user_reads", names)


if __name__ == "__main__":
    unittest.main()
