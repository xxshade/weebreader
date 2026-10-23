"""MyAnimeList configuration failures in real desktop and mobile viewports."""
import argparse
import io
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

import requests

from browser_checks import add_browser_argument, selected_browsers, baseline_passed, mutation_rejected
from ci_report import unittest_failures
import test_audit_browser as audit

ROOT = Path(__file__).resolve().parents[1]


class TrackingChecks(audit.AuditChecks):
    client_id = ''
    redirect_uri = ''

    @classmethod
    def setUpClass(cls):
        # Compose passes an empty value when the host has not configured it.
        with patch.dict(os.environ, {'APP_MYANIMELIST_CLIENT_ID': cls.client_id,
                                    'APP_MYANIMELIST_REDIRECT_URI': cls.redirect_uri}):
            super().setUpClass()

    def test_missing_registration_explains_configuration(self):
        # Inspect the real redirect before clicking: a broken guard must not
        # send the test browser to the external OAuth provider.
        with requests.Session() as session:
            session.cookies.update({c['name']: c['value'] for c in self.driver.get_cookies()})
            response = session.post(self.base + '/settings/tracking/myanimelist/connect',
                                    allow_redirects=False, timeout=10)
        self.assertEqual(response.headers.get('location'),
                         '/settings?tracker_error=not-configured#tracking',
                         'Missing registration must stay in Settings')
        self.assertEqual(self.row('SELECT COUNT(*) AS count FROM tracker_oauth_pending'), {'count': 0})
        self.driver.get(self.base + '/settings#tracking')
        self.driver.find_element('css selector', 'form[action="/settings/tracking/myanimelist/connect"] button').click()
        self.wait("location.search==='?tracker_error=not-configured'")
        error = self.driver.find_element('css selector', '#tracking > p')
        self.assertTrue(error.is_displayed())
        self.assertIn('APP_MYANIMELIST_CLIENT_ID', error.text,
                      'Configuration error must explain the client ID')
        self.assertIn('APP_MYANIMELIST_REDIRECT_URI', error.text,
                      'Configuration error must explain the redirect URI')
        self.assertIn('then restart the server', error.text)
        self.assertLessEqual(self.js('return document.documentElement.scrollWidth'),
                             self.js('return innerWidth'))


def suite(*, mobile=True, configurations=None):
    if configurations is None:
        configurations = (('', 'https://reader.example/cb'), ('fixture-client-id', ''), ('', ''))
    return unittest.TestSuite(type(browser.title() + 'Tracking' + str(width), (TrackingChecks,), {
        'browser': browser, 'viewport_width': width, 'client_id': client_id,
        'redirect_uri': redirect_uri})('test_missing_registration_explains_configuration')
        for browser in selected_browsers(ARGS) for width in ((1280, 320) if mobile else (1280,))
        for client_id, redirect_uri in configurations)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    parser.add_argument('--chrome-binary', default='/usr/bin/chromium')
    parser.add_argument('--chromedriver', default='/usr/bin/chromedriver')
    parser.add_argument('--firefox-binary', default='/usr/bin/firefox')
    parser.add_argument('--geckodriver', default='/usr/bin/geckodriver')
    ARGS = parser.parse_args()
    audit.ARGS = ARGS
    result = unittest.TextTestRunner(verbosity=2).run(suite())
    unittest_failures(result)
    if not baseline_passed(result, len(selected_browsers(ARGS)) * 6):
        sys.exit(1)
    for filename, before, after, signal, configuration in (
        ('app/tracking/router.py', 'if not settings.myanimelist_client_id or not settings.myanimelist_redirect_uri:',
         'if not settings.myanimelist_redirect_uri:', 'Missing registration must stay in Settings',
         ('', 'https://reader.example/cb')),
        ('app/tracking/router.py', 'if not settings.myanimelist_client_id or not settings.myanimelist_redirect_uri:',
         'if not settings.myanimelist_client_id:', 'Missing registration must stay in Settings',
         ('fixture-client-id', '')),
        ('app/templates/settings.html', 'Set APP_MYANIMELIST_CLIENT_ID', 'Configure OAuth',
         'Configuration error must explain the client ID', ('', '')),
        ('app/templates/settings.html', 'APP_MYANIMELIST_REDIRECT_URI to the values', 'the callback to the values',
         'Configuration error must explain the redirect URI', ('', '')),
    ):
        source = ROOT / filename
        original = source.read_text()
        if original.count(before) != 1:
            raise ValueError('Tracking mutation must match exactly once.')
        try:
            source.write_text(original.replace(before, after))
            output = io.StringIO()
            control = unittest.TextTestRunner(stream=output, verbosity=2).run(
                suite(mobile=False, configurations=(configuration,)))
        finally:
            source.write_text(original)
        if not mutation_rejected(control, selected_browsers(ARGS), 'Tracking', signal):
            print(output.getvalue())
            raise AssertionError('Every selected browser must reject the tracking defect.')
        print(' and '.join(b.title() for b in selected_browsers(ARGS)) + ' rejected: ' + signal, flush=True)
