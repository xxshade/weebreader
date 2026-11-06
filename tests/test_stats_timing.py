"""Stats diagnostics measure real boundaries without changing page identity."""
import asyncio
import unittest
from unittest.mock import patch

from app.config import settings
from app.stats import queries
from tests import test_detailed_timing as home_timing

metrics = home_timing.metrics


class StatsPhases(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = home_timing.HomePhases.asyncSetUp
    asyncTearDown = home_timing.HomePhases.asyncTearDown

    async def test_stats_boundaries_default_off_and_cache_identity(self):
        with patch('app.request_timing.perf_counter', side_effect=AssertionError('disabled clock')):
            normal = await self.client.get('/stats')
        self.assertEqual(normal.status_code, 200)
        self.assertEqual(set(metrics(normal)), {'app'})
        settings.detailed_server_timing = True
        boundaries = {
            'daily_activity': 'stats_daily',
            'seconds_per_page_by_kind': 'stats_by_kind',
            'text_pace': 'stats_pace',
            'recent_items': 'stats_recent',
            'series_by_status': 'stats_series_status',
            'items_by_status': 'stats_item_status',
            'novel_totals': 'stats_novels',
            'library_totals': 'stats_libraries',
            'reading_totals': 'stats_totals',
            'time_to_finish': 'stats_finish',
        }
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in boundaries:
                original = getattr(queries, name)
                async def delayed(*args, _original=original, **kwargs):
                    await asyncio.sleep(.002)
                    return await _original(*args, **kwargs)
                stack.enter_context(patch.object(queries, name, side_effect=delayed))
            detailed = await self.client.get('/stats')
            cached = await self.client.get('/stats', headers={'If-None-Match': normal.headers['etag']})
        self.assertEqual(detailed.content, normal.content)
        self.assertEqual(detailed.headers['etag'], normal.headers['etag'])
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b'')
        self.assertEqual(cached.headers['cache-control'], 'private, no-cache')
        for response in (detailed, cached):
            values = metrics(response)
            self.assertEqual(set(values), {'app', 'db_acquire', 'auth', 'render',
                'revalidate', 'stats_other', *boundaries.values()})
            for phase in boundaries.values():
                self.assertGreaterEqual(values[phase], 2, phase)
            self.assertGreater(values['render'], 0)
            self.assertTrue(all(v >= 0 for v in values.values()))
            self.assertAlmostEqual(values['app'], sum(v for k, v in values.items() if k != 'app'), delta=.02)
        partial = await self.client.get('/stats/daily-activity')
        self.assertEqual(set(metrics(partial)), {'app'})


class StatsIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_stats_scope_isolated_from_home_and_reset_on_error(self):
        from app import request_timing as timing
        entered = asyncio.Event()
        leave = asyncio.Event()

        async def stats():
            with self.assertRaises(RuntimeError):
                with timing.collect(True, stats=True):
                    entered.set()
                    await leave.wait()
                    with timing.stats_phase('stats_totals'):
                        pass
                    self.assertTrue(any(v.startswith('stats_totals;') for v in timing.headers(10)))
                    raise RuntimeError('fixture')
            self.assertEqual(timing.headers(0), [])
            with timing.collect(True):
                self.assertFalse(any(v.startswith('stats_') for v in timing.headers(10)))

        async def home():
            await entered.wait()
            with timing.collect(True):
                with patch('app.request_timing.perf_counter', side_effect=AssertionError('leaked stats scope')):
                    with timing.stats_phase('stats_totals'):
                        pass
                self.assertFalse(any(v.startswith('stats_') for v in timing.headers(10)))
            leave.set()

        await asyncio.gather(stats(), home())
