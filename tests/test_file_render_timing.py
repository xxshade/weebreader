"""Dispatch and template probes separate costs without changing work or output."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jinja2 import Environment, TemplateSyntaxError

from app import request_timing as timing
from app.config import settings
from app.stats import ready_estimates as ready
from app.template_timing import RenderTimingExtension


class FileDispatchTiming(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / 'source'
        path.write_bytes(b'fixture')
        stat = path.stat()
        sources = {i: (str(path), (stat.st_size, stat.st_mtime_ns)) for i in range(257)}
        self.state = SimpleNamespace(sources=sources, items={i: {'series_id': 1} for i in sources},
                                    file_checks=await asyncio.to_thread(ready._prepare_files, sources))

    async def observed(self):
        with timing.collect(True, api=True), timing.api_phase('api_validate'):
            self.assertEqual(await ready.cache._files(self.state), set())
            values = dict(timing._metrics.get())
            fields = timing.headers(100)
        self.assertEqual(values['file_sources'], 257)
        self.assertEqual(values['file_blocks'], 3)
        self.assertGreaterEqual(values['file_jobs'], 1)
        self.assertLessEqual(sum(values[k] for k in ('file_queue', 'file_worker', 'file_resume')),
                             values['api_files'] + .1)
        worker = next(field for field in fields if field.startswith('file_worker;'))
        self.assertIn(f';desc="jobs={values["file_jobs"]} blocks=3 sources=257"', worker)
        self.assertNotIn('source', worker.split('desc=')[0])
        return values

    async def test_queue_is_separate_from_worker(self):
        entered, release = threading.Event(), threading.Event()
        def occupied():
            entered.set()
            if not release.wait(5):
                raise AssertionError('Executor was not released')
        other = self.loop.run_in_executor(None, occupied)
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task = asyncio.create_task(self.observed())
            await asyncio.sleep(.025)
            release.set()
            values = await task
            self.assertGreaterEqual(values['file_queue'], 15)
        finally:
            release.set()
            await other

    async def test_worker_wall_and_cpu_are_distinct(self):
        original = ready._check_prepared_files
        def sleeping(*args):
            time.sleep(.025)
            return original(*args)
        with patch.object(ready, '_check_prepared_files', sleeping):
            values = await self.observed()
        self.assertGreaterEqual(values['file_worker'], 15)
        self.assertLess(values['file_worker_cpu'], values['file_worker'] * .7)
        def busy(*args):
            deadline = time.thread_time() + .02
            while time.thread_time() < deadline:
                pass
            return original(*args)
        with patch.object(ready, '_check_prepared_files', busy):
            values = await self.observed()
        self.assertGreaterEqual(values['file_worker_cpu'], 15)

    async def test_loop_resume_is_separate_from_worker(self):
        original = ready._check_prepared_files
        def delayed_resume(*args):
            value = original(*args)
            self.loop.call_soon_threadsafe(lambda: time.sleep(.025))
            return value
        with patch.object(ready, '_check_prepared_files', delayed_resume):
            values = await self.observed()
        self.assertGreaterEqual(values['file_resume'], 15)

    async def test_cancelled_worker_does_not_publish_late_metrics(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        original = ready._check_prepared_files
        def blocked(*args):
            entered.set()
            try:
                if not release.wait(5):
                    raise AssertionError('Worker was not released')
                return original(*args)
            finally:
                done.set()
        with patch.object(ready, '_check_prepared_files', blocked), timing.collect(True, api=True), \
                timing.api_phase('api_validate'):
            active = timing._metrics.get()
            task = asyncio.create_task(ready.cache._files(self.state))
            try:
                while not entered.is_set():
                    await asyncio.sleep(.001)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                before = dict(active)
            finally:
                release.set()
                while not done.is_set():
                    await asyncio.sleep(.001)
            await asyncio.sleep(.01)
            self.assertEqual(active, before)
            self.assertNotIn('file_jobs', active)
            self.assertIsNone(timing._metrics.get().get('file_sources'))

    async def test_inactive_scopes_read_no_new_clocks_and_keep_checks(self):
        for enabled in (False, True):
            with timing.collect(enabled), patch.object(ready, 'thread_time', side_effect=AssertionError('CPU probe outside scope')), \
                    patch.object(timing, 'perf_counter', side_effect=AssertionError('Wall probe outside scope')):
                self.assertEqual(await ready.cache._files(self.state), set())
        # No stat cache: an actual edit is detected even when probes are off.
        Path(self.state.sources[0][0]).write_bytes(b'changed fixture')
        with timing.collect(False):
            self.assertEqual(await ready.cache._files(self.state), {1})
            self.assertEqual(timing.headers(1), [])


class TemplateTiming(unittest.TestCase):
    def env(self):
        return Environment(autoescape=True, extensions=[RenderTimingExtension])

    def test_disabled_compiles_to_original_and_enabled_preserves_escaping_and_accounting(self):
        plain = 'prefix {{ text }} suffix'
        marked = "{% render_timing 'render_content' %}prefix {% render_timing 'render_grid' %}{{ text }}{% endrender_timing %} suffix{% endrender_timing %}"
        with patch.object(settings, 'detailed_server_timing', False):
            env = self.env()
            self.assertEqual(env.compile(plain, raw=True), env.compile(marked, raw=True))
            template = env.from_string(marked)
        with timing.collect(True), patch.object(timing, 'perf_counter', side_effect=AssertionError('Disabled template clock')):
            expected = template.render(text='<script>&')
            self.assertNotIn('render_content', timing._metrics.get())
        with patch.object(settings, 'detailed_server_timing', True):
            template = self.env().from_string(marked)
        with timing.collect(True), patch.object(timing, 'perf_counter', side_effect=[1., 1.01, 1.03, 1.04]):
            self.assertEqual(template.render(text='<script>&'), expected)
            self.assertEqual(expected, 'prefix &lt;script&gt;&amp; suffix')
            values = dict(field.split(';dur=') for field in timing.headers(50))
            self.assertEqual(values['render_grid'], '20.000')
            self.assertEqual(values['render_content'], '40.000')
            self.assertEqual(values['render_content_other'], '20.000')
        with timing.collect(False), patch.object(timing, 'perf_counter', side_effect=AssertionError('Inactive request clock')):
            self.assertEqual(template.render(text='<script>&'), expected)

    def test_unknown_phase_fails_and_failed_render_leaks_no_request_metrics(self):
        with self.assertRaisesRegex(TemplateSyntaxError, 'Unknown render timing phase'):
            self.env().from_string("{% render_timing 'file_name' %}x{% endrender_timing %}")
        with patch.object(settings, 'detailed_server_timing', True):
            template = self.env().from_string("{% render_timing 'render_content' %}{{ fail() }}{% endrender_timing %}")
        def fail():
            raise RuntimeError('fixture')
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with timing.collect(True):
                template.render(fail=fail)
        self.assertEqual(timing.headers(1), [])
