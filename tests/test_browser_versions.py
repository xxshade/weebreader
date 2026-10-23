"""Stable metadata is resolved once and unavailable/wrong targets fail closed."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
import zipfile

from scripts.browser_versions import resolve, require_version
from scripts.provision_android_browsers import chromium_version


class StableBrowsers(unittest.TestCase):
    def setUp(self):
        self.values = {
            'chrome': {'channels': {'Stable': {'version': '155.0.8059.84'}}},
            'firefox': {'LATEST_FIREFOX_VERSION': '158.0.1'},
            'firefox_android': {'version': '158.0'},
            'vivaldi_android': '<a href="https://downloads.vivaldi.com/stable/Vivaldi.8.2.4147.163_x86-64.apk">Android</a>',
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                value = owner.values.get(self.path[1:])
                self.send_response(200 if value is not None else 503)
                self.end_headers()
                self.wfile.write(value.encode() if isinstance(value, str) else json.dumps(value).encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.sources = {name: f'http://127.0.0.1:{server.server_port}/{name}' for name in self.values}

    def test_default_selection_needs_only_desktop_sources(self):
        self.values.pop('firefox_android')
        self.values.pop('vivaldi_android')
        value = resolve(self.sources)
        self.assertEqual(value['versions'], {'chrome': '155.0.8059.84', 'firefox': '158.0.1'})
        self.assertEqual(set(value['sources']), {'chrome', 'firefox'})
        self.assertEqual(value['downloads'], {})

    def test_all_four_targets_are_selected_from_their_own_stable_channel(self):
        value = resolve(self.sources, include_android=True)
        self.assertEqual(value['versions'], {'chrome': '155.0.8059.84', 'firefox': '158.0.1',
                                            'vivaldi_android': '8.2.4147.163', 'firefox_android': '158.0'})
        self.assertEqual(value['downloads']['vivaldi_android'], 'https://downloads.vivaldi.com/stable/Vivaldi.8.2.4147.163_x86-64.apk')

    def test_missing_or_invalid_stable_metadata_blocks_selection(self):
        for broken in (None, {'version': '125.3.0-old'}, {}):
            with self.subTest(broken=broken):
                self.values['firefox_android'] = broken
                with self.assertRaises(Exception):
                    resolve(self.sources, include_android=True)

    def test_old_browser_and_missing_package_do_not_satisfy_selected_version(self):
        for actual in ('125.3.0', None, ''):
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                require_version(actual, '158.0')
        require_version('158.0', '158.0')

    def test_vivaldi_without_an_official_stable_android_apk_blocks_selection(self):
        self.values['vivaldi_android'] = '<a href="https://example.invalid/Vivaldi.8.2.4147.163_x86-64.apk">Other source</a>'
        with self.assertRaises(ValueError):
            resolve(self.sources, include_android=True)

    def test_vivaldi_driver_uses_the_signed_engine_version_instead_of_the_brand(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'browser.apk'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('lib/x86_64/libchrome.so', b'Chrome/8.2.4147.163\0Chromium\0' + b'152.0.7977.169\0')
            self.assertEqual(chromium_version(path), '152.0.7977.169')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('lib/x86_64/libchrome.so', b'152.0.7977.169\0other\0' + b'151.0.7900.12\0')
            with self.assertRaises(ValueError):
                chromium_version(path)
