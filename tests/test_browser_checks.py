"""Selected engines and their completion criteria cannot silently lose coverage."""
import argparse
from contextlib import redirect_stderr
import io
from itertools import product
from types import SimpleNamespace
import unittest

from scripts.browser_checks import (
    add_browser_argument, baseline_passed, browser_results_passed,
    mutation_rejected, selected_browsers,
)


def result(*, count=1, skipped=(), errors=(), failures=(), successful=True):
    return SimpleNamespace(testsRun=count, skipped=skipped, errors=errors,
                           failures=failures, wasSuccessful=lambda: successful)


class BrowserChecks(unittest.TestCase):
    def parser(self):
        parser = argparse.ArgumentParser()
        add_browser_argument(parser)
        return parser

    def test_default_invocation_keeps_both_browsers(self):
        self.assertEqual(selected_browsers(self.parser().parse_args([])), ('chrome', 'firefox'))

    def test_an_explicit_target_does_not_execute_the_other_browser(self):
        for browser in ('chrome', 'firefox'):
            with self.subTest(browser=browser):
                args = self.parser().parse_args(['--browser', browser])
                self.assertEqual(selected_browsers(args), (browser,))

    def test_an_unknown_target_is_rejected(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser().parse_args(['--browser', 'other'])

    def test_baseline_requires_the_complete_unskipped_suite(self):
        self.assertTrue(baseline_passed(result(count=2), 2))
        for outcome, expected in ((result(count=0), 0), (result(count=1), 2),
                                  (result(count=2, skipped=['test']), 2),
                                  (result(count=2, successful=False), 2)):
            with self.subTest(outcome=outcome, expected=expected):
                self.assertFalse(baseline_passed(outcome, expected))

    def test_mutations_require_the_intended_failure_in_each_selected_browser(self):
        failures = [('ChromeHeader.test', 'intended'), ('FirefoxHeader.test', 'intended')]
        self.assertTrue(mutation_rejected(result(count=2, failures=failures),
                                          ('chrome', 'firefox'), 'Header', 'intended'))
        for browser, failure in zip(('chrome', 'firefox'), failures):
            self.assertTrue(mutation_rejected(result(failures=[failure]), (browser,), 'Header', 'intended'))
        for outcome, browsers in (
            (result(count=1, failures=failures[:1]), ('chrome', 'firefox')),
            (result(count=2, failures=failures[:1]), ('chrome', 'firefox')),
            (result(failures=failures[:1]), ('firefox',)),
            (result(failures=[('ChromeHeader.test', 'unrelated')]), ('chrome',)),
            (result(errors=['setup failed'], failures=failures[:1]), ('chrome',)),
            (result(skipped=['test'], failures=failures[:1]), ('chrome',)),
            (result(count=0), ()),
        ):
            with self.subTest(outcome=outcome, browsers=browsers):
                self.assertFalse(mutation_rejected(outcome, browsers, 'Header', 'intended'))

    def test_summary_only_accepts_two_successful_results(self):
        for chrome, firefox in product(('success', 'failure', 'cancelled', 'skipped', ''), repeat=2):
            with self.subTest(chrome=chrome, firefox=firefox):
                self.assertEqual(browser_results_passed(chrome, firefox),
                                 (chrome, firefox) == ('success', 'success'))
