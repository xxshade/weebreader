"""Opt-in home phases are isolated per request and do not alter cache identity."""
import asyncio
from contextvars import ContextVar
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from app import request_timing as timing
from app.auth.service import hash_token
from app.config import settings
from app.database import close_pool, get_connection, init_db
from app.main import app


def metrics(response):
    return {name: float(duration.removeprefix('dur=')) for name, duration in
            (part.strip().split(';') for part in response.headers['server-timing'].split(','))}


class HomePhases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await close_pool()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = patch.object(settings, 'data_dir', Path(self.directory.name))
        self.path.start()
        self.addCleanup(self.path.stop)
        self.flag = patch.object(settings, 'detailed_server_timing', False)
        self.flag.start()
        self.addCleanup(self.flag.stop)
        await init_db()
        db = await get_connection()
        try:
            await db.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'probe','not-a-password')")
            await db.execute("INSERT INTO sessions(user_id,token_hash,expires_at,totp_verified) VALUES(1,?,'2999-01-01T00:00:00+00:00',1)", (hash_token('timing-fixture'),))
            await db.commit()
        finally:
            await db.close()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                       headers={'Cookie': 'session=timing-fixture'})

    async def asyncTearDown(self):
        await self.client.aclose()
        await close_pool()

    async def test_opt_in_home_phases_preserve_200_304_and_default_output(self):
        with patch('app.request_timing.perf_counter', side_effect=AssertionError('disabled clock read')):
            normal = await self.client.get('/')
        self.assertEqual(set(metrics(normal)), {'app'})
        settings.detailed_server_timing = True
        detailed = await self.client.get('/')
        phase = metrics(detailed)
        self.assertEqual(set(phase), {'app', 'db_acquire', 'auth', 'home_data', 'render', 'revalidate', 'other',
                    'home_queries', 'home_estimator', 'home_series', 'home_novels',
                    'home_tracking', 'home_cards', 'home_reading', 'home_other',
                    'series_query', 'series_prepare', 'series_aggregate', 'series_other'})
        for key in ('db_acquire', 'auth', 'home_data', 'render', 'revalidate',
                    'home_queries', 'home_estimator', 'home_series', 'home_novels',
                    'home_tracking', 'home_cards', 'home_reading', 'home_other'):
            self.assertGreater(phase[key], 0, key)
        self.assertEqual(normal.content, detailed.content)
        self.assertEqual(normal.headers['etag'], detailed.headers['etag'])
        cached = await self.client.get('/', headers={'If-None-Match': detailed.headers['etag']})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b'')
        self.assertEqual(cached.headers['etag'], detailed.headers['etag'])
        self.assertEqual(cached.headers['cache-control'], 'private, no-cache')
        for response in (detailed, cached):
            values = metrics(response)
            self.assertTrue(all(value >= 0 for value in values.values()))
            self.assertAlmostEqual(values['home_series'], sum(value for key, value in values.items() if key.startswith('series_')), delta=0.01)
            self.assertAlmostEqual(values['home_data'], sum(value for key, value in values.items() if key.startswith('home_') and key != 'home_data'), delta=0.01)
            self.assertAlmostEqual(values['app'], sum(value for key, value in values.items() if key in ('db_acquire', 'auth', 'home_data', 'render', 'revalidate', 'other')), delta=0.01)
        other_page = await self.client.get('/settings')
        self.assertEqual(set(metrics(other_page)), {'app'})
        self.assertEqual(timing.headers(0), [], 'Request context must be reset after every response')


class PhaseIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_tasks_have_separate_phases_and_exception_cleanup(self):
        clock = ContextVar('test_clock', default=0)
        ready = [asyncio.Event(), asyncio.Event()]

        async def measure(index, name, elapsed):
            with timing.collect(True):
                clock.set(1)
                started = timing.start()
                ready[index].set()
                await ready[1-index].wait()
                clock.set(1 + elapsed)
                timing.finish(name, started)
                return timing.headers(elapsed * 1000)

        with patch('app.request_timing.perf_counter', side_effect=lambda: clock.get()):
            first, second = await asyncio.gather(measure(0, 'auth', .05), measure(1, 'render', .09))
        self.assertIn('auth;dur=50.000', first)
        self.assertIn('render;dur=0.000', first)
        self.assertIn('render;dur=90.000', second)
        self.assertIn('auth;dur=0.000', second)
        with self.assertRaises(RuntimeError):
            with timing.collect(True):
                raise RuntimeError('fixture')
        self.assertEqual(timing.headers(100), [])

    async def test_nested_phases_are_not_counted_twice_in_residuals(self):
        with timing.collect(True):
            with patch('app.request_timing.perf_counter', return_value=1.0):
                parent = timing.start()
                child = timing.start()
            for name, begin, end in (('series_query', 1.0, 1.005),
                                     ('series_prepare', 1.005, 1.015),
                                     ('series_aggregate', 1.015, 1.020)):
                with patch('app.request_timing.perf_counter', return_value=begin):
                    grandchild = timing.start()
                with patch('app.request_timing.perf_counter', return_value=end):
                    timing.finish(name, grandchild)
            with patch('app.request_timing.perf_counter', return_value=1.025):
                timing.finish('home_series', child)
            with patch('app.request_timing.perf_counter', return_value=1.1):
                timing.finish('home_data', parent)
            values = dict(entry.split(';dur=') for entry in timing.headers(150))
            self.assertEqual(values['series_query'], '5.000')
            self.assertEqual(values['series_prepare'], '10.000')
            self.assertEqual(values['series_aggregate'], '5.000')
            self.assertEqual(values['series_other'], '5.000')
            self.assertEqual(values['home_series'], '25.000')
            self.assertEqual(values['home_data'], '100.000')
            self.assertEqual(values['home_other'], '75.000')
            self.assertEqual(values['other'], '50.000')

    async def test_disabled_phases_do_not_read_the_clock(self):
        with patch('app.request_timing.perf_counter', side_effect=AssertionError('disabled clock read')):
            with timing.collect(False):
                self.assertIsNone(timing.start())
                timing.finish('auth', None)
                self.assertEqual(timing.headers(100), [])
