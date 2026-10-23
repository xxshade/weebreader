"""Native cover deferral, reveal and live updates in both desktop/mobile viewports."""
import argparse
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import sys
import unittest

from selenium.webdriver.common.keys import Keys

import test_audit_browser as audit
from browser_checks import add_browser_argument, selected_browsers, baseline_passed, mutation_rejected
from ci_report import unittest_failures
from test_resync_browser import BrowserChecks


class LazyCovers(audit.AuditChecks):
    @classmethod
    def seed_readers(cls):
        with closing(sqlite3.connect(cls.fixture.data / 'library.db')) as db, db:
            db.execute('UPDATE items SET cover_path=(SELECT cover_path FROM series WHERE series.id=items.series_id)')
            if cls.listing == 'series':
                # Separate URLs are necessary: shared bytes would mask eager loads.
                db.execute("UPDATE items SET series_id=1, title=printf('Volume %02d',id)")
            elif cls.listing == 'reading':
                db.execute("INSERT INTO reading_progress(user_id,item_id,status,progress_percent) SELECT 1,id,'reading',25 FROM items")

    def test_visible_series_covers(self):
        for height, filtered, count in ((800, False, 40), (800, True, 40),
                                        (120, False, 40), (800, False, 32)):
            # Each activation uses cold item URLs; the home keeps its existing
            # series URLs. A cached image needs no request prioritization.
            covers = self.fixture.data / 'covers'
            with closing(sqlite3.connect(self.fixture.data / 'library.db')) as db, db:
                db.execute('DELETE FROM items WHERE id>?', (count,))
                for item_id, old in db.execute('SELECT id,cover_path FROM items').fetchall():
                    name = f'visible-{height}-{int(filtered)}-{count}-{item_id}.webp'
                    (covers / name).write_bytes((covers / old).read_bytes())
                    db.execute('UPDATE items SET cover_path=? WHERE id=?', (name, item_id))
                if filtered:
                    db.execute("INSERT OR REPLACE INTO reading_progress(user_id,item_id,status,progress_percent) "
                               "SELECT 1,id,'completed',100 FROM items WHERE id>=18")
            if self.browser == 'chrome':
                self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
                    {'width': self.width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False})
            else:
                self.driver.browsing_context.set_viewport(context=self.driver.current_window_handle,
                    viewport={'width': self.width, 'height': height})
            self.driver.get(self.base + '/')
            self.wait('window.libraryGrid?.isLive()')
            self.driver.execute_script('''uiPrefs.write('thumbnail-size:series', arguments[0]);
                localStorage.setItem('series-item-sort:1','original-desc');
                localStorage.setItem('series-unread-only:1',arguments[2]);
                localStorage.setItem('series-search:1',arguments[1]);''',
                str(min(280, self.width - 32)), 'Volume 1' if filtered else '', '1' if filtered else '0')
            self.js('''window.coverDecodes=[];
                const decode=HTMLImageElement.prototype.decode;
                HTMLImageElement.prototype.decode=function(...args){
                    coverDecodes.push(this.src);return decode.apply(this,args);
                };''')
            marker = self.js('return audit.document')
            self.boosted('/series/1')
            self.wait("!!document.querySelector('#items-grid img')")
            state = self.js('''return [...document.querySelectorAll('#items-grid img')].map(i=>{
                const r=i.getBoundingClientRect();return {loading:i.loading, priority:i.fetchPriority,
                    item:Number(i.closest('[data-item-id]').dataset.itemId),
                    decoded:coverDecodes.includes(i.src),
                    visible:r.width>0 && r.top<innerHeight && r.bottom>0,
                    top:r.top};
            })''')
            visible = [i for i in state if i['visible']]
            if count == 32:
                self.assertTrue(visible)
                self.assertTrue(all(i['loading'] == 'lazy' and not i['decoded'] for i in state),
                                'Small series must retain native cover scheduling')
            elif height == 800:
                self.assertTrue(visible)
                self.assertEqual(visible[0]['item'], 17 if filtered else 40,
                                 'Cover priority must follow the saved filter and sort')
                self.assertTrue(all(i['loading'] == 'eager' for i in visible),
                                'Visible series covers must start without lazy deferral')
                self.assertTrue(all(i['priority'] == 'high' for i in visible),
                                'Visible series covers must have fetch priority')
                self.assertTrue(all(i['decoded'] for i in visible),
                                'Visible series covers must begin decoding early')
            else:
                self.assertEqual(visible, [])
            self.assertTrue(all(i['loading'] == 'lazy' for i in state if not i['visible']),
                            'Only visible series covers may bypass lazy deferral')
            self.assertFalse(any(i['decoded'] for i in state if not i['visible']),
                             'Only visible series covers may begin decoding early')
            self.assertEqual(self.js('return audit.document'), marker)
            self.assertEqual(self.js('return audit.errors'), [])

    def test_cover_loading(self):
        width = self.width
        if self.browser == 'chrome':
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
                {'width': width, 'height': 700, 'deviceScaleFactor': 1, 'mobile': False})
        else:
            self.driver.browsing_context.set_viewport(context=self.driver.current_window_handle,
                viewport={'width': width, 'height': 700})
        self.assertEqual(self.js('return innerWidth'), width)
        selector = {'home': '#series-grid img', 'reading': '#reading-grid img',
                    'series': '#items-grid img'}[self.listing]
        path = '/series/1' if self.listing == 'series' else '/'
        # Large covers put the end beyond either engine's normal prefetch range.
        self.driver.add_cookie({'name': 'thumbnail-size', 'value': str(min(400, width - 32))})
        self.driver.get(self.base + path)
        self.wait(f'document.querySelectorAll({selector!r}).length===40')
        self.wait("document.getElementById('library-events')?.libraryEventsBound")
        # A real visible cover must load without any scrolling or forced fetch.
        self.wait(f'''[...document.querySelectorAll({selector!r})].some(i=>{{
            const r=i.getBoundingClientRect();return r.top<innerHeight && r.bottom>0 && i.complete && i.naturalWidth>0;
        }})''')
        # Let native eager transfers settle; only the native loading policy differs
        # in the controls. No network interception or disabled cache is involved.
        self.driver.execute_async_script('const done=arguments[0];setTimeout(done,500)')
        state = self.js(f'''return [...document.querySelectorAll({selector!r})].map(i=>({{
            loaded:i.complete && i.naturalWidth>0, top:i.getBoundingClientRect().top,
            width:i.parentElement.getBoundingClientRect().width,
            height:i.parentElement.getBoundingClientRect().height
        }}))''')
        initial = sum(row['loaded'] for row in state)
        self.assertTrue(any(not row['loaded'] and row['top'] > 7000 for row in state),
                        'Distant covers must remain deferred')
        self.assertTrue(all(abs(row['height'] / row['width'] - 1.5) < .02 for row in state),
                        'Deferred covers must reserve their full layout space')
        self.assertLess((self.fixture.fixture / 'server.log').read_text().count('"GET /covers/'), 40,
                        'Distant covers must remain deferred')
        if self.listing == 'home':
            # Reveal a still-cold distant cover through a real search, not scroll.
            search = self.driver.find_element('id', 'series-search')
            search.send_keys('Series 40')
            self.wait("[...document.querySelectorAll('.series-card')].filter(c=>c.getClientRects().length).length===1")
            self.wait("document.querySelector('[data-series-id=\"40\"] img').naturalWidth>0")
            search.send_keys(Keys.CONTROL, 'a', Keys.NULL, Keys.BACKSPACE)
            self.driver.find_element('css selector', '[data-library-filter="novels"]').click()
            self.driver.find_element('css selector', '[data-library-filter="all"]').click()
            self.wait("document.querySelector('[data-series-id=\"40\"]').getClientRects().length>0")
        before = self.driver.find_elements('css selector', selector)[-1].rect
        BrowserChecks.load_all_covers(self, selector)
        self.assertTrue(self.js(f'return [...document.querySelectorAll({selector!r})].every(i=>i.complete && i.naturalWidth>0)'))
        after = self.driver.find_elements('css selector', selector)[-1].rect
        for key in ('width', 'height', 'y'):
            self.assertAlmostEqual(before[key], after[key], delta=1)
        requests = (self.fixture.fixture / 'server.log').read_text().count('"GET /covers/')
        marker = self.js('return audit.document')
        self.assertTrue(self.driver.execute_async_script(
            'const done=arguments[0];libraryResync().then(()=>done(true),()=>done(false))'))
        BrowserChecks.load_all_covers(self, selector)
        self.assertEqual(self.js('return audit.document'), marker)
        self.assertEqual((self.fixture.fixture / 'server.log').read_text().count('"GET /covers/'), requests,
                         'Morph must reuse covers already visited')
        if self.listing == 'home':
            self.js('window.oldCover=document.querySelector(\'[data-series-id="40"] img\')')
            result = self.driver.execute_async_script('''const done=arguments[0];
                fetch('/series/40/mark-all-read',{method:'POST'}).then(r=>done(r.ok));''')
            self.assertTrue(result)
            self.wait('document.querySelector(\'[data-series-id="40"] img\')!==window.oldCover')
            self.assertEqual(self.js('return document.querySelector(\'[data-series-id="40"] img\').loading'),
                             'lazy', 'Live cards must retain native cover deferral')
        self.assertEqual(self.js('return audit.errors'), [])
        print(f'{self.browser} {width}px {self.listing}: {initial}/40 initially loaded; all load on reveal; morph reuses cache', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    for name, default in (('chrome-binary', '/usr/bin/chromium'), ('chromedriver', '/usr/bin/chromedriver'),
                          ('firefox-binary', '/usr/bin/firefox'), ('geckodriver', '/usr/bin/geckodriver')):
        parser.add_argument('--' + name, default=default)
    audit.ARGS = parser.parse_args()

    def suite(listings=('home', 'reading', 'series'), widths=(1280, 320)):
        return unittest.TestSuite(type(browser.title() + 'LazyCovers', (LazyCovers,),
            {'browser': browser, 'width': width, 'listing': listing})('test_cover_loading')
            for browser in selected_browsers(audit.ARGS) for width in widths for listing in listings)

    def visible_suite(widths=(1280, 320)):
        return unittest.TestSuite(type(browser.title() + 'VisibleCovers', (LazyCovers,),
            {'browser': browser, 'width': width, 'listing': 'series'})('test_visible_series_covers')
            for browser in selected_browsers(audit.ARGS) for width in widths)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([suite(), visible_suite()]))
    unittest_failures(result)
    if not baseline_passed(result, 8 * len(selected_browsers(audit.ARGS))):
        sys.exit(1)
    root = Path(__file__).resolve().parents[1]
    for filename, before, after, listing, signal in (
        ('app/templates/library/index.html', 'loading="lazy"', 'loading="eager"', 'home', 'Distant covers must remain deferred'),
        ('app/templates/library/_reading_grid.html', 'loading="lazy"', 'loading="eager"', 'reading', 'Distant covers must remain deferred'),
        ('app/templates/library/_item_card.html', 'loading="lazy"', 'loading="eager"', 'series', 'Distant covers must remain deferred'),
        ('app/static/js/library-events.js', 'img.loading = "lazy";', 'img.loading = "eager";', 'home', 'Live cards must retain native cover deferral'),
    ):
        source = root / filename
        original = source.read_text()
        if original.count(before) != 1:
            raise ValueError('Cover mutation must match exactly once')
        try:
            source.write_text(original.replace(before, after))
            output = io.StringIO()
            control = unittest.TextTestRunner(stream=output, verbosity=2).run(suite((listing,), (320,)))
        finally:
            source.write_text(original)
        if not mutation_rejected(control, selected_browsers(audit.ARGS), 'LazyCovers', signal):
            print(output.getvalue())
            raise AssertionError('Every selected browser must reject eager cover loading')
        print('Selected browsers rejected eager cover loading in ' + filename, flush=True)
    source = root / 'app/static/js/thumbnail-size.js'
    for before, after, signal in (
        ('image.loading = "eager";', '', 'Visible series covers must start without lazy deferral'),
        ('image.fetchPriority = "high";', '', 'Visible series covers must have fetch priority'),
        ('image.decode().catch(() => {});', '', 'Visible series covers must begin decoding early'),
        ('grid.childElementCount <= 32', 'false', 'Small series must retain native cover scheduling'),
        ('if (rect.top >= innerHeight) break;', '', 'Only visible series covers may bypass lazy deferral'),
    ):
        original = source.read_text()
        if original.count(before) != 1:
            raise ValueError('Visible cover mutation must match exactly once')
        try:
            source.write_text(original.replace(before, after))
            output = io.StringIO()
            control = unittest.TextTestRunner(stream=output, verbosity=2).run(visible_suite((320,)))
        finally:
            source.write_text(original)
        if not mutation_rejected(control, selected_browsers(audit.ARGS), 'VisibleCovers', signal):
            print(output.getvalue())
            raise AssertionError('Every selected browser must reject broken visible-cover priority')
        print('Selected browsers rejected broken visible-cover priority: ' + signal, flush=True)
