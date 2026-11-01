"""Optional API phases follow the real dependency/route/header lifecycle."""
import asyncio
from contextvars import ContextVar
from unittest.mock import patch

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

from app import request_timing as timing
from app.auth import dependencies
from app.auth.service import hash_token
from app.config import settings
from app.database import close_pool
from app.library import router
from app.main import app, revalidate_pages
from app.stats import ready_estimates as ready
from app.stats.estimates import build_estimator
from tests import test_estimates as fixtures, test_word_estimates as words
from tests.test_detailed_timing import metrics


TARGETS = {'targets': [{'kind': 'series', 'id': 1}, {'kind': 'item', 'id': 1}]}
ROOT_PHASES = ('api_auth', 'api_slot', 'api_db', 'api_input', 'api_estimator',
               'api_cache', 'api_fallback', 'api_encode', 'api_flush')


class EstimateApiTiming(fixtures.EstimateCase):
    book = words.WordEstimates.book
    evidence = words.WordEstimates.evidence

    async def asyncSetUp(self):
        await close_pool()
        await super().asyncSetUp()
        self.addAsyncCleanup(close_pool)
        await ready.cache.stop()
        self.addAsyncCleanup(ready.cache.stop)
        slots = patch.object(router, '_estimate_slots', asyncio.Semaphore(1))
        slots.start()
        self.addCleanup(slots.stop)
        self.clock = [1.0]
        flag = patch.object(settings, 'detailed_server_timing', False)
        flag.start()
        self.addCleanup(flag.stop)
        await self.db.execute("INSERT INTO sessions(user_id,token_hash,expires_at,totp_verified) VALUES(1,?,'2999-01-01T00:00:00+00:00',1)", (hash_token('api-timing-fixture'),))
        await self.db.execute("INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(1,1,'Series','series','/series')")
        await self.book(1)
        await self.db.execute('UPDATE items SET series_id=1 WHERE id=1')
        await self.db.commit()
        await self.evidence()
        estimator = await build_estimator(self.db, 1)
        await estimator.prepare_items([await self.row('SELECT * FROM items WHERE id=1')])
        ready.cache.enabled = True
        ready.cache._current_path()
        await ready.cache.get(self.db, 1, await build_estimator(self.db, 1, cached_only=True))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                       headers={'Cookie': 'session=api-timing-fixture'})
        self.addAsyncCleanup(self.client.aclose)

    async def request(self, **kwargs):
        return await self.client.post('/library/reading-estimates', json=TARGETS, **kwargs)

    async def test_opt_in_preserves_json_and_exposes_only_api_phases(self):
        with patch('app.request_timing.perf_counter', side_effect=AssertionError('disabled clock read')):
            normal = await self.request()
        self.assertNotIn('server-timing', normal.headers)
        settings.detailed_server_timing = True
        response = await self.request()
        self.assertIn('server-timing', response.headers, 'Enabled API diagnostics must be present')
        phase = metrics(response)
        self.assertEqual(response.content, normal.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(phase), {'app', *ROOT_PHASES, 'api_other', 'api_validate', 'api_targets',
                                    'api_cache_other', 'api_files', 'api_index', 'api_revisions', 'api_validate_other'})
        for name in ('api_auth', 'api_db', 'api_input', 'api_estimator', 'api_cache',
                     'api_validate', 'api_targets', 'api_files', 'api_index', 'api_revisions', 'api_encode'):
            self.assertGreater(phase[name], 0, name)
        self.assertEqual(phase['api_fallback'], 0)
        self.assertAlmostEqual(phase['app'], sum(phase[name] for name in ROOT_PHASES)+phase['api_other'], delta=.02)
        self.assertAlmostEqual(phase['api_cache'], phase['api_validate']+phase['api_targets']+phase['api_cache_other'], delta=.01)
        self.assertAlmostEqual(phase['api_validate'], sum(phase[name] for name in ('api_files','api_index','api_revisions','api_validate_other')), delta=.01)
        self.assertNotIn('etag', response.headers)
        self.assertNotIn('cache-control', response.headers)

    async def clocked_request(self):
        with patch('app.request_timing.perf_counter', side_effect=lambda: self.clock[0]), \
             patch('app.main.perf_counter', side_effect=lambda: self.clock[0]):
            return await self.request()

    async def test_disabled_or_unrelated_requests_cannot_enable_api_diagnostics(self):
        with patch('app.request_timing.perf_counter', side_effect=AssertionError('disabled clock read')):
            response = await self.request(params={'detailed_server_timing':'true'}, headers={'X-Detailed-Server-Timing':'true'})
        self.assertNotIn('server-timing', response.headers)
        settings.detailed_server_timing = True
        for method, path in (('GET','/library/reading-estimates'), ('POST','/not-an-estimate-route')):
            response = await self.client.request(method, path)
            self.assertNotIn('server-timing', response.headers)

    async def test_native_fallback_and_handled_errors_have_truthful_phases(self):
        ready.cache.enabled = False
        normal = await self.request()
        settings.detailed_server_timing = True
        response = await self.request()
        self.assertEqual(normal.content, response.content)
        values = metrics(response)
        self.assertGreater(values['api_fallback'], 0)
        self.assertEqual(values['api_validate'], 0)
        self.assertEqual(values['api_targets'], 0)
        for name, body in (('invalid_json', b'{'), ('empty_targets', b'{"targets":[]}')):
            with self.subTest(case=name):
                response = await self.client.post('/library/reading-estimates', content=body)
                self.assertEqual(response.status_code, 400)
                values = metrics(response)
                self.assertGreater(values['api_input'], 0)
                self.assertEqual(values['api_estimator'], 0)
                self.assertEqual(values['api_flush'], 0)
        self.client.headers.pop('cookie')
        response = await self.request()
        self.assertEqual(response.status_code, 401)
        values = metrics(response)
        self.assertGreater(values['api_auth'], 0)
        self.assertEqual(values['api_db'], 0)
        self.assertEqual(values['api_input'], 0)

    async def test_top_level_phase_boundaries_use_the_actual_call_sites(self):
        settings.detailed_server_timing = True
        original_auth = dependencies.get_current_user
        original_db = router.get_connection
        original_input = router.json_object
        original_estimator = router.build_estimator
        original_render = timing.JSONResponse.render
        async def auth(*args, **kwargs):
            value = await original_auth(*args, **kwargs)
            self.clock[0] += .020
            return value
        async def connection(*args, **kwargs):
            value = await original_db(*args, **kwargs)
            self.clock[0] += .015
            return value
        def parse(*args, **kwargs):
            value = original_input(*args, **kwargs)
            self.clock[0] += .010
            return value
        async def estimator(*args, **kwargs):
            value = await original_estimator(*args, **kwargs)
            self.clock[0] += .025
            return value
        def render(instance, content):
            value = original_render(instance, content)
            if isinstance(instance, timing.EstimateJSONResponse):
                self.clock[0] += .012
            return value
        for owner, name, replacement, phase, expected in (
                (dependencies,'get_current_user',auth,'api_auth',20),
                (router,'get_connection',connection,'api_db',15),
                (router,'json_object',parse,'api_input',10),
                (router,'build_estimator',estimator,'api_estimator',25),
                (timing.JSONResponse,'render',render,'api_encode',12)):
            with self.subTest(phase=phase), patch.object(owner,name,replacement):
                self.clock[0] = 1.0
                values = metrics(await self.clocked_request())
                self.assertEqual(values[phase], expected, 'Wrong top-level timing boundary: '+phase)
                self.assertEqual(values['app'], expected)
                self.assertEqual(values['api_other'], 0)

    async def test_cache_children_account_for_files_index_revisions_and_targets(self):
        settings.detailed_server_timing = True
        original_files = ready._stat_prepared_files
        original_version = ready.snapshots.version
        original_revisions = ready._revisions
        def files(rows):
            value = original_files(rows)
            self.clock[0] += .040
            return value
        def version():
            value = original_version()
            self.clock[0] += .030
            return value
        async def revisions(*args, **kwargs):
            value = await original_revisions(*args, **kwargs)
            self.clock[0] += .020
            return value
        for owner, name, replacement, phase in (
                (ready,'_stat_prepared_files',files,'api_files'),
                (ready.snapshots,'version',version,'api_index'),
                (ready,'_revisions',revisions,'api_revisions')):
            with self.subTest(phase=phase), patch.object(owner,name,replacement):
                self.clock[0] = 1.0
                values = metrics(await self.clocked_request())
                self.assertGreaterEqual(values[phase], 20, 'Missing cache child: '+phase)
                self.assertEqual(values[phase], values['api_validate'])
                self.assertEqual(values[phase], values['api_cache'])
                self.assertEqual(values[phase], values['app'])
                self.assertEqual(values['api_validate_other'], 0)
                self.assertEqual(values['api_cache_other'], 0)
        clock = self.clock
        class SeriesLookup(dict):
            def get(self, *args, **kwargs):
                clock[0] += .020
                return super().get(*args, **kwargs)
        state = ready.cache.states[1]
        state.series = SeriesLookup(state.series)
        self.clock[0] = 1.0
        values = metrics(await self.clocked_request())
        self.assertEqual(values['api_targets'], 20, 'Target lookup must be separate from cache validation')
        self.assertEqual(values['api_validate'], 0)
        self.assertEqual(values['app'], 20)
        self.clock[0] = 1.0
        with timing.collect(True,api=True), patch('app.request_timing.perf_counter',side_effect=lambda:self.clock[0]):
            with timing.api_phase('api_cache'):
                self.clock[0] += .005
                with timing.api_phase('api_validate'):
                    self.clock[0] += .003
                    with timing.api_phase('api_files'):
                        self.clock[0] += .040
            values = dict(entry.split(';dur=') for entry in timing.headers(58))
            self.assertEqual(float(values['api_other']), 10)
            self.assertEqual(float(values['api_cache_other']), 5)
            self.assertEqual(float(values['api_validate_other']), 3)

    async def test_real_semaphore_wait_is_outside_opening_and_cache_validation(self):
        settings.detailed_server_timing = True
        slots = router._estimate_slots
        await slots.acquire()
        task = asyncio.create_task(self.clocked_request())
        try:
            async def queued():
                while not slots._waiters:
                    await asyncio.sleep(.001)
            await asyncio.wait_for(queued(), 2)
            self.assertTrue(slots._waiters, 'Request must wait for the estimate connection slot')
            self.clock[0] += .030
        finally:
            slots.release()
        values = metrics(await task)
        self.assertEqual(values['api_slot'], 30)
        self.assertEqual(values['api_db'], 0)
        self.assertEqual(values['api_validate'], 0)
        self.assertEqual(values['app'], 30)

    async def test_pending_flush_delays_headers_outside_the_route(self):
        settings.detailed_server_timing = True
        entered, release = asyncio.Event(), asyncio.Event()
        previous = ready.cache.task
        pending = asyncio.create_task(release.wait())
        ready.cache.task = pending
        original = ready.cache.flush
        async def flush():
            entered.set()
            await original()
        try:
            with patch.object(ready.cache,'flush',flush):
                request = asyncio.create_task(self.clocked_request())
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    self.clock[0] += .050
                finally:
                    release.set()
                values = metrics(await request)
            self.assertEqual(values['api_flush'], 50, 'Pending maintenance must be included before headers')
            self.assertEqual(values['app'], 50)
            self.assertEqual(values['api_cache'], 0)
            self.assertEqual(values['api_other'], 0)
        finally:
            release.set()
            await pending
            ready.cache.task = previous

    async def test_cleanup_is_excluded_after_success_but_can_precede_error_headers(self):
        settings.detailed_server_timing = True
        original = router.get_connection
        closed = []
        clock = self.clock
        class Connection:
            def __init__(self, db):
                self.db = db
            def __getattr__(self, name):
                return getattr(self.db, name)
            async def close(self):
                clock[0] += .040
                closed.append(True)
                await self.db.close()
        async def connection():
            return Connection(await original())
        with patch.object(router,'get_connection',connection):
            response = await self.clocked_request()
            self.assertTrue(closed)
            self.assertEqual(metrics(response)['app'], 0, 'Connection cleanup follows successful headers')
            with patch('app.request_timing.perf_counter', side_effect=lambda: self.clock[0]), \
                 patch('app.main.perf_counter', side_effect=lambda: self.clock[0]):
                response = await self.client.post('/library/reading-estimates', json={'targets':[]})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(metrics(response)['api_other'], 40, 'Early error cleanup belongs to elapsed residual')
        self.clock[0] = 1.0
        consumed = []
        async def body():
            consumed.append(True)
            self.clock[0] += .040
            yield b'{"estimates":[]}'
        stream = StreamingResponse(body(),media_type='application/json')
        async def route(_):
            self.clock[0] += .010
            return stream
        request = Request({'type':'http','method':'POST','path':'/library/reading-estimates',
                           'headers':[],'query_string':b''})
        with patch('app.request_timing.perf_counter',side_effect=lambda:self.clock[0]), \
             patch('app.main.perf_counter',side_effect=lambda:self.clock[0]):
            response = await revalidate_pages(request,route)
        self.assertFalse(consumed, 'API timing must not consume the JSON body before headers')
        self.assertIs(response,stream)
        self.assertEqual(metrics(response)['app'], 10)

    async def test_concurrent_requests_and_exception_scopes_are_isolated(self):
        settings.detailed_server_timing = True
        clocks = ContextVar('api_test_clock', default=None)
        original = dependencies.get_current_user
        async def auth(*args, **kwargs):
            result = await original(*args, **kwargs)
            clocks.get()[0] += clocks.get()[1]
            return result
        async def request(delay):
            token = clocks.set([1.0,delay])
            try:
                return metrics(await self.request())
            finally:
                clocks.reset(token)
        with patch.object(dependencies,'get_current_user',auth), \
             patch('app.request_timing.perf_counter',side_effect=lambda:clocks.get()[0]), \
             patch('app.main.perf_counter',side_effect=lambda:clocks.get()[0]):
            first, second = await asyncio.gather(request(.020),request(.070))
        self.assertEqual(first['api_auth'], 20)
        self.assertEqual(second['api_auth'], 70)
        with self.assertRaises(RuntimeError), timing.collect(True,api=True), timing.api_phase('api_validate'):
            raise RuntimeError('fixture')
        self.assertEqual(timing.headers(100), [])
        self.assertFalse(timing.api_enabled())

    async def test_inactive_spans_and_validation_children_do_not_collect(self):
        for enabled,api in ((False,False),(False,True),(True,False)):
            with self.subTest(enabled=enabled,api=api), timing.collect(enabled,api=api):
                with patch('contextlib._GeneratorContextManager',side_effect=AssertionError('inactive span allocation')), \
                     patch('app.request_timing.perf_counter',side_effect=AssertionError('inactive clock read')):
                    self.assertIsNone(timing.api_start())
                    with timing.api_phase('api_files'):
                        pass
        with timing.collect(True,api=True), patch('app.request_timing.perf_counter',side_effect=lambda:self.clock[0]):
            with timing.api_phase('api_files'):
                self.clock[0] += .020
            values = dict(entry.split(';dur=') for entry in timing.headers(20))
            self.assertEqual(float(values['api_files']), 0, 'Validation children must not escape their parent scope')

    async def test_background_refresh_does_not_inherit_api_phases(self):
        original = build_estimator
        async def build(*args, **kwargs):
            with timing.api_phase('api_estimator'):
                result = await original(*args, **kwargs)
                self.clock[0] += .100
                return result
        with timing.collect(True,api=True), patch('app.stats.estimates.build_estimator',build), \
             patch('app.request_timing.perf_counter',side_effect=lambda:self.clock[0]):
            await asyncio.create_task(ready.cache._refresh(1))
            with timing.api_phase('api_input'):
                self.clock[0] += .020
            values = dict(entry.split(';dur=') for entry in timing.headers(20))
            self.assertEqual(float(values['api_estimator']), 0, 'Background work must not become an API route phase')
            self.assertEqual(float(values['api_input']), 20)
