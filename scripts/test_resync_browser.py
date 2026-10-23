"""Real-server SSE/morph regressions, always in Chromium AND Firefox.

Requires the app's venv plus Selenium (already in requirements.txt).
Run from the repository root, supplying installed browser/driver paths:
  .venv/bin/python scripts/test_resync_browser.py --help
No production data, external websites or browser cache disabling are used.
"""
import argparse

from browser_checks import add_browser_argument, selected_browsers, baseline_passed

from ci_report import unittest_failures
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

import requests
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Installed before navigation, including the control navigation at the end.
# The marker dies on a real reload. Image src mutations and per-frame order
# are observed before any morph, not installed after its possible flash.
PROBE = """() => {
    window.resyncProbe = {
        document: crypto.randomUUID(), requests: 0, failures: 0,
        srcChanges: 0, frames: [], errors: [], fault: null, hold: false,
        visibility: [], connections: 0,
    };
    const p = resyncProbe;
    document.addEventListener('visibilitychange', () => p.visibility.push(document.visibilityState));
    document.addEventListener('htmx:sse:after:connection', () => p.connections++);
    window.addEventListener('error', e => p.errors.push(String(e.error || e.message)));
    window.addEventListener('unhandledrejection', e => p.errors.push(String(e.reason)));
    const fetchPage = window.fetch;
    window.fetch = async (...args) => {
        if (String(args[0]) === location.href && args[1]?.cache === 'no-store') {
            p.requests++;
            if (p.fault === 'network') {
                p.fault = null; p.failures++;
                throw new TypeError('transient network failure (test)');
            }
            if (p.fault === 'http') {
                p.fault = null; p.failures++;
                return new Response('temporarily unavailable', {status: 503});
            }
            const response = await fetchPage(...args);
            if (p.holdStats && new URL(args[0], location.href).pathname === '/stats') {
                p.holdStats = false;
                await new Promise(resolve => { p.releaseStats = resolve; });
            }
            if (p.hold) {
                p.hold = false;
                await new Promise(resolve => { p.release = resolve; });
            }
            return response;
        }
        return fetchPage(...args);
    };
    const sample = () => {
        if (p.sample) p.frames.push([...document.querySelectorAll('#series-grid .series-card')]
            .map(c => c.dataset.name).join('|'));
        requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
}"""


