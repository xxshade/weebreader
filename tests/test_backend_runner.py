"""A passing unittest result must not hide asynchronous errors or leaked resources."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class BackendRunner(unittest.TestCase):
    def execute(self, body, *, asynchronous=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'probe.py').write_text(
                'import asyncio, gc, logging, unittest, warnings\n'
                'from app.background import run_background\n'
                f'class Probe(unittest.{"IsolatedAsyncioTestCase" if asynchronous else "TestCase"}):\n'
                f'    {"async " if asynchronous else ""}def test_probe(self):\n' +
                textwrap.indent(textwrap.dedent(body).strip(), '        ') + '\n')
            return subprocess.run([sys.executable, '-m', 'scripts.test_backend', 'probe'],
                                  env={**os.environ, 'PYTHONPATH': str(path) + os.pathsep + str(Path.cwd()),
                                       'APP_DATA_DIR': str(path / 'data')},
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)

    def test_unexpected_diagnostics_fail_the_process_even_when_the_test_passes(self):
        cases = (
            (False, 'warnings.warn("leaked resource", ResourceWarning)', 'leaked resource'),
            (False, 'warnings.warn("runtime bug", RuntimeWarning)', 'runtime bug'),
            (False, 'logging.getLogger("app.probe").error("logged failure")', 'logged failure'),
            (False, 'async def abandoned(): pass\nabandoned()\ngc.collect()', 'was never awaited'),
            (False, 'class Broken:\n    def __del__(self): raise RuntimeError("destructor bug")\nBroken()\ngc.collect()', 'destructor bug'),
            (True, 'async def broken(): raise RuntimeError("unhandled task")\nasyncio.create_task(broken())\nawait asyncio.sleep(.01)', 'unhandled task'),
            (True, 'async def broken(): raise RuntimeError("retrieved but logged")\nrun_background(broken())\nawait asyncio.sleep(.01)', 'retrieved but logged'),
        )
        for asynchronous, body, message in cases:
            with self.subTest(message=message):
                result = self.execute(body, asynchronous=asynchronous)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(message, result.stdout)

    def test_explicitly_asserted_failures_and_warnings_remain_successful(self):
        result = self.execute('''
            with self.assertWarns(ResourceWarning):
                warnings.warn('expected resource warning', ResourceWarning)
            with self.assertLogs('app.probe', level='ERROR') as logs:
                logging.getLogger('app.probe').error('expected failure')
            self.assertIn('expected failure', logs.output[0])
        ''')
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_no_tests_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'empty.py').write_text('')
            result = subprocess.run([sys.executable, '-m', 'scripts.test_backend', 'empty'],
                                    env={**os.environ, 'PYTHONPATH': directory,
                                         'APP_DATA_DIR': directory}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
