"""Press-only home detail preloads, reuse and invalidation in both engines."""
import argparse
import io
from pathlib import Path
import sqlite3
import sys
import unittest

import requests

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput

import test_audit_browser as audit
from browser_checks import add_browser_argument, selected_browsers, baseline_passed, mutation_rejected
from ci_report import unittest_failures


class HomePreload(audit.AuditChecks):
    # Installed before navigation: the stream can connect before home() runs.
    probe = "() => { (" + audit.PROBE + """)();
        window.preloadStreamConnections = 0;
        document.addEventListener('htmx:sse:after:connection', () => preloadStreamConnections++);
    }"""

    def home(self):
        self.driver.get(self.base + '/')
        self.wait("window.libraryGrid?.isLive() && document.getElementById('library-events')?.libraryEventsBound && window.preloadStreamConnections > 0")
        if self.browser == 'chrome':
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
                {'width': self.width, 'height': 800, 'deviceScaleFactor': 1, 'mobile': False})
        else:
            self.driver.browsing_context.set_viewport(context=self.driver.current_window_handle,
                viewport={'width': self.width, 'height': 800})
        self.assertEqual(self.js('return innerWidth'), self.width)
        self.js('''window.preloadProbe={requests:0,hold:false,fault:null,accountChanges:0};
            document.getElementById('library-events').addEventListener('account_updated',()=>preloadProbe.accountChanges++);
            document.addEventListener('load',e=>{
                if(e.target.tagName==='SCRIPT' && e.target.src.includes('/js/appearance.js'))
                    preloadProbe.appearanceLoaded=true;
            },true);
            document.addEventListener('click',e=>{
                if(e.target.closest?.('#series-grid a, #user-menu-dropdown a')) preloadProbe.clicked=true;
            },true);
            document.addEventListener('htmx:after:request',e=>{
                if(e.detail.ctx.sourceElement.closest?.('#series-grid a, #user-menu-dropdown a')) preloadProbe.finished=true;
            });
            for(const name of ['mousedown','touchstart']) document.addEventListener(name,e=>{
                const card=e.target.closest?.('#series-grid a, #user-menu-dropdown a');
                if(card) {preloadProbe.entry=card._htmx?.preload;preloadProbe.pressedWithCache=!!preloadProbe.entry;}
            });
            const original=fetch;
            window.fetch=async(url,options)=>{
                if(!/^\\/(series\\/[12]|novels\\/100|stats|settings)$/.test(String(url))) return original(url,options);
                const probe=preloadProbe;probe.requests++;
                if(!probe.clicked) probe.speculated=true;
                const fault=probe.fault;probe.fault=null;
                if(fault==='network') throw new TypeError('simulated preload connection loss');
                if(fault==='http') return new Response('<p>Unavailable</p>',{status:503,headers:{'Content-Type':'text/html'}});
                if(fault==='content') return new Response('{}',{headers:{'Content-Type':'application/json'}});
                const response=await original(url,options);
                if(probe.hold) {
                    const mode=probe.hold;probe.hold=false;
                    const html=await response.text();
                    probe.held=true;
                    if(mode==='body') return new Response(new ReadableStream({start(controller){
                        probe.release=()=>{controller.enqueue(new TextEncoder().encode(html));controller.close()};
                    },pull(){probe.bodyRead=true;}},{highWaterMark:0}),{status:response.status,headers:response.headers});
                    await new Promise(resolve=>probe.release=resolve);
                    return new Response(html,{status:response.status,headers:response.headers});
                }
                return response;
            };''')

    def card(self, kind='series', number=1):
        card = self.driver.find_element('css selector', f'#series-grid [data-{kind}-id="{number}"]')
        self.driver.execute_script('arguments[0].scrollIntoView({block:"center"})', card)
        return card

    def pointer(self):
        kind = 'touch' if self.width == 320 else 'mouse'
        return ActionBuilder(self.driver, mouse=PointerInput(kind, 'card-' + kind), duration=0)

    def down(self, card):
        pointer = self.pointer()
        pointer.pointer_action.move_to(card)
        pointer.pointer_action.pointer_down()
        if self.width == 320:
            # Keep the touch sequence together: Chrome's driver does not
            # reliably deliver touchend across separate perform-actions calls.
            pointer.pointer_action.pause(.06)
            pointer.pointer_action.pointer_up()
        pointer.perform()

    def up(self):
        if self.width == 320:
            return
        pointer = self.pointer()
        pointer.pointer_action.pointer_up()
        pointer.perform()

    def arrived(self, path, signal='Opening a card must reuse its preload'):
        try:
            if path in ('/stats', '/settings'):
                marker = "!!document.querySelector('main .viz')" if path == '/stats' else "!!document.getElementById('profile')"
            else:
                marker = "document.getElementById('series-page')?.dataset.seriesId===" + repr(path.rsplit('/', 1)[-1]) if path.startswith('/series/') else "document.querySelector('main')?.dataset.novelId==='100'"
            self.wait('location.pathname===' + repr(path) + ' && (' + marker + ')')
            if path == '/settings':
                # A body swap exposes the default-valued select before its
                # external script runs, even when that script is cached.
                self.appearance_ready()
        except TimeoutException:
            self.fail(signal)

    def test_press_scope_and_reuse(self):
        for kind, number, path in [('series', 1, '/series/1'), ('novel', 100, '/novels/100')]:
            self.home()
            card = self.card(kind, number)
            ActionChains(self.driver).move_to_element(card).perform()
            self.driver.execute_script('arguments[0].focus()', card)
            self.frames()
            self.assertEqual(self.js('return preloadProbe.requests'), 0, 'Hover and focus must not preload')
            self.assertFalse(self.js("return [...document.querySelectorAll('#reading-grid a, #search-items a')].some(a=>a.hasAttribute('hx-preload'))"))
            self.down(card)
            self.frames()
            self.assertEqual(self.js('return preloadProbe.requests'), 1, 'Pressing a detail card must preload')
            self.assertTrue(self.js('return !!preloadProbe.pressedWithCache'), 'Pressing a detail card must preload')
            if self.width != 320:
                self.assertEqual(self.driver.current_url, self.base + '/')
            self.assertFalse(self.js("return !!document.querySelector('#reader-root')"))
            self.up()
            self.arrived(path)
            self.assertEqual(self.js('return preloadProbe.requests'), 1, 'Opening a card must reuse its preload')
            self.assertEqual(self.js('return audit.errors'), [])

    def test_failed_and_expired_preloads(self):
        for fault in ('http', 'network', 'content'):
            self.home()
            self.js('preloadProbe.fault=' + repr(fault))
            self.down(self.card())
            self.wait('preloadProbe.requests>=1')
            self.up()
            self.wait('preloadProbe.requests===2 || preloadProbe.finished')
            self.assertEqual(self.js('return preloadProbe.requests'), 2, 'Failed preloads must fall back to a fresh request')
            self.arrived('/series/1', 'Failed preloads must fall back to a fresh request')
            self.assertEqual(self.js('return preloadProbe.requests'), 2,
                             'Failed preloads must fall back to a fresh request')
            self.assertEqual(self.js('return audit.errors'), [])
        self.home()
        self.js("preloadProbe.hold='body'")
        self.down(self.card())
        self.wait('preloadProbe.held')
        self.up()
        self.wait('preloadProbe.finished || !document.querySelector(\'#series-grid [data-series-id="1"]\')._htmx.preload')
        self.js('preloadProbe.entry.expiresAt=0;preloadProbe.release()')
        self.arrived('/series/1')
        self.assertEqual(self.js('return preloadProbe.requests'), 1,
                         'An accepted preload must not expire during its body transfer')
        self.home()
        # A canceled press keeps the page open, like touching then scrolling.
        self.driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0}))", self.card())
        self.wait('preloadProbe.requests===1')
        self.js('document.querySelector(\'#series-grid [data-series-id="1"]\')._htmx.preload.expiresAt=0')
        self.down(self.card())
        self.wait('preloadProbe.requests===2')
        self.up()
        self.arrived('/series/1')
        self.assertEqual(self.js('return preloadProbe.requests'), 2)

    def test_changes_while_navigation_is_pending(self):
        self.home()
        self.js("preloadProbe.hold='body'")
        self.down(self.card())
        self.wait('preloadProbe.held')
        # Commit the real click while its already-rendered response is held.
        self.up()
        with sqlite3.connect(self.fixture.data / 'library.db') as db:
            db.execute("UPDATE series SET name='New detail title' WHERE id=1")
        seen = self.js('return window.libraryEventsSeen || 0')
        result = self.driver.execute_async_script('''const done=arguments[0];
            fetch('/series/2/mark-all-read',{method:'POST'}).then(r=>done(r.ok));''')
        self.assertTrue(result)
        self.wait(f'(window.libraryEventsSeen || 0)>{seen}')
        self.js('preloadProbe.release()')
        self.arrived('/series/1')
        self.assertIn('New detail title', self.driver.title,
                      'A changed library must discard a pending stale preload')
        self.assertEqual(self.js('return preloadProbe.requests'), 2)
        self.home()
        self.driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0}))", self.card())
        self.wait('preloadProbe.requests===1')
        self.assertTrue(self.driver.execute_async_script('const done=arguments[0];libraryResync().then(()=>done(true),()=>done(false))'))
        self.assertFalse(self.js('return !!document.querySelector(\'#series-grid [data-series-id="1"]\')._htmx.preload'),
                         'Resync must discard preloaded details')
        self.js('window.previousCard=document.querySelector(\'#series-grid [data-series-id="2"]\')')
        self.assertTrue(self.driver.execute_async_script("const done=arguments[0];fetch('/series/2/mark-all-unread',{method:'POST'}).then(r=>done(r.ok))"))
        self.wait('document.querySelector(\'#series-grid [data-series-id="2"]\')!==window.previousCard')
        before = self.js('return preloadProbe.requests')
        self.down(self.card(number=2))
        self.up()
        self.arrived('/series/2')
        self.assertEqual(self.js('return preloadProbe.requests'), before + 1, 'Live cards must retain press preloads')
        self.assertEqual(self.js('return audit.errors'), [])

    def account_link(self, path):
        self.wait('window.userMenuBound')
        self.driver.find_element('id', 'user-menu-btn').click()
        return self.driver.find_element('css selector', '#user-menu-dropdown a[href="' + path + '"]')

    def test_account_press_and_failed_response_fallback(self):
        for path in ('/stats', '/settings'):
            for fault in (None, 'http', 'network', 'content'):
                self.home()
                link = self.account_link(path)
                ActionChains(self.driver).move_to_element(link).perform()
                self.driver.execute_script('arguments[0].focus()', link)
                self.frames()
                self.assertEqual(self.js('return preloadProbe.requests'), 0,
                                 'Account links must preload only on press')
                marker = self.js('return audit.document')
                self.js('preloadProbe.fault=' + repr(fault).replace('None', 'null'))
                self.down(link)
                self.assertTrue(self.js('return !!preloadProbe.speculated'),
                                f'Pressing an account link must preload ({path}, {fault})')
                self.up()
                self.arrived(path)
                self.assertEqual(self.js('return preloadProbe.requests'), 1 if fault is None else 2,
                                 'Account navigation must reuse or retry its preload exactly once')
                self.assertEqual(self.js('return audit.document'), marker)
                self.assertEqual(self.js('return audit.errors'), [])

    def test_account_changes_discard_a_response_during_its_body(self):
        signal = 'Committed account changes must invalidate anticipated pages'
        with requests.Session() as other:
            other.post(self.base + '/login', data={'username': 'probe',
                'password': 'disposable-browser-test-password'}).raise_for_status()
            for path in ('/stats', '/settings'):
                self.home()
                link = self.account_link(path)
                self.js("preloadProbe.hold='body'")
                self.down(link)
                self.wait('preloadProbe.held')
                self.up()
                # Consume the held response before mutating; an invalidated
                # press could otherwise navigate before the write commits.
                self.wait('preloadProbe.bodyRead === true')
                seen = self.js('return preloadProbe.accountChanges')
                if path == '/stats':
                    response = other.post(self.base + '/reading-time', json={
                        'spans': [{'item_id': 1, 'page_index': 0, 'seconds': 5, 'ago': 0}]})
                    self.assertEqual(response.json(), {'stored': 1})
                else:
                    response = other.post(self.base + '/settings/text-time-cap',
                        data={'cap_seconds': 240}, headers={'HX-Request': 'true'})
                response.raise_for_status()
                try:
                    self.wait(f'preloadProbe.accountChanges>{seen}')
                except TimeoutException:
                    self.fail(signal)
                self.js('preloadProbe.release()')
                self.arrived(path)
                self.assertEqual(self.js('return preloadProbe.requests'), 2, signal)
                if path == '/stats':
                    self.assertIn('Time read', self.js('return document.querySelector("main").textContent'), signal)
                    self.assertNotIn('Nothing recorded yet.', self.js('return document.querySelector("main").textContent'), signal)
                else:
                    self.assertEqual(self.js('return document.getElementById("text-cap-seconds").value'), '240', signal)

    def test_device_preferences_and_reconnection_discard_account_preloads(self):
        self.home()
        self.delay_appearance()
        link = self.account_link('/settings')
        self.driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0}))", link)
        self.wait('preloadProbe.requests===1')
        self.js("uiPrefs.write('app-theme','amoled')")
        self.assertFalse(self.driver.execute_script('return !!arguments[0]._htmx.preload', link),
                         'Device preference changes must discard anticipated pages')
        self.down(link)
        self.up()
        self.arrived('/settings')
        self.assertEqual(self.js('return document.documentElement.dataset.appTheme'), 'amoled',
                         'AMOLED must already be present in the document')
        self.assertEqual(self.js('return preloadProbe.requests'), 2)
        self.home()
        self.delay_appearance()
        link = self.account_link('/settings')
        self.driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0}))", link)
        self.wait('preloadProbe.requests===1')
        # Cookies are shared with other tabs; this bypasses the local preference helper.
        self.driver.add_cookie({'name': 'app-theme', 'value': 'dark', 'path': '/'})
        self.down(link)
        self.up()
        self.arrived('/settings')
        self.assertEqual(self.js('return document.documentElement.dataset.appTheme'), 'amoled',
                         'Changed device cookies must not reuse old HTML')
        self.assertEqual(self.js('return preloadProbe.requests'), 2,
                         'Changed device cookies must not reuse old HTML')
        self.home()
        link = self.account_link('/stats')
        self.driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0}))", link)
        self.wait('preloadProbe.requests===1')
        self.js("document.dispatchEvent(new CustomEvent('htmx:sse:error'))")
        self.assertFalse(self.driver.execute_script('return !!arguments[0]._htmx.preload', link),
                         'Reconnection must discard anticipated pages')
        self.down(link)
        self.up()
        self.arrived('/stats')
        self.assertEqual(self.js('return preloadProbe.requests'), 2)

    def appearance_ready(self):
        self.wait('preloadProbe.appearanceLoaded===true')

    def delay_appearance(self):
        # Force the ordering from the release failure: HTML is available while
        # its external appearance script is still pending. This delay is a
        # controlled fault, not a sleep used to synchronize assertions.
        self.js('''const replace=Element.prototype.replaceWith;
            Element.prototype.replaceWith=function(...nodes){
                if(nodes[0]?.tagName==='SCRIPT' && nodes[0].src.includes('/js/appearance.js')) {
                    setTimeout(()=>replace.apply(this,nodes),750);
                } else replace.apply(this,nodes);
            };''')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    for name, default in [('chrome-binary', '/usr/bin/chromium'), ('chromedriver', '/usr/bin/chromedriver'),
                          ('firefox-binary', '/usr/bin/firefox'), ('geckodriver', '/usr/bin/geckodriver')]:
        parser.add_argument('--' + name, default=default)
    audit.ARGS = parser.parse_args()
    names = [name for name in HomePreload.__dict__ if name.startswith('test_')]

    def suite(tests=names, widths=(1280, 320)):
        return unittest.TestSuite(type(browser.title() + 'HomePreload' + str(width), (HomePreload,),
            {'browser': browser, 'width': width})(test)
            for browser in selected_browsers(audit.ARGS) for width in widths for test in tests)

    result = unittest.TextTestRunner(verbosity=2).run(suite())
    unittest_failures(result)
    if not baseline_passed(result, 2 * len(names) * len(selected_browsers(audit.ARGS))):
        sys.exit(1)
    root = Path(__file__).resolve().parents[1]
    for filename, before, after, test, signal in [
        ('app/templates/library/index.html', 'hx-preload="home-card-press"', '',
         'test_press_scope_and_reuse', 'Pressing a detail card must preload'),
        ('app/static/js/home-preload.js', 'response.ok && !response.redirected', '!response.redirected',
         'test_failed_and_expired_preloads', 'Failed preloads must fall back to a fresh request'),
        ('app/static/js/home-preload.js', 'generation++;', 'generation += 0;',
         'test_changes_while_navigation_is_pending', 'A changed library must discard a pending stale preload'),
        ('app/static/js/home-preload.js', 'await response.clone().arrayBuffer();', '',
         'test_changes_while_navigation_is_pending', 'A changed library must discard a pending stale preload'),
        ('app/static/js/home-preload.js', 'if (current()) return response;',
         'if (current() && Date.now() < saved.entry.expiresAt) return response;',
         'test_failed_and_expired_preloads', 'An accepted preload must not expire during its body transfer'),
        ('app/templates/base.html', '<a href="/stats" hx-preload="home-card-press"', '<a href="/stats"',
         'test_account_press_and_failed_response_fallback', 'Pressing an account link must preload'),
        ('app/account_events.py', "events.publish({'type': 'account_updated'})", 'pass',
         'test_account_changes_discard_a_response_during_its_body', 'Committed account changes must invalidate anticipated pages'),
        ('app/static/js/ui-prefs.js', 'window.homePreload?.invalidate();', '',
         'test_device_preferences_and_reconnection_discard_account_preloads', 'Device preference changes must discard anticipated pages'),
        ('app/static/js/home-preload.js', ' && saved.cookies === document.cookie', '',
         'test_device_preferences_and_reconnection_discard_account_preloads', 'Changed device cookies must not reuse old HTML'),
    ]:
        source = root / filename
        original = source.read_text()
        if original.count(before) != (2 if filename.endswith('ui-prefs.js') else 1):
            raise ValueError('Preload mutation must match exactly once')
        try:
            source.write_text(original.replace(before, after))
            output = io.StringIO()
            control = unittest.TextTestRunner(stream=output, verbosity=2).run(suite([test], (320,)))
        finally:
            source.write_text(original)
        if not mutation_rejected(control, selected_browsers(audit.ARGS), 'HomePreload', signal):
            print(output.getvalue())
            raise AssertionError('Every selected browser must reject the broken preload rule')
        print('Selected browsers rejected broken preload behavior: ' + test, flush=True)