class BrowserChecks:
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='ebookreader-resync-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.fixture = Path(cls.temp.name)
        cls.data = cls.fixture / 'data'
        # All imports that initialize application paths happen after this.
        os.environ['APP_DATA_DIR'] = str(cls.data)
        from app.config import settings
        from app.database import init_db
        from app.auth.service import hash_password
        settings.data_dir = cls.data
        settings.covers_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(init_db())
        media = cls.fixture / 'media'
        media.mkdir()
        db = sqlite3.connect(settings.db_path)
        db.execute('INSERT INTO users(id,username,password_hash) VALUES(1,?,?)',
                   ('probe', hash_password('disposable-browser-test-password')))
        db.execute("INSERT INTO libraries(id,name,root_path,media_type) VALUES(1,'Comics',?,'comic')",
                   (str(media),))
        for i in range(1, 41):
            name = f'Series {i:02}'
            folder = media / name
            folder.mkdir()
            archive = folder / 'Volume 1.cbz'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('ComicInfo.xml', '<ComicInfo><Title>Volume 1</Title></ComicInfo>')
            cover = f'series-{i}-fixture.webp'
            Image.new('RGB', (200, 300), (i * 5, 80, 100)).save(settings.covers_dir / cover)
            db.execute('INSERT INTO series(id,library_id,name,sort_name,folder_path,cover_path) '
                       'VALUES(?,1,?,?,?,?)', (i, name, name.lower(), str(folder), cover))
            db.execute('INSERT INTO items(id,series_id,library_id,title,sort_title,file_path,'
                       'file_hash,file_size,file_format) VALUES(?,?,1,?,?,?, ?,?,\'cbz\')',
                       (i, i, 'Volume 1', 'volume1', str(archive), str(i), archive.stat().st_size))
        db.commit()
        db.close()
        # Obtain an unused local port without assuming the dev server's port.
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.base = f'http://localhost:{cls.port}'
        cls.server = None
        cls.addClassCleanup(cls.stop_server)
        cls.start_server()

    @classmethod
    def start_server(cls):
        cls.log = open(cls.fixture / 'server.log', 'a', encoding='utf-8')
        cls.server = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
             '--port', str(cls.port)], cwd=ROOT,
            env={**os.environ, 'APP_DATA_DIR': str(cls.data), 'APP_SCHEDULED_SCAN_CRON': ''},
            stdout=cls.log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError((cls.fixture / 'server.log').read_text())
            try:
                if requests.get(cls.base + '/login', timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(.1)
        raise RuntimeError('Test server did not start')

    @classmethod
    def stop_server(cls):
        if cls.server and cls.server.poll() is None:
            cls.server.kill()  # real lost SSE connection, not a mocked event
            cls.server.wait(timeout=10)
        if hasattr(cls, 'log'):
            cls.log.close()

    def covers_requested(self):
        return (self.fixture / 'server.log').read_text().count('"GET /covers/')

    def js(self, code):
        return self.driver.execute_script(code)

    def morph(self):
        self.driver.execute_async_script(
            'const done=arguments[0];libraryResync().then(()=>done(true),e=>done(String(e)))')

    def wait_for(self, condition):
        return WebDriverWait(self.driver, 20).until(lambda _: self.js('return ' + condition))

    def same_document(self):
        self.assertEqual(self.js('return resyncProbe.document'), self.document)

    def check_images(self):
        self.assertTrue(self.js('return resyncProbe.images.every(i=>i.isConnected && '
                                'i.parentElement.parentElement===resyncProbe.cards[i.src])'))
        self.assertEqual(self.js('return resyncProbe.srcChanges'), 0)
        self.assertEqual(self.covers_requested(), self.image_requests)

    def load_all_covers(self, selector='#series-grid img'):
        # Visit every cover before measuring reuse. Offscreen lazy images
        # are neither broken nor warmed yet.
        for image in self.driver.find_elements('css selector', selector):
            self.assertTrue(self.driver.execute_async_script('''const [image,done]=arguments;
                image.scrollIntoView({block:'center'});
                if(image.complete && image.naturalWidth>0) { done(true); return; }
                const timer=setTimeout(()=>finish(false),20000);
                const finish=ok=>{
                    clearTimeout(timer);image.removeEventListener('load',loaded);
                    image.removeEventListener('error',failed);done(ok);
                };
                const loaded=()=>finish(image.naturalWidth>0), failed=()=>finish(false);
                image.addEventListener('load',loaded);image.addEventListener('error',failed);
            ''', image), 'A revealed cover must load successfully')
        self.js('window.scrollTo(0,0)')

    def test_resync_survives_failures_navigation_and_restarts(self):
        if self.browser_name == 'chromium':
            options = ChromeOptions()
            options.binary_location = ARGS.chrome_binary
            options.add_argument('--headless=new')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            driver = webdriver.Chrome(options=options, service=ChromeService(ARGS.chromedriver))
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',
                                   {'source': '(' + PROBE + ')()'})
        else:
            options = FirefoxOptions()
            options.binary_location = ARGS.firefox_binary
            options.add_argument('-headless')
            options.enable_bidi = True
            options.set_preference('browser.cache.disk.enable', True)
            options.set_preference('browser.cache.memory.enable', True)
            driver = webdriver.Firefox(options=options, service=FirefoxService(
                ARGS.geckodriver, log_output=str(self.fixture / 'geckodriver.log')))
            driver.script.add_preload_script(PROBE)
        self.driver = driver
        self.addCleanup(driver.quit)
        session = requests.Session()
        session.post(self.base + '/login', data={
            'username': 'probe', 'password': 'disposable-browser-test-password'}).raise_for_status()
        driver.get(self.base + '/login')
        for name, value in session.cookies.items():
            driver.add_cookie({'name': name, 'value': value})
        self.js("localStorage.setItem('library-sort','name-desc')")
        driver.get(self.base + '/')
        self.wait_for('!!window.libraryResync && !!window.libraryGrid')
        self.load_all_covers()
        self.wait_for('[...document.querySelectorAll("#series-grid img")].length===40 && '
                      '[...document.querySelectorAll("#series-grid img")].every(i=>i.complete && i.naturalWidth>0)')
        self.document = self.js('return resyncProbe.document')
        self.js('''resyncProbe.images=[...document.querySelectorAll('#series-grid img')];
            resyncProbe.cards=Object.fromEntries(resyncProbe.images.map(i=>[i.src,i.parentElement.parentElement]));
            resyncProbe.sample=true;
            new MutationObserver(ms=>resyncProbe.srcChanges+=ms.filter(m=>m.attributeName==='src').length)
                .observe(document.getElementById('series-grid'),{subtree:true,attributes:true});''')
        self.image_requests = self.covers_requested()
        self.js("""const dialog=document.getElementById('new-library-dialog');
            dialog.showModal();
            const input=dialog.querySelector('input[name="name"]');
            input.value='half-typed library';input.focus();
            window.scrollTo(0,350);resyncProbe.scroll=window.scrollY;""")
        expected_order = self.js("return [...document.querySelectorAll('#series-grid .series-card')].map(c=>c.dataset.name).join('|')")
        self.morph()
        self.same_document()
        self.check_images()
        self.assertTrue(self.js("return document.getElementById('new-library-dialog').open"))
        self.assertEqual(self.js('return document.activeElement.value'), 'half-typed library')
        self.assertAlmostEqual(self.js('return window.scrollY'), self.js('return resyncProbe.scroll'), delta=2)
        self.assertTrue(self.js('return resyncProbe.frames.every(f=>f===' + json.dumps(expected_order) + ')'))
        print(self.browser_name + ': no-op morph preserves all 40 images and sorted frames', flush=True)

        for fault in ('network', 'http'):
            before = self.js('return resyncProbe.requests')
            self.js('resyncProbe.fault=' + json.dumps(fault))
            self.morph()
            self.same_document()
            self.wait_for(f'resyncProbe.document !== {json.dumps(self.document)} || '
                          f'resyncProbe.requests >= {before + 2}')
            self.same_document()
            self.morph()  # wait for any in-flight retry and its morph to finish
            self.same_document()
            self.check_images()
        swap = 'window.resyncProbe.realSwap=htmx.swap;htmx.swap=()=>{htmx.swap=resyncProbe.realSwap;return Promise.reject(new Error("temporary morph error (test)"))}'
        self.js(swap)
        before = self.js('return resyncProbe.requests')
        self.morph()
        self.same_document()
        self.wait_for(f'resyncProbe.document !== {json.dumps(self.document)} || '
                      f'resyncProbe.requests >= {before + 2}')
        self.same_document()
        self.morph()
        self.check_images()
        self.js("document.getElementById('new-library-dialog').close()")
        print(self.browser_name + ': network, HTTP 503 and morph failures retry without reload', flush=True)

        # Keep an old home response in flight while a real boosted link
        # navigates to /stats; the late response must not paint home over it.
        self.js('resyncProbe.hold=true;resyncProbe.holdStats=true;resyncProbe.pending=libraryResync()')
        self.wait_for('!!resyncProbe.release')
        self.js("const a=document.createElement('a');a.id='probe-navigation';a.href='/stats';a.textContent='Stats';document.body.append(a);htmx.process(a)")
        driver.find_element('id', 'probe-navigation').click()
        self.wait_for("location.pathname==='/stats' && !document.getElementById('series-grid')")
        self.js('resyncProbe.release()')
        driver.execute_async_script('const done=arguments[0];resyncProbe.pending.then(()=>done(true))')
        self.same_document()
        self.assertFalse(self.js("return !!document.getElementById('series-grid')"))
        # Discarding the old response starts a separate resync of /stats.
        # The old promise does not await it. Hold that follow-up explicitly
        # and finish its morph before adding the next synthetic link,
        # which otherwise disappears because it is not in the server HTML.
        self.wait_for('!!resyncProbe.releaseStats')
        self.js('resyncProbe.followup=libraryResync();resyncProbe.releaseStats()')
        driver.execute_async_script('const done=arguments[0];resyncProbe.followup.then(()=>done(true))')
        self.same_document()
        self.assertEqual(self.js('return location.pathname'), '/stats')
        self.assertFalse(self.js("return !!document.getElementById('series-grid')"))
        self.js("const a=document.createElement('a');a.id='probe-home';a.href='/';a.textContent='Home';document.body.append(a);htmx.process(a)")
        driver.find_element('id', 'probe-home').click()
        self.wait_for("location.pathname==='/' && !!document.getElementById('series-grid')")
        self.same_document()
        # The boosted navigation legitimately made new image elements;
        # keep fresh references for the restart tests. HTTP cache stays on.
        self.js("resyncProbe.sample=false;resyncProbe.images=[...document.querySelectorAll('#series-grid img')];resyncProbe.cards=Object.fromEntries(resyncProbe.images.map(i=>[i.src,i.parentElement.parentElement]))")
        self.image_requests = self.covers_requested()
        print(self.browser_name + ': late response cannot overwrite boosted navigation', flush=True)

        home_tab = driver.current_window_handle
        for iteration in range(3):
            # Real tab switches pause the SSE extension, then a killed
            # server gives the returning tab a new event epoch.
            before_connections = self.js('return resyncProbe.connections')
            driver.switch_to.new_window('tab')
            driver.get(self.base + '/static/manifest.webmanifest')
            background = driver.current_window_handle
            self.stop_server()
            db = sqlite3.connect(self.data / 'library.db')
            renamed = f'Updated {iteration}'
            db.execute('UPDATE series SET name=?,sort_name=? WHERE id=1',
                       (renamed, renamed.lower()))
            db.commit()
            db.close()
            self.start_server()
            driver.switch_to.window(home_tab)
            self.wait_for("document.querySelector('.series-card[data-series-id=\"1\"]')?.dataset.name===" + json.dumps(renamed.lower()))
            self.wait_for(f'resyncProbe.connections > {before_connections}')
            self.assertIn('hidden', self.js('return resyncProbe.visibility'))
            self.same_document()
            self.check_images()
            self.assertEqual(self.js("return [...document.querySelectorAll('#series-grid .series-card')].map(c=>c.dataset.name)"),
                             sorted(self.js("return [...document.querySelectorAll('#series-grid .series-card')].map(c=>c.dataset.name)"), reverse=True))
            driver.switch_to.window(background)
            driver.close()
            driver.switch_to.window(home_tab)
        print(self.browser_name + ': three server restarts and background-tab returns, zero image requests', flush=True)
        self.assertEqual(self.js('return resyncProbe.errors'), [])

        # An expired session is an intentional navigation, not a transient
        # connection failure. Exercise the real server's redirect handler.
        db = sqlite3.connect(self.data / 'library.db')
        db.execute('DELETE FROM sessions')
        db.commit()
        db.close()
        self.js('libraryResync()')
        self.wait_for("location.pathname==='/login'")
        self.assertNotEqual(self.js('return resyncProbe.document'), self.document)
        print(self.browser_name + ': expired session navigates to login', flush=True)

        # Positive control: a real document navigation MUST lose the marker.
        # This ensures the same-document assertions above can detect a reload.
        control_document = self.js('return resyncProbe.document')
        driver.refresh()
        self.wait_for('!!window.resyncProbe')
        self.assertNotEqual(self.js('return resyncProbe.document'), control_document)
        print(self.browser_name + ': reload detector positive control passed', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    parser.add_argument('--chrome-binary', default='/usr/bin/chromium')
    parser.add_argument('--chromedriver', default='/usr/bin/chromedriver')
    parser.add_argument('--firefox-binary', default='/usr/bin/firefox-esr')
    parser.add_argument('--geckodriver', default='/usr/bin/geckodriver')
    ARGS = parser.parse_args()
    suite = unittest.TestSuite()
    for target in selected_browsers(ARGS):
        browser = 'chromium' if target == 'chrome' else target
        case = type(browser.title() + 'Resync', (BrowserChecks, unittest.TestCase),
                    {'browser_name': browser})
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    expected = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    unittest_failures(result)
    sys.exit(not baseline_passed(result, expected))
