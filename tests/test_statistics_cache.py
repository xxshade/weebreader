"""Prepared Statistics: committed generations, reader ownership and recovery."""
import asyncio
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import settings
from app.database import close_pool, get_connection, init_db, write_transaction
from app.stats import page_data, queries
from app.stats.page_cache import Period, ReaderDeferred, StatisticsCache, StatisticsReaderPriority


class StatisticsCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await close_pool()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = patch.object(settings, 'data_dir', Path(self.directory.name))
        self.path.start()
        self.addCleanup(self.path.stop)
        await init_db()
        self.db = await get_connection()
        await self.db.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'one','unused'),(2,'two','unused')")
        for uid, seconds, visit in [(1, 120, 'repeat'), (1, 3, 'repeat'), (1, 5, None), (2, 17, 'other')]:
            await self.db.execute("""INSERT INTO page_reads(user_id,kind,page_index,started_at,seconds,cap_seconds,visit_id)
                VALUES(?,'cbz',1,datetime('now'),?,20,?)""", (uid, seconds, visit))
        await self.db.commit()
        self.cache = StatisticsCache()
        self.cache.COALESCE_SECONDS = .001
        self.cache.READER_QUIET_SECONDS = .001

    async def asyncTearDown(self):
        await self.cache.close()
        await self.db.close()
        await close_pool()

    async def settle(self):
        while self.cache._worker is not None and not self.cache._worker.done():
            await asyncio.wait_for(asyncio.shield(self.cache._worker), 5)

    async def test_prepares_all_presets_accounts_and_mutable_results_are_private(self):
        await self.cache.start()
        await self.settle()
        with patch.object(page_data, 'load', side_effect=AssertionError('warm cache queried history')):
            for uid in (1, 2):
                for days in (30, 60, 90):
                    _, data = await self.cache.get(uid, days)
                    self.assertEqual(data['seconds_per_page_by_kind'][0]['seconds'], 28 if uid == 1 else 17)
                    self.assertEqual(data['seconds_per_page_by_kind'][0]['pages'], 2 if uid == 1 else 1)
                    data['seconds_per_page_by_kind'][0]['seconds'] = -999
        self.assertEqual(len(self.cache.periods), 6)
        self.assertLessEqual(self.cache.bytes, self.cache.MAX_BYTES)

    async def test_external_commit_refreshes_other_periods_and_preserves_query_contracts(self):
        await self.cache.start()
        await self.settle()
        # Direct committed write bypasses application notifications.
        await self.db.execute("INSERT INTO page_reads(user_id,kind,page_index,started_at,seconds,cap_seconds) VALUES(1,'epub',1,datetime('now'),29,180)")
        await self.db.commit()
        _, data = await self.cache.get(1)
        self.assertEqual(data['text_pace']['words'], 0)
        self.assertEqual(sum(day['seconds'] for day in data['daily_activity']), 57)
        await self.settle()
        with patch.object(page_data, 'load', side_effect=AssertionError('other period was not rebuilt')):
            await self.cache.get(1, 90)
        totals = await queries.reading_totals(self.db, 1)
        self.assertEqual(totals['seconds'], 57)
        self.assertEqual(totals['pages'], 3)
        self.assertEqual(await page_data.reading_overview(self.db, 1), {'days': 1, 'pages': 1})
        self.assertEqual({v['kind'] for v in await queries.seconds_per_page_by_kind(self.db, 1)}, {'epub', 'cbz'})

    async def test_account_recreation_with_identical_fields_forgets_only_old_interests(self):
        await self.cache.start()
        await self.cache.get(1, start='2020-01-01', end='2020-01-02', partial=True)
        await self.settle()
        generation = (await (await self.db.execute('SELECT incarnation FROM stats_account_generations WHERE user_id=1')).fetchone())[0]
        row = await (await self.db.execute('SELECT * FROM users WHERE id=1')).fetchone()
        columns = row.keys()
        await self.db.execute('DELETE FROM users WHERE id=1')
        await self.db.execute('INSERT INTO users('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')', tuple(row))
        await self.db.commit()
        _, data = await self.cache.get(1)
        self.assertEqual(data['reading_totals']['pages'], 0)
        self.assertFalse(any(p.user_id == 1 and not p.preset for p in self.cache.periods))
        current = (await (await self.db.execute('SELECT incarnation FROM stats_account_generations WHERE user_id=1')).fetchone())[0]
        self.assertNotEqual(generation, current)
        self.assertEqual((await self.cache.get(2))[1]['seconds_per_page_by_kind'][0]['seconds'], 17)

    async def test_idle_calendar_change_rebuilds_presets_and_customs_without_a_page_request(self):
        await self.cache.start()
        await self.cache.get(1, days=7, partial=True)
        await self.settle()
        tomorrow = date.today() + timedelta(days=1)
        import app.stats.windows as windows
        class Tomorrow(date):
            @classmethod
            def today(cls):
                return tomorrow
        with patch.object(page_data, 'calendar_token', return_value=('new-day', 'new-zone')), patch.object(windows, 'date', Tomorrow):
            await self.cache.check_calendar()
            await self.settle()
            with patch.object(page_data, 'load', side_effect=AssertionError('calendar was not prepared')):
                window, _ = await self.cache.get(1, 90)
                self.assertEqual(window['end'], tomorrow)
                await self.cache.get(1, 7, partial=True)

    async def test_readers_preempt_only_background_and_completed_queries_survive(self):
        await self.cache.start()
        await self.settle()
        self.cache._invalidate()
        import threading
        original = page_data.load
        arrived = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def wait_sql():
            loop.call_soon_threadsafe(arrived.set)
            release.wait(3)
            return 1
        await self.cache._connection.create_function('statistics_guard_wait', 0, wait_sql)
        async def held(db, name, *args):
            if name == 'text_pace':
                await db.execute('WITH RECURSIVE n(x) AS (VALUES(statistics_guard_wait()) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n')
            return await original(db, name, *args)
        period = Period(1)
        with patch.object(page_data, 'load', side_effect=held):
            task = asyncio.create_task(self.cache._snapshot(period, epoch=self.cache._reader_epoch))
            await asyncio.wait_for(arrived.wait(), 2)
            self.cache.reader_enter()
            release.set()
            with self.assertRaises(ReaderDeferred):
                await task
            self.cache.reader_leave()
        self.assertTrue(any(key[0] == 'daily_activity' for key in self.cache.entries))
        self.assertFalse(self.cache._connection.in_transaction)
        # Reader arrival must never interrupt foreground SQL using that connection.
        arrived.clear(); release.clear()
        with patch.object(page_data, 'load', side_effect=held):
            task = asyncio.create_task(self.cache._snapshot(Period(1)))
            await asyncio.wait_for(arrived.wait(), 2)
            self.cache.reader_enter(); release.set()
            connection = self.cache._connection
            try:
                result = await task
            except Exception as error:
                self.fail('Foreground SQL must not be interrupted: ' + str(error))
            finally:
                self.cache.reader_leave()
            self.assertIs(self.cache._connection, connection, 'Foreground query was interrupted and retried')
            self.assertIn('text_pace', result[1])

    async def test_commit_during_interrupted_snapshot_discards_completed_queries(self):
        await self.cache.start()
        await self.settle()
        self.cache._invalidate()
        original = page_data.load
        arrived, release = asyncio.Event(), asyncio.Event()
        async def held(db, name, *args):
            if name == 'text_pace':
                arrived.set(); await release.wait()
            return await original(db, name, *args)
        with patch.object(page_data, 'load', side_effect=held):
            task = asyncio.create_task(self.cache._snapshot(Period(1), epoch=self.cache._reader_epoch))
            await asyncio.wait_for(arrived.wait(), 2)
            await self.db.execute("INSERT INTO page_reads(user_id,kind,page_index,started_at,seconds,cap_seconds) VALUES(1,'cbz',2,datetime('now'),37,180)")
            await self.db.commit()
            self.cache.reader_enter(); release.set()
            with self.assertRaises(ReaderDeferred):
                await task
            self.cache.reader_leave()
        self.assertFalse(self.cache.entries)
        self.assertEqual((await self.cache.get(1))[1]['seconds_per_page_by_kind'][0]['seconds'], 65)

    async def test_cancelled_setup_and_failed_validation_drop_connection_and_recover(self):
        await self.cache.get(1)
        for method, error in [('execute', asyncio.CancelledError()), ('rollback', RuntimeError('validation fault'))]:
            connection = self.cache._connection
            with patch.object(connection, method, side_effect=error):
                with self.assertRaises(type(error)):
                    await self.cache.get(1)
            self.assertIsNone(self.cache._connection)
            _, data = await self.cache.get(1)
            self.assertEqual(data['seconds_per_page_by_kind'][0]['seconds'], 28)
            self.assertFalse(self.cache._connection.in_transaction)

    async def test_restart_forgets_custom_periods_and_rebuilds_restored_history(self):
        await self.cache.start()
        await self.cache.get(1, days=7)
        await self.settle()
        await self.cache.close()
        await self.db.execute('DELETE FROM page_reads WHERE user_id=1')
        await self.db.commit()
        await self.cache.start()
        await self.settle()
        self.assertTrue(all(p.preset for p in self.cache.periods))
        with patch.object(page_data, 'load', side_effect=AssertionError('restart not prepared')):
            self.assertEqual((await self.cache.get(1))[1]['reading_totals']['pages'], 0)

    async def test_budget_overflow_returns_correct_data_and_invalid_input_retains_nothing(self):
        self.cache.MAX_BYTES = 1
        _, data = await self.cache.get(1)
        self.assertEqual(data['seconds_per_page_by_kind'][0]['seconds'], 28)
        self.assertFalse(self.cache.entries)
        periods = list(self.cache.periods)
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            await self.cache.get(1, start='not-a-date')
        self.assertEqual(list(self.cache.periods), periods)
        for days in range(100, 120):
            await self.cache.get(1, days, partial=True)
        self.assertEqual(sum(not p.preset for p in self.cache.periods), self.cache.MAX_CUSTOM_PERIODS)

    async def test_application_commits_notify_only_after_writer_lock_released(self):
        from app.stats.page_cache import cache
        from app.database import _write_lock
        observed = []
        with patch.object(cache, 'notify', side_effect=lambda: observed.append(_write_lock.locked())):
            async with write_transaction() as db:
                await db.execute("UPDATE users SET username='renamed' WHERE id=1")
                await db.commit()
        self.assertEqual(observed, [False])

    async def test_stream_lifetime_and_failure_release_reader_priority(self):
        from app.stats.page_cache import cache
        started, finish = asyncio.Event(), asyncio.Event()
        async def streaming(scope, receive, send):
            started.set(); await finish.wait(); raise RuntimeError('stream disconnected')
        middleware = StatisticsReaderPriority(streaming)
        task = asyncio.create_task(middleware({'type': 'http', 'path': '/reader/1/page/0'}, None, None))
        await started.wait()
        self.assertEqual(cache._reader_count, 1)
        self.assertFalse(cache._idle.is_set())
        finish.set()
        with self.assertRaises(RuntimeError):await task
        self.assertEqual(cache._reader_count, 0)
        self.assertTrue(cache._idle.is_set())
        self.assertFalse(middleware.is_reader('/library/events'))

    async def test_path_switch_after_reader_failure_forgets_old_custom_interests(self):
        import sqlite3
        await self.cache.get(1, days=7)
        await self.cache._drop_connection()
        other = Path(self.directory.name) / 'restored'
        other.mkdir()
        from contextlib import closing
        with closing(sqlite3.connect(other / 'library.db')) as target:
            await self.db.backup(target)
        with patch.object(settings, 'data_dir', other):
            _, data = await self.cache.get(1)
        self.assertEqual(data['seconds_per_page_by_kind'][0]['seconds'], 28)
        self.assertTrue(all(p.preset for p in self.cache.periods))

    async def test_generation_backfill_is_idempotent_and_private_reader_cannot_write(self):
        await self.db.executescript('DROP TRIGGER stats_account_insert; DROP TRIGGER stats_account_delete; DROP TABLE stats_account_generations;')
        await self.db.commit()
        await init_db()
        before = [tuple(row) for row in await (await self.db.execute('SELECT * FROM stats_account_generations ORDER BY user_id')).fetchall()]
        await init_db()
        after = [tuple(row) for row in await (await self.db.execute('SELECT * FROM stats_account_generations ORDER BY user_id')).fetchall()]
        self.assertEqual([row[0] for row in after], [1, 2])
        self.assertEqual(before, after)
        await self.cache.get(1)
        import sqlite3
        with self.assertRaises(sqlite3.OperationalError):
            await self.cache._connection.execute("UPDATE users SET username='forbidden' WHERE id=1")
