"""Real Android Vivaldi/Firefox regressions on a disposable application and AVD.

CI runs both matrix entries without fail-fast. Missing packages, wrong versions,
failed driver sessions and zero-test runs fail; desktop emulation is not used.
"""
import argparse
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import traceback
import unittest
import uuid
import zipfile
from xml.etree import ElementTree

import requests
from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.support.ui import Select, WebDriverWait

import test_audit_browser as audit
from test_pagination_browser import PaginationChecks, WORDS
from test_header_browser import check_compact_header
from test_reading_statistics_browser import READING_CHECKS
from android_input import NativeInput
from browser_versions import require_version
from ci_report import failure
from provision_android_browsers import installed_version, adb


class AndroidChecks(PaginationChecks):
    @classmethod
    def seed_readers(cls):
        super().seed_readers()
        with sqlite3.connect(cls.fixture.data / 'library.db') as db:
            archive = db.execute('SELECT file_path FROM items WHERE id=1').fetchone()[0]
            with zipfile.ZipFile(archive, 'a') as book:
                for n in range(3):
                    out = io.BytesIO()
                    Image.new('RGB', (320, 480), (40 + n * 60, 90, 100)).save(out, 'PNG')
                    book.writestr(f'{n:03}.png', out.getvalue())

    def setUp(self):
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            print(f'::notice title=Android test started::{self.browser}: {self._testMethodName}', flush=True)
        self.package = 'com.vivaldi.browser' if self.browser == 'vivaldi' else 'org.mozilla.firefox'
        evidence = json.loads(ARGS.installation.read_text())
        self.activity = evidence['activity']
        if evidence['browser'] != self.browser:
            raise ValueError('Installation evidence belongs to another browser.')
        versions = json.loads(ARGS.versions.read_text())['versions']
        expected = versions[self.browser + '_android']
        require_version(installed_version(self.package), expected)
        if self._testMethodName == 'test_compact_header_and_panels':
            density = adb('shell', 'wm', 'density')
            size = adb('shell', 'wm', 'size')
            previous_ime = adb('shell', 'settings', 'get', 'secure', 'show_ime_with_hard_keyboard')

            def restore_display():
                adb('shell', 'input', 'keyevent', '111')
                for option, previous in (('size', size), ('density', density)):
                    override = re.search(r'Override (?:size|density): (\S+)', previous)
                    adb('shell', 'wm', option, override[1] if override else 'reset')
                if previous_ime == 'null':
                    adb('shell', 'settings', 'delete', 'secure', 'show_ime_with_hard_keyboard')
                else:
                    adb('shell', 'settings', 'put', 'secure', 'show_ime_with_hard_keyboard', previous_ime)

            self.addCleanup(restore_display)
            adb('shell', 'settings', 'put', 'secure', 'show_ime_with_hard_keyboard', '1')
            adb('shell', 'wm', 'density', '320')
            adb('shell', 'wm', 'size', '640x1600')
        adb('reverse', f'tcp:{self.fixture.port}', f'tcp:{self.fixture.port}')
        self.addCleanup(lambda: adb('reverse', '--remove', f'tcp:{self.fixture.port}'))
        if self.browser == 'vivaldi':
            self.deny_notifications()
            options = ChromeOptions()
            options.add_experimental_option('androidPackage', self.package)
            options.add_experimental_option('androidDeviceSerial', ARGS.serial)
            options.add_experimental_option('androidActivity', evidence['activity'])
            # ChromeDriver's custom-package launch uses a different command-line
            # filename. Start the verified browser explicitly with Chromium's
            # Android filename, then attach to its observed DevTools socket.
            adb('shell', 'am', 'force-stop', self.package)
            adb('shell', 'am', 'set-debug-app', '--persistent', self.package)
            adb('shell', 'echo chrome --no-first-run --disable-fre --enable-automation --remote-debugging-socket-name=ebookreader_devtools_remote > /data/local/tmp/chrome-command-line')
            adb('shell', 'am', 'start', '-W', '-n', self.package + '/' + evidence['activity'],
                '-a', 'android.intent.action.VIEW', '-d', self.base + '/login')

            def socket_name(_):
                available = re.findall(r'@(\S*devtools_remote)\b', adb('shell', 'cat', '/proc/net/unix'))
                candidates = set(available) & {'ebookreader_devtools_remote', 'vivaldi_devtools_remote', 'chrome_devtools_remote'}
                if len(candidates) > 1:
                    raise ValueError('Multiple browser DevTools sockets; refusing an ambiguous attachment.')
                return next(iter(candidates), None)

            options.add_experimental_option('androidExecName', 'chrome')
            options.add_experimental_option('androidDeviceSocket', WebDriverWait(None, 60).until(socket_name))
            options.add_experimental_option('androidUseRunningApp', True)
            # Vivaldi brands its CDP product version. The driver was selected
            # from its signed Chromium library, checked independently below.
            self.driver = webdriver.Chrome(options=options, service=ChromeService(ARGS.chromedriver,
                service_args=['--disable-build-check', '--verbose'],
                log_output=str(ARGS.output / 'chromedriver-android.log')))
            self.driver.execute_cdp_cmd('Page.bringToFront', {})
            self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': '(' + audit.PROBE + ')()'})
        else:
            options = FirefoxOptions()
            options.enable_bidi = True
            self.driver = self.start_firefox(options)
            self.driver.script.add_preload_script(audit.PROBE)
        self.addCleanup(self.driver.quit)
        self.driver.set_script_timeout(60)
        self.driver.set_page_load_timeout(60)
        actual = self.driver.capabilities['browserVersion']
        if actual not in (expected, evidence['engine_version']):
            raise ValueError('Running browser version differs from its verified installation.')
        if self.browser == 'vivaldi':
            driver_version = self.driver.capabilities.get('chrome', {}).get('chromedriverVersion', '').split(' ')[0]
            if driver_version.split('.')[:3] != evidence['engine_version'].split('.')[:3]:
                raise ValueError('ChromeDriver build differs from Vivaldi\'s verified Chromium build.')
        session = requests.Session()
        response = session.post(self.base + '/login', data={
            'username': 'probe', 'password': 'disposable-browser-test-password'})
        response.raise_for_status()
        self.driver.get(self.base + '/login')
        for name, value in session.cookies.items():
            self.driver.add_cookie({'name': name, 'value': value})
        self.wait("document.visibilityState==='visible'")
        self.assertIn('Android', self.js('return navigator.userAgent'))
        self.assertGreater(self.js('return navigator.maxTouchPoints'), 0)
        evidence = {'browser': self.browser, 'version': expected,
                    'android': adb('shell', 'getprop', 'ro.build.version.release'),
                    'user_agent': self.js('return navigator.userAgent'), 'test': self._testMethodName}
        with (ARGS.output / 'executions.jsonl').open('a') as stream:
            stream.write(json.dumps(evidence) + '\n')

    def tearDown(self):
        self.assertEqual(self.js('return window.audit?.errors'), [])

    def deny_notifications(self):
        # A delayed native notification permission dialog can cover the app
        # after launch. Keep the disposable browser's permission denied so
        # touch/keyboard checks exercise the application, not that dialog.
        if int(adb('shell', 'getprop', 'ro.build.version.sdk')) >= 33:
            adb('shell', 'pm', 'revoke', self.package, 'android.permission.POST_NOTIFICATIONS')
            adb('shell', 'pm', 'set-permission-flags', self.package,
                'android.permission.POST_NOTIFICATIONS', 'user-set', 'user-fixed')

    def start_firefox(self, options):
        # GeckoView's supported configuration file starts the real Android app.
        # Geckodriver then attaches through forwarded Marionette/BiDi ports.
        # This avoids its fixed 5s deadline around Android's am start -W.
        with socket.socket() as marionette_socket, socket.socket() as bidi_socket:
            marionette_socket.bind(('127.0.0.1', 0))
            bidi_socket.bind(('127.0.0.1', 0))
            marionette = marionette_socket.getsockname()[1]
            bidi = bidi_socket.getsockname()[1]
        token = uuid.uuid4().hex
        app_data = '/data/user/0/' + self.package
        root = app_data + '/files/ebookreader-' + token
        staged_prefs = '/data/local/tmp/ebookreader-' + token + '.js'
        profile = root + '/profile'
        config_path = '/data/local/tmp/org.mozilla.firefox-geckoview-config.yaml'

        def cleanup():
            adb('shell', 'am', 'force-stop', self.package)
            adb('shell', 'am', 'clear-debug-app')
            adb('shell', 'su', '0', 'rm', '-rf', root)
            adb('shell', 'rm', '-f', config_path, staged_prefs)
            for port in (marionette, bidi):
                adb('forward', '--remove', f'tcp:{port}')

        adb('shell', 'am', 'force-stop', self.package)
        adb('shell', 'pm', 'clear', self.package)
        self.deny_notifications()
        uid = adb('shell', 'su', '0', 'stat', '-c', '%u', app_data)
        context = adb('shell', 'su', '0', 'ls', '-Zd', app_data).split()[0]
        if not uid.isdecimal() or int(uid) < 10000 or not re.fullmatch(r'u:object_r:app_data_file:s0(?::c[0-9]+(?:,c[0-9]+)*)?', context):
            raise ValueError('Unexpected Firefox app-data ownership or SELinux context.')
        adb('shell', 'su', '0', 'mkdir', '-p', profile)
        prefs = ARGS.output / 'firefox-user.js'
        prefs.write_text('user_pref("marionette.enabled", true);\nuser_pref("marionette.port", 2829);\nuser_pref("marionette.log.level", "Debug");\n')
        adb('push', str(prefs), staged_prefs)
        adb('shell', 'su', '0', 'cp', staged_prefs, profile + '/user.js')
        # Keep Android's sandbox enabled: the profile belongs to Firefox's
        # private data directory, with that package's UID and SELinux label.
        adb('shell', 'su', '0', 'chown', uid + ':' + uid, app_data + '/files')
        adb('shell', 'su', '0', 'chown', '-R', uid + ':' + uid, root)
        adb('shell', 'su', '0', 'chcon', context, app_data + '/files')
        adb('shell', 'su', '0', 'chcon', '-R', context, root)
        adb('shell', 'su', '0', 'chmod', '700', root, profile)
        config = ARGS.output / 'firefox-native-config.json'
        # JSON is a YAML subset; these are GeckoView's own automation arguments.
        config.write_text(json.dumps({'args': ['--marionette', '--profile', profile,
            '--remote-debugging-port', str(bidi), '--remote-allow-hosts', 'localhost'],
            'env': {'MOZ_CRASHREPORTER_NO_REPORT': '1'}}))
        adb('push', str(config), config_path)
        adb('shell', 'chmod', '644', config_path)
        adb('shell', 'am', 'set-debug-app', '--persistent', self.package)
        adb('forward', f'tcp:{marionette}', 'tcp:2829')
        adb('forward', f'tcp:{bidi}', f'tcp:{bidi}')
        self.addCleanup(cleanup)
        adb('shell', 'am', 'start', '-n', self.package + '/org.mozilla.fenix.IntentReceiverActivity',
            '-a', 'android.intent.action.VIEW', '-d', self.base + '/login',
            '--ez', 'performancetest', 'true', '--ez', 'automationtest', 'true')
        return webdriver.Firefox(options=options, service=FirefoxService(ARGS.geckodriver,
            log_output=str(ARGS.output / 'geckodriver-android.log'), service_args=[
                '--connect-existing', '--marionette-port', str(marionette),
                '--log', 'debug']))

    def wait(self, expression):
        return WebDriverWait(self.driver, 60).until(lambda _: self.js('return ' + expression))

    def touch(self, x, y, end=None, *, duration=250):
        if end:
            NativeInput(self.driver, adb, ARGS.output).swipe((x, y), end, duration)
            return
        actions = ActionBuilder(self.driver, mouse=PointerInput('touch', 'finger'))
        actions.pointer_action.move_to_location(round(x), round(y))
        actions.pointer_action.pointer_down()
        actions.pointer_action.pause(.12)
        actions.pointer_action.release()
        actions.perform()

    def tap(self, selector):
        def ready(driver):
            element = driver.find_element('css selector', selector)
            return element if element.is_displayed() and element.is_enabled() else False

        # Android may dispatch the synthesized click after the touch action
        # returns. Wait for the panel's control before reading its coordinates.
        WebDriverWait(self.driver, 60).until(ready)
        NativeInput(self.driver, adb, ARGS.output).tap(selector)

    def show_controls(self):
        self.js("audit.beforeControls=document.querySelector('#paginated-viewer iframe')")
        self.tap('#paginated-viewer iframe')
        self.wait("document.querySelector('#paginated-viewer iframe') !== audit.beforeControls && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")

    def next_page(self):
        self.js("audit.previousFrame=document.querySelector('#paginated-viewer iframe')")
        self.tap('#btn-next')
        self.wait("[...document.querySelectorAll('#reader-root div')].some(e=>e.textContent==='End of series') || (document.querySelector('#paginated-viewer iframe') !== audit.previousFrame && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible')")

    def native_header_tap(self, selector):
        NativeInput(self.driver, adb, ARGS.output).tap(selector, 'user-menu-btn')

    def test_compact_header_and_panels(self):
        # Change the real Android display, not a desktop viewport or user agent.
        # A native 320px layout is the tightest normal-width acceptance case.
        self.wait('innerWidth===320')

        def keyboard_visible(selector):
            self.wait('document.hasFocus() && document.activeElement.id===' + repr(selector.removeprefix('#')))
            WebDriverWait(self.driver, 20).until(lambda _: re.search(
                r'(?:mInputShown|mIsInputViewShown|isInputViewShown)=true',
                adb('shell', 'dumpsys', 'input_method')),
                message='The native Android keyboard must be visible after touching search')

        check_compact_header(self, self.native_header_tap, keyboard=keyboard_visible)

    def test_touch_pagination_preserves_epub_and_novel_text_and_resume(self):
        for path in ('/reader/100', '/novels/100/read/1/1'):
            with self.subTest(path=path):
                self.open_reader(path)
                first = self.page()
                pages = [first['text']]
                self.assertTrue(first['long'], 'The paragraph must fill the space after the introduction')
                self.assertLess(len(first['long']), len(WORDS))
                self.next_page()
                text = self.page()['text']
                self.assertEqual(self.page()['indent'], '0px')
                self.driver.refresh()
                self.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
                self.assertTrue(self.page()['text'].startswith(text[:60]))
                self.show_controls()
                for _ in range(100):
                    page = self.page()
                    self.assertLessEqual(page['height'], page['viewport'] + 1)
                    pages.append(page['text'])
                    if page['text'].endswith('The final paragraph.'):
                        break
                    self.next_page()
                else:
                    self.fail('Mobile pagination did not reach the end')
                self.assertEqual(''.join(pages), 'A short introduction.' + WORDS + 'The final paragraph.')
                reverse = [self.page()['text']]
                for _ in range(100):
                    if reverse[0].startswith('A short introduction.'):
                        break
                    self.js("audit.previousFrame=document.querySelector('#paginated-viewer iframe')")
                    self.tap('#btn-prev')
                    self.wait("document.querySelector('#paginated-viewer iframe') !== audit.previousFrame && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
                    reverse.insert(0, self.page()['text'])
                self.assertEqual(''.join(reverse), ''.join(pages))
                self.assertEqual(self.js('return audit.errors'), [])

    def test_typography_font_controls_and_scroll_gesture(self):
        self.open_reader('/reader/100')
        self.next_page()
        before = self.page()['text'][:60]
        original_size = self.js("return parseFloat(document.querySelector('#paginated-viewer iframe').contentWindow.getComputedStyle(document.querySelector('#paginated-viewer iframe').contentDocument.body).fontSize)")
        self.tap('#btn-settings')
        self.js("audit.previousFrame=document.querySelector('#paginated-viewer iframe')")
        self.tap('#font-size-inc')
        self.wait("document.querySelector('#paginated-viewer iframe') !== audit.previousFrame && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
        self.assertTrue(self.page()['text'].startswith(before))
        self.assertGreater(self.js("return parseFloat(document.querySelector('#paginated-viewer iframe').contentWindow.getComputedStyle(document.querySelector('#paginated-viewer iframe').contentDocument.body).fontSize)"), original_size, 'The font control must increase the rendered text size')
        Select(self.driver.find_element('id', 'flow-select')).select_by_value('scrolled-doc')
        self.wait("document.querySelector('#custom-scroll iframe')?.contentDocument?.body?.textContent?.includes('word0449')")
        self.tap('#btn-settings')
        size = self.js('return {w:innerWidth,h:innerHeight}')
        before_scroll = self.js("return document.getElementById('custom-scroll').scrollTop")
        self.touch(size['w'] / 2, size['h'] * .75, (size['w'] / 2, size['h'] * .3), duration=700)
        try:
            WebDriverWait(self.driver, 15).until(lambda _: self.js("return document.getElementById('custom-scroll').scrollTop") > before_scroll)
        except TimeoutException:
            pass
        self.assertGreater(self.js("return document.getElementById('custom-scroll').scrollTop"), before_scroll,
                           'A native reading gesture must change the scroll position')
        # Keep the production iframe CSS and measure both flow-root paragraphs.
        metrics = self.driver.execute_async_script("""const done=arguments[0],d=document.querySelector('#custom-scroll iframe').contentDocument;
            d.body.innerHTML='<p id="short" style="display:flow-root">MMMMMMMMMM</p><p id="long" style="display:flow-root">'+('MMMMMMMMMM '.repeat(200))+'</p><h2 style="font-size:2em" id="heading">MMMMMMMMMM</h2>';
            d.body.getBoundingClientRect();d.fonts.ready.then(()=>requestAnimationFrame(()=>requestAnimationFrame(()=>{
                const measure=id=>{const p=d.getElementById(id),r=d.createRange();r.setStart(p.firstChild,0);r.setEnd(p.firstChild,10);return r.getBoundingClientRect().width};
                done({short:measure('short'),long:measure('long'),heading:measure('heading')});})));""")
        self.assertAlmostEqual(metrics['short'], metrics['long'], delta=1)
        self.assertGreater(metrics['heading'], metrics['short'] * 1.8)

    def test_comic_touch_and_resume(self):
        self.js("localStorage.setItem('comic-reader-prefs',JSON.stringify({mode:'single',rtl:false,fit:'height'}))")
        self.driver.get(self.base + '/reader/1')
        self.wait("document.getElementById('page-indicator')?.textContent.includes('1 / 3') && document.getElementById('page-image')?.complete && document.getElementById('page-image').naturalWidth>0")
        self.js("""audit.comicEvents=[];for(const name of ['pointerdown','touchstart','touchend','click']){
            document.addEventListener(name,event=>{const r=document.getElementById('page-single')?.getBoundingClientRect();
                audit.comicEvents.push({name,target:event.target.id||event.target.tagName,
                    x:event.clientX??event.changedTouches?.[0]?.clientX,
                    y:event.clientY??event.changedTouches?.[0]?.clientY,rect:r&&[r.x,r.y,r.width,r.height]});
            },{capture:true,passive:true});}""")
        self.frames()
        rect = self.js("const r=document.getElementById('page-single').getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}")
        self.touch(rect['x'] + rect['w'] * .9, rect['y'] + rect['h'] * .5)
        self.wait("document.getElementById('page-indicator').textContent.includes('2 / 3')")
        self.driver.refresh()
        self.wait("document.getElementById('page-image')?.complete && document.getElementById('page-indicator')?.textContent")
        self.assertIn('2 / 3', self.js("return document.getElementById('page-indicator').textContent"),
                      'Comic resume must restore the touched page')

    def visible_covers_ready(self):
        return "(()=>{const images=[...document.querySelectorAll('.series-card img')].filter(i=>{const r=i.getBoundingClientRect();return r.top<innerHeight && r.bottom>0});return images.length>0 && images.every(i=>i.complete)})()"

    def test_background_restart_reconnects_without_navigation_or_image_fetch(self):
        self.driver.get(self.base + '/')
        self.wait("document.getElementById('library-events')?.libraryEventsBound")
        marker = self.js('return audit.document')
        # Reader fixtures add coverless books above the comic covers. Align a
        # real cover before measuring cache reuse across background/restart.
        self.js("document.querySelector('.series-card img').scrollIntoView({block:'center',behavior:'instant'})")
        self.wait(self.visible_covers_ready())
        count = self.fixture.covers_requested(self.fixture)
        adb('shell', 'input', 'keyevent', 'KEYCODE_HOME')
        self.fixture.stop_server()
        self.fixture.start_server()
        adb('shell', 'am', 'start', '-n', self.package + '/' + self.activity,
            '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER')
        self.wait("document.visibilityState==='visible' && document.getElementById('library-events')?.libraryEventsBound")
        self.driver.execute_async_script('const done=arguments[0];libraryResync().then(()=>done(true),e=>done(String(e)))')
        self.wait(self.visible_covers_ready())
        self.assertEqual(self.js('return audit.document'), marker)
        self.assertEqual(self.fixture.covers_requested(self.fixture), count,
                         'Reconnection must reuse cached cover images')
        self.assertEqual(self.js('return audit.errors'), [])


class AndroidResult(unittest.TextTestResult):
    def browser_state(self, test):
        try:
            state = test.js("""return {url:location.href,body:document.body.innerText.slice(0,1000),
                errors:window.audit?.errors,comicEvents:window.audit?.comicEvents,visibility:document.visibilityState,viewport:[innerWidth,innerHeight],
                focus:{id:document.activeElement?.id,document:document.hasFocus()},
                visualViewport:{width:visualViewport?.width,height:visualViewport?.height,scale:visualViewport?.scale},
                items:[...document.querySelectorAll('#items-grid [data-item-id]')].map(e=>({id:e.dataset.itemId,classes:e.className,rects:e.getClientRects().length})),
                frames:[...document.querySelectorAll('iframe')].map(f=>({id:f.id,src:f.getAttribute('src'),
                    visibility:f.style.visibility,rect:[f.clientWidth,f.clientHeight],
                    document:{url:f.contentDocument?.URL,ready:f.contentDocument?.readyState,
                        textLength:f.contentDocument?.body?.textContent?.length}})),
                scroll:(()=>{const e=document.getElementById('custom-scroll');return e &&
                    {top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight}})()}""")
            (ARGS.output / 'browser-state.json').write_text(json.dumps(state, indent=2))
            failure('Browser state\n' + json.dumps(state)[:3500])
        except Exception as error:
            failure('Browser state unavailable: ' + str(error)[-1000:])
        native = ARGS.output / 'native-input.jsonl'
        if native.exists():
            failure('Native input state\n' + '\n'.join(native.read_text().splitlines()[-3:])[-3500:])
        try:
            screen = subprocess.run(['adb', 'exec-out', 'screencap', '-p'],
                                    capture_output=True, timeout=15)
            if screen.returncode == 0 and screen.stdout.startswith(b'\x89PNG'):
                (ARGS.output / (test._testMethodName + '.png')).write_bytes(screen.stdout)
            else:
                failure('Native screenshot unavailable: ' + screen.stderr.decode(errors='replace')[-500:])
        except Exception as error:
            failure('Native screenshot unavailable: ' + str(error)[-500:])
        failure('Fixture server\n' + (test.fixture.fixture / 'server.log').read_text()[-3500:])

    def addError(self, test, error):
        self.browser_state(test)
        try:
            diagnostic = adb('shell', 'cat', '/proc/net/unix')
            diagnostic = '\n'.join(line for line in diagnostic.splitlines() if 'devtools' in line)
            adb('shell', 'uiautomator', 'dump', '/sdcard/browser-ui.xml')
            ui = adb('shell', 'cat', '/sdcard/browser-ui.xml')
            diagnostic += '\n' + '\n'.join(json.dumps({key: node.get(key) for key in ('text', 'content-desc', 'resource-id')})
                for node in ElementTree.fromstring(ui).iter('node') if node.get('text') or node.get('content-desc'))
            diagnostic += '\n' + adb('logcat', '-d', '-t', '100')
            (ARGS.output / 'android-failure-state.log').write_text(diagnostic)
            failure('Android UI state\n' + diagnostic[:3500])
            failure('Android logcat\n' + diagnostic[-3500:])
        except Exception as diagnostic_error:
            failure('Could not collect Android state: ' + str(diagnostic_error))
        for name in ('geckodriver-android.log', 'chromedriver-android.log'):
            path = ARGS.output / name
            if path.exists():
                lines = path.read_text(errors='replace').splitlines()
                failure(name + '\n' + '\n'.join(line for line in lines if 'DevTools HTTP Request' not in line)[-3500:])
        super().addError(test, error)
        failure(str(test) + '\n' + ''.join(traceback.format_exception(*error))[-3400:])

    def addFailure(self, test, error):
        self.browser_state(test)
        path = ARGS.output / 'header-native.xml'
        if test._testMethodName == 'test_compact_header_and_panels' and path.exists():
            nodes = [{key:n.get(key) for key in ('package','resource-id','class','text','content-desc','bounds')}
                     for n in ElementTree.fromstring(path.read_text()).iter('node')
                     if n.get('resource-id') or n.get('content-desc') or n.get('text')]
            failure('Native header tree\n'+json.dumps(nodes)[:3500])
        super().addFailure(test, error)
        failure(str(test) + '\n' + ''.join(traceback.format_exception(*error))[-3400:])

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error:
            self.browser_state(test)
            failure(str(subtest) + '\n' + ''.join(traceback.format_exception(*error)))


for name, check in READING_CHECKS.items():
    setattr(AndroidChecks, name, check)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', choices=('vivaldi', 'firefox'), required=True)
    parser.add_argument('--installation', type=Path, required=True)
    parser.add_argument('--versions', type=Path, required=True)
    parser.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL', 'emulator-5554'))
    parser.add_argument('--chromedriver', default='chromedriver')
    parser.add_argument('--geckodriver', default='geckodriver')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--test')
    parser.add_argument('--result-json', type=Path)
    ARGS = parser.parse_args()
    ARGS.output.mkdir(parents=True, exist_ok=True)
    browsers = (ARGS.browser,)
    names = [name for name in AndroidChecks.__dict__ if name.startswith('test_') and (not ARGS.test or name == ARGS.test)]
    if not names:
        parser.error('No Android tests selected.')
    suite = unittest.TestSuite(type(browser.title() + 'Android', (AndroidChecks,), {'browser': browser})(name)
                               for browser in browsers for name in names)
    result = unittest.TextTestRunner(verbosity=2, resultclass=AndroidResult, failfast=True).run(suite)
    if ARGS.result_json:
        ARGS.result_json.write_text(json.dumps({'run': result.testsRun, 'failures': len(result.failures),
            'errors': len(result.errors), 'skipped': len(result.skipped)}) + '\n')
    sys.exit(not result.wasSuccessful() or bool(result.skipped) or result.testsRun != len(names) * len(browsers))
