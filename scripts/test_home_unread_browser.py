"""Home unread filtering, saved-state restoration and real SSE updates in both engines."""
import argparse

from browser_checks import add_browser_argument, selected_browsers, baseline_passed, mutation_rejected
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import sys
import unittest

from selenium.webdriver.common.keys import Keys

import test_audit_browser as audit
from ci_report import unittest_failures


class HomeUnreadChecks(audit.AuditChecks):
    @classmethod
    def seed_readers(cls):
        super().seed_readers()
        with closing(sqlite3.connect(cls.fixture.data / 'library.db')) as db, db:
            db.execute("INSERT INTO reading_progress(user_id,item_id,status,current_page,progress_percent) VALUES(1,1,'completed',0,100),(1,2,'reading',0,25)")
            for novel in (101, 102):
                db.execute("INSERT INTO novels(id,title,source_url,sync_status,active) VALUES(?,?,?,'idle',0)",
                           (novel, f'Novel {novel}', f'https://example.invalid/{novel}'))
            db.execute('INSERT INTO volumes(id,novel_id,number) VALUES(101,101,1)')
            db.execute("INSERT INTO chapters(id,novel_id,volume_id,number,title,url,order_index) VALUES(101,101,101,1,'Not downloaded','https://example.invalid/chapter101',0)")

    def home(self, width=1280):
        self.driver.set_window_size(width, 900)
        if self.browser == 'chrome':
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
                {'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        else:
            self.driver.browsing_context.set_viewport(context=self.driver.current_window_handle,
                viewport={'width': width, 'height': 900})
        self.assertEqual(self.js('return innerWidth'), width)
        self.driver.get(self.base + '/')
        self.wait('window.libraryGrid?.isLive()')

    def visible(self, kind, number):
        card = self.driver.find_element('css selector', f'.series-card[data-{kind}-id="{number}"]')
        return card.is_displayed()

    def post(self, path, body=None):
        result = self.driver.execute_async_script("""const [path,body,done]=arguments;
            fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
              .then(r=>done(r.ok),e=>done(String(e)));""", path, body)
        self.assertIs(result, True)

    def test_home_unread_browsing(self):
        for width in (1280, 320):
            self.home(width)
            self.js('localStorage.clear()')
            self.home(width)
            toggle = self.driver.find_element('id', 'home-unread-only')
            self.assertFalse(toggle.is_selected())
            self.assertTrue(self.visible('series', 1))
            reading = self.driver.find_element('id', 'reading-section').is_displayed()
            self.driver.find_element('id', 'filters-btn').click()
            toggle.click()
            self.assertFalse(self.visible('series', 1), 'Unread-only browsing must hide completed series')
            self.assertTrue(self.visible('series', 2), 'Reading items still count as unfinished')
            self.assertTrue(self.visible('series', 3))
            self.assertTrue(self.visible('novel', 101), 'Listed chapters count before download')
            self.assertFalse(self.visible('novel', 102), 'Empty works have no unfinished chapters')
            self.assertEqual(self.driver.find_element('id', 'reading-section').is_displayed(), reading)
            self.assertEqual(self.js("return localStorage.getItem('library-unread-only')"), '1')
            self.assertTrue(self.js("return document.getElementById('filters-btn').classList.contains('text-indigo-400')"))
            self.driver.find_element('id', 'filters-btn').click()
            search = self.driver.find_element('id', 'series-search')
            search.send_keys('Series 01')
            self.wait("!document.querySelector('[data-series-id=\"1\"]').classList.contains('hidden')")
            self.assertFalse(self.js("return document.getElementById('filters-btn').classList.contains('text-indigo-400')"))
            search.send_keys(Keys.CONTROL, 'a', Keys.NULL, Keys.BACKSPACE)
            self.wait("document.querySelector('[data-series-id=\"1\"]').classList.contains('hidden')")
            self.driver.find_element('css selector', '[data-library-filter="novels"]').click()
            self.assertTrue(self.visible('novel', 101))
            self.assertFalse(self.visible('series', 2))
            self.driver.find_element('css selector', '[data-library-filter="all"]').click()
            self.home(width)
            self.assertTrue(self.driver.find_element('id', 'home-unread-only').is_selected())
            self.assertFalse(self.visible('series', 1))
            self.boosted('/series/2')
            self.assertFalse(self.driver.find_element('id', 'item-unread-only').is_selected())
            self.boosted('/')
            self.wait('window.libraryGrid?.isLive()')
            self.assertFalse(self.visible('series', 1))
            self.assertTrue(self.driver.find_element('id', 'home-unread-only').is_selected())
        self.wait("document.getElementById('library-events')?.libraryEventsBound")
        self.post('/series/3/mark-all-read')
        self.wait("document.querySelector('[data-series-id=\"3\"]')?.dataset.unreadCount==='0'")
        self.assertFalse(self.visible('series', 3))
        self.post('/series/3/mark-all-unread')
        self.wait("document.querySelector('[data-series-id=\"3\"]')?.dataset.unreadCount==='1'")
        self.assertTrue(self.visible('series', 3))
        self.post('/novels/chapters/101/read-status', {'read': True})
        self.wait("document.querySelector('[data-novel-id=\"101\"]')?.dataset.unreadCount==='0'")
        self.assertFalse(self.visible('novel', 101))
        self.post('/novels/chapters/101/read-status', {'read': False})
        self.wait("document.querySelector('[data-novel-id=\"101\"]')?.dataset.unreadCount==='1'")
        self.assertTrue(self.visible('novel', 101))
        self.assertTrue(self.driver.execute_async_script(
            'const done=arguments[0];libraryResync().then(()=>done(true),()=>done(false))'))
        self.wait("document.querySelector('[data-series-id=\"1\"]').classList.contains('hidden')")
        self.assertTrue(self.driver.find_element('id', 'home-unread-only').is_selected())
        self.assertEqual(self.js('return audit.errors'), [])

    def test_saved_unread_rule_in_inline_restoration(self):
        self.home(320)
        self.js("localStorage.clear();localStorage.setItem('library-unread-only','1')")
        # Execute the production inline restoration by itself. Leaving home.js
        # out of this call makes a missing pre-paint rule observable even if
        # the deferred script would subsequently repair it.
        self.js("""const script=[...document.scripts].find(s=>s.textContent.includes('Pre-applies EVERY saved filter'));
            document.querySelectorAll('.series-card').forEach(c=>c.classList.remove('hidden'));
            document.getElementById('home-unread-only').checked=false;
            (0,eval)(script.textContent);""")
        self.assertFalse(self.visible('series', 1), 'Inline restoration must hide completed series')
        self.assertTrue(self.visible('series', 2))
        self.assertTrue(self.driver.find_element('id', 'home-unread-only').is_selected())
        self.assertTrue(self.js("return document.getElementById('filters-btn').classList.contains('text-indigo-400')"))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    for name, default in (('chrome-binary', '/usr/bin/chromium'), ('chromedriver', '/usr/bin/chromedriver'),
                          ('firefox-binary', '/usr/bin/firefox'), ('geckodriver', '/usr/bin/geckodriver')):
        parser.add_argument('--' + name, default=default)
    audit.ARGS = parser.parse_args()

    def suite(names=('test_home_unread_browsing', 'test_saved_unread_rule_in_inline_restoration')):
        return unittest.TestSuite(type(browser.title() + 'HomeUnread', (HomeUnreadChecks,),
            {'browser': browser})(name) for browser in selected_browsers(audit.ARGS) for name in names)

    result = unittest.TextTestRunner(verbosity=2).run(suite())
    unittest_failures(result)
    if not baseline_passed(result, 2 * len(selected_browsers(audit.ARGS))):
        sys.exit(1)
    root = Path(__file__).resolve().parents[1]
    for filename, before, after, test, signal in (
        ('app/static/js/home.js', '&& (!unreadOnly || Number(card.dataset.unreadCount || 0) > 0)', '',
         'test_home_unread_browsing', 'Unread-only browsing must hide completed series'),
        ('app/templates/library/index.html', 'const unreadOnly = localStorage.getItem("library-unread-only") === "1";',
         'const unreadOnly = false;', 'test_saved_unread_rule_in_inline_restoration', 'Inline restoration must hide completed series'),
    ):
        source = root / filename
        original = source.read_text()
        if original.count(before) != 1:
            raise ValueError('The unread filter mutation must match exactly once.')
        try:
            source.write_text(original.replace(before, after))
            output = io.StringIO()
            control = unittest.TextTestRunner(stream=output, verbosity=2).run(suite([test]))
        finally:
            source.write_text(original)
        if not mutation_rejected(control, selected_browsers(audit.ARGS), 'HomeUnread', signal):
            print(output.getvalue())
            raise AssertionError('Every selected browser must reject the disabled unread filter rule.')
        print(' and '.join(b.title() for b in selected_browsers(audit.ARGS)) + ' rejected disabled unread filtering in ' + filename, flush=True)
