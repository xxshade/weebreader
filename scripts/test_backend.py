"""Run unittest with unexpected async errors and resource warnings as failures.

Use ``python -m scripts.test_backend [test.name ...]``. Expected failures must
be captured with assertLogs/assertWarns/assertRaises inside their tests.
"""
import asyncio
from contextlib import ExitStack
import gc
import logging
import sys
import threading
import os
import traceback
import unittest
from unittest.mock import patch
import warnings

from scripts.ci_report import unittest_failures


def run(suite, *, stream=None, verbosity=2, cleanup=None):
    issues = []
    owner = ['test discovery']

    def report(message):
        issues.append(f'{owner[0]}: {message}')

    class Errors(logging.Handler):
        def emit(self, record):
            report(self.format(record))

    class Result(unittest.TextTestResult):
        slow_test = None

        def report_slow_test(self, name, thread_id):
            # An Actions timeout otherwise discards the useful diagnostic:
            # identify the test and the frame that is still waiting.
            frame = sys._current_frames().get(thread_id)
            trace = ''.join(traceback.format_stack(frame)) if frame else 'Thread has exited.'
            message = f'Test still running after 60 seconds: {name}\n{trace}'
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                escaped = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
                print(f'::warning title=Slow backend test::{escaped}', flush=True)
            else:
                self.stream.write(message + '\n')
                self.stream.flush()

        def startTest(self, test):
            owner[0] = test.id()
            super().startTest(test)
            self.slow_test = threading.Timer(60, self.report_slow_test,
                                             args=(test.id(), threading.get_ident()))
            self.slow_test.daemon = True
            self.slow_test.start()

        def stopTest(self, test):
            if self.slow_test:
                self.slow_test.cancel()
            super().stopTest(test)

        def stopTestRun(self):
            if cleanup:
                cleanup()
            # Warnings emitted by destructors must be observed before the
            # result is evaluated, rather than at interpreter shutdown.
            gc.collect()
            for message in issues:
                test = unittest.FunctionTestCase(lambda: None, description='Unexpected test diagnostic')
                self.addFailure(test, (AssertionError, AssertionError(message), None))
            super().stopTestRun()

    original_new_loop = asyncio.events.new_event_loop

    def new_loop():
        loop = original_new_loop()

        def exception_handler(loop, context):
            exc = context.get('exception')
            report(context.get('message', 'Asyncio exception') + '\n' +
                   (''.join(traceback.format_exception(exc)) if exc else repr(context)))

        loop.set_exception_handler(exception_handler)
        return loop

    def showwarning(message, category, filename, lineno, file=None, line=None):
        if issubclass(category, (RuntimeWarning, ResourceWarning)):
            report(warnings.formatwarning(message, category, filename, lineno))
        else:
            original_showwarning(message, category, filename, lineno, file, line)

    def unraisable(event):
        report(f'{event.err_msg or "Unraisable exception"}: ' +
               ''.join(traceback.format_exception(event.exc_type, event.exc_value, event.exc_traceback)))

    handler = Errors(logging.ERROR)
    original_showwarning = warnings.showwarning
    with ExitStack() as stack:
        stack.enter_context(warnings.catch_warnings())
        warnings.simplefilter('always', RuntimeWarning)
        warnings.simplefilter('always', ResourceWarning)
        stack.enter_context(patch.object(warnings, 'showwarning', showwarning))
        stack.enter_context(patch.object(sys, 'unraisablehook', unraisable))
        stack.enter_context(patch.object(asyncio.events, 'new_event_loop', new_loop))
        logging.getLogger().addHandler(handler)
        stack.callback(logging.getLogger().removeHandler, handler)
        discovered = suite() if callable(suite) else suite
        result = unittest.TextTestRunner(stream=stream, verbosity=verbosity, resultclass=Result).run(discovered)
    unittest_failures(result)
    return result


def main():
    from app.database import close_pool
    loader = unittest.defaultTestLoader
    def suite():
        return loader.loadTestsFromNames(sys.argv[1:]) if len(sys.argv) > 1 else loader.discover('tests', top_level_dir='.')
    result = run(suite, cleanup=lambda: asyncio.run(close_pool()))
    return int(not result.wasSuccessful() or result.testsRun == 0)


if __name__ == '__main__':
    sys.exit(main())
