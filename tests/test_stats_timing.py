"""Stats diagnostics measure real boundaries without changing page identity."""
import asyncio
import unittest
from unittest.mock import patch

from app.config import settings
from app.stats import page_data
from app.stats.page_cache import cache
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
        boundaries = page_data.PHASES
        await cache.close()
        original = page_data.load
        async def delayed(*args, **kwargs):
            await asyncio.sleep(.002)
            return await original(*args, **kwargs)
        with patch.object(page_data, 'load', side_effect=delayed) as load:
            detailed = await self.client.get('/stats')
            self.assertEqual(load.call_count, len(boundaries))
            load.reset_mock()
            cached = await self.client.get('/stats', headers={'If-None-Match': normal.headers['etag']})
            self.assertEqual(load.call_count, 0)
        self.assertEqual(detailed.content, normal.content)
        self.assertEqual(detailed.headers['etag'], normal.headers['etag'])
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b'')
        self.assertEqual(cached.headers['cache-control'], 'private, no-cache')
        for response in (detailed, cached):
            values = metrics(response)
            self.assertEqual(set(values), {'app', 'db_acquire', 'auth', 'render',
                'revalidate', 'stats_other', 'stats_finish', *boundaries.values()})
            if response is detailed:
                for phase in boundaries.values():
                    self.assertGreaterEqual(values[phase], 2, phase)
            self.assertEqual(values['stats_finish'], 0)
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
