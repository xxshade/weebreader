"""Cover cache regressions with persistent profiles, in Chrome AND Firefox.

Use the same browser flags as test_resync_browser.py. All data is disposable;
HTTP cache remains enabled. Server requests distinguish cache hits from 304s.
"""
import argparse

from browser_checks import add_browser_argument, selected_browsers, baseline_passed
from contextlib import closing
import hashlib
import io
import os
import re
import sqlite3
import sys
import time
import unittest

import requests
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService

from ci_report import unittest_failures
from test_resync_browser import BrowserChecks


class CoverCache(unittest.TestCase):
    js = BrowserChecks.js
    wait_for = BrowserChecks.wait_for

    @classmethod
    def setUpClass(cls):
        fixture = type('Fixture', (BrowserChecks, unittest.TestCase), {})
        cls.addClassCleanup(fixture.doClassCleanups)
        fixture.setUpClass()
        cls.fixture, cls.data, cls.base = fixture.fixture, fixture.data, fixture.base
        cls.cover = (cls.data / 'covers' / 'series-1-fixture.webp').read_bytes()
        cls.version = hashlib.sha256(cls.cover).hexdigest()
        with closing(sqlite3.connect(cls.data / 'library.db')) as db, db:
            db.execute('UPDATE items SET cover_path=(SELECT cover_path FROM series WHERE series.id=items.series_id)')
            db.execute("INSERT INTO novels(id,title,source_url,active,cover_image,cover_image_content_type,cover_image_hash) VALUES(1,'Cache fixture','https://example.invalid/cache',0,?,'image/webp',?)",
                       (cls.cover, cls.version))
            db.execute('INSERT INTO volumes(id,novel_id,number) VALUES(1,1,1)')
            db.execute("INSERT INTO chapters(id,novel_id,volume_id,number,title,url,content_html,content_size) VALUES(1,1,1,1,'Chapter','https://example.invalid/ch1','<p>Fixture</p>',14)")

    def launch(self):
        profile = self.fixture / 'browser-profile'
        profile.mkdir(exist_ok=True)
        if self.browser == 'chrome':
            options = ChromeOptions()
            options.binary_location = ARGS.chrome_binary
            for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage', f'--user-data-dir={profile}'):
                options.add_argument(flag)
            driver = webdriver.Chrome(options=options, service=ChromeService(ARGS.chromedriver))
        else:
            options = FirefoxOptions()
            options.binary_location = ARGS.firefox_binary
            options.add_argument('-headless')
            options.add_argument('-profile')
            options.add_argument(str(profile))
            options.set_preference('browser.cache.disk.enable', True)
            options.set_preference('browser.cache.memory.enable', True)
            driver = webdriver.Firefox(options=options, service=FirefoxService(
                ARGS.geckodriver, log_output=str(self.fixture / 'geckodriver.log'),
                env={**os.environ, 'MOZ_DISABLE_CONTENT_SANDBOX': '1',
                     'XDG_CACHE_HOME': str(self.fixture / 'xdg-cache')}))
        self.driver = driver
        self.addCleanup(self.quit_driver)
        return driver

    def quit_driver(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    def image_requests(self):
        return [line for line in (self.fixture / 'server.log').read_text().splitlines()
                if re.search(r'"GET /(?:covers/|novels/\d+/cover)', line)]

    def images_ready(self):
        BrowserChecks.load_all_covers(self, 'img[src^="/covers/"], img[src^="/novels/"]')
        self.wait_for('[...document.images].filter(i=>/\\/covers\\/|\\/novels\\/\\d+\\/cover/.test(i.src)).length>0 && '
                      '[...document.images].filter(i=>/\\/covers\\/|\\/novels\\/\\d+\\/cover/.test(i.src)).every(i=>i.complete && i.naturalWidth>0)')

    def test_restart_and_other_device_reading_reuse_the_same_cover(self):
        session = requests.Session()
        self.addCleanup(session.close)
        session.post(self.base + '/login', data={
            'username': 'probe', 'password': 'disposable-browser-test-password'}).raise_for_status()
        driver = self.launch()
        driver.get(self.base + '/login')
        for name, value in session.cookies.items():
            driver.add_cookie({'name': name, 'value': value, 'expiry': int(time.time()) + 3600})
        driver.get(self.base + '/')
        self.images_ready()
        expected = self.base + f'/novels/1/cover?v={self.version}'
        self.assertEqual(self.js("return document.querySelector('#series-grid [data-novel-id=\"1\"] img').src"), expected)
        cold = len(self.image_requests())
        self.assertGreater(cold, 0, 'The cold-cache control must transfer image bodies')
        for path in ('/series/1', '/novels/1', '/'):
            driver.get(self.base + path)
            self.images_ready()
            self.assertEqual(len(self.image_requests()), cold)
        self.quit_driver()
        driver = self.launch()  # Same on-disk profile, including the session cookie.
        for path in ('/series/1', '/novels/1', '/'):
            driver.get(self.base + path)
            self.images_ready()
            self.assertEqual(len(self.image_requests()), cold, 'Restart must reuse retained disk cache')
        self.wait_for("document.getElementById('library-events')?.libraryEventsBound")
        home = driver.current_window_handle
        driver.switch_to.new_window('tab')
        driver.get(self.base + '/static/manifest.webmanifest')
        response = session.post(self.base + '/novels/1/progress', json={
            'chapter_id': 1, 'scroll_percent': 25, 'chapter_percent': 25, 'paragraph_index': 0})
        response.raise_for_status()
        driver.switch_to.window(home)
        self.wait_for('!!document.querySelector(\'#reading-grid [data-novel-id="1"] img\')')
        self.images_ready()
        self.assertEqual(self.js('return document.querySelector(\'#reading-grid [data-novel-id="1"] img\').src'), expected)
        self.assertEqual(len(self.image_requests()), cold, 'Reading must reuse the cached, versioned cover')
        # A changed cover must still transfer its new bytes. Old versions
        # intentionally remain cached under their own distinct URL.
        buffer = io.BytesIO()
        Image.new('RGB', (200, 300), (240, 20, 70)).save(buffer, 'WEBP')
        updated = buffer.getvalue()
        version = hashlib.sha256(updated).hexdigest()
        with closing(sqlite3.connect(self.data / 'library.db')) as db, db:
            db.execute('UPDATE novels SET cover_image=?,cover_image_hash=? WHERE id=1', (updated, version))
        driver.get(self.base + '/novels/1')
        self.images_ready()
        new_url = self.base + f'/novels/1/cover?v={version}'
        self.assertEqual(self.js("return document.querySelector('main img').src"), new_url)
        self.assertEqual(len(self.image_requests()), cold + 1)
        with session.get(new_url) as response:
            self.assertEqual(response.content, updated)
            self.assertIn('immutable', response.headers['Cache-Control'])
        print(self.browser + ': retained cache survives restart and SSE; changed cover transfers new content', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    parser.add_argument('--chrome-binary', default='/usr/bin/chromium')
    parser.add_argument('--chromedriver', default='/usr/bin/chromedriver')
    parser.add_argument('--firefox-binary', default='/usr/bin/firefox-esr')
    parser.add_argument('--geckodriver', default='/usr/bin/geckodriver')
    ARGS = parser.parse_args()
    suite = unittest.TestSuite()
    for browser in selected_browsers(ARGS):
        case = type(browser.title() + 'CoverCache', (CoverCache,), {'browser': browser})
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    expected = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    unittest_failures(result)
    sys.exit(not baseline_passed(result, expected))
