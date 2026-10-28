import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.library import watcher

class FakeTimer:
    instances = []
    def __init__(self, delay, fn, args=()):
        self.fn, self.args, self.cancelled = fn, args, False
        self.instances.append(self)
    def start(self): pass
    def cancel(self): self.cancelled = True
    def fire(self): self.fn(*self.args)

class WatcherTimers(unittest.TestCase):
    def test_started_old_timer_cannot_remove_or_run_new_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'book.epub'; p.write_bytes(b'partial')
            loop = asyncio.new_event_loop()
            self.addCleanup(loop.close)
            handler = watcher._DebouncedHandler(1, directory, loop)
            submitted = []
            def capture(coro, event_loop):
                submitted.append(True); coro.close()
            with patch.object(watcher.threading, 'Timer', FakeTimer), patch.object(asyncio, 'run_coroutine_threadsafe', capture):
                handler._schedule(str(p)); old = FakeTimer.instances[-1]
                handler._schedule(str(p)); new = FakeTimer.instances[-1]
                old.fire()
                self.assertIs(handler._pending[str(p)], new)
                self.assertFalse(submitted)
                handler._schedule_delete(str(p))
                self.assertTrue(new.cancelled)

    def test_escaping_future_failure_is_logged(self):
        import concurrent.futures
        handler = watcher._DebouncedHandler(1, '/tmp', None)
        future = concurrent.futures.Future()
        future.set_exception(RuntimeError('synthetic failure'))
        with self.assertLogs('watcher', level='ERROR') as logs:
            handler._log_future_failure(future)
        self.assertIn('synthetic failure', '\n'.join(logs.output))
