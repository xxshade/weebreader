"""Audit regressions through the real application, in Chrome AND Firefox.

Run with the same browser/driver flags as test_resync_browser.py.
Every fixture, account and server is disposable. HTTP cache stays enabled.
"""
import argparse

from browser_checks import add_browser_argument, selected_browsers, baseline_passed

from ci_report import unittest_failures
import base64
import sqlite3
import zipfile
import os
from pathlib import Path
import sys
import unittest

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resync_browser import BrowserChecks

PROBE = """() => {
    window.audit = {document:crypto.randomUUID(),errors:[],held:false};
    Element.prototype.requestFullscreen=()=>Promise.resolve();
    window.addEventListener('error', e=>audit.errors.push(String(e.error||e.message)));
    window.addEventListener('unhandledrejection', e=>audit.errors.push(String(e.reason)));
    // Release the actual exit payload only after bulk completion commits.
    // No sleeps: this forces the ordering that previously undid completion.
    const originalBeacon=navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon=(url,body)=>{
        if (audit.holdExit && url==='/reader/101/progress') {
            audit.exitSave={url,body};
            return true;
        }
        return originalBeacon(url,body);
    };
    const original=fetch;
    window.fetch=async(...args)=>{
        const response=await original(...args);
        if (String(args[0])==='/series/101/mark-all-read' && audit.holdExit) {
            await response.clone().text();
            if (!audit.exitSave) throw new Error('Expected a held reader exit save');
            const {url,body}=audit.exitSave;
            const saved=await original(url,{method:'POST',body});
            if (!saved.ok) throw new Error('Delayed reader exit save failed');
            audit.exitReleased=true;
        }
        if (String(args[0])==='/series/1/items-grid' && audit.hold) {
            const body=await response.text();
            audit.hold=false;audit.held=true;
            await new Promise(resolve=>audit.release=resolve);
            return new Response(body,{status:response.status,headers:response.headers});
        }
        return response;
    };
}"""


class AuditChecks(unittest.TestCase):
    viewport_width = 1280
    probe = PROBE

    @classmethod
    def setUpClass(cls):
        cls.fixture = type('Fixture', (BrowserChecks, unittest.TestCase), {})
        cls.addClassCleanup(cls.fixture.doClassCleanups)
        cls.fixture.setUpClass()
        cls.base = cls.fixture.base
        cls.seed_readers()

    @classmethod
    def seed_readers(cls):
        from tests.test_archives import PNG_1PX
        paragraphs = lambda n, total=18: ''.join(
            f'<p>Chapter {n} paragraph {i}. ' +
            ('A disposable book exercises the ordinary reader with real page turns. ' * 9) + '</p>'
            for i in range(1, total + 1))
        with sqlite3.connect(cls.fixture.data / 'library.db') as db:
            for item, count in ((100, 1), (101, 2), (102, 1), (104, 1)):
                folder = cls.fixture.fixture / f'epub-{item}'
                folder.mkdir()
                archive = folder / 'book.epub'
                with zipfile.ZipFile(archive, 'w') as book:
                    book.writestr('mimetype', 'application/epub+zip')
                    book.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
                    manifest = ''.join(f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(count))
                    spine = ''.join(f'<itemref idref="c{i}"/>' for i in range(count))
                    book.writestr('OEBPS/content.opf', f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book {item}</dc:title><dc:identifier id="uid">{item}</dc:identifier><dc:language>en</dc:language></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
                    for i in range(count):
                        book.writestr(f'OEBPS/ch{i}.xhtml', f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter {i}</title></head><body>{paragraphs(i, 200 if item == 102 else 18)}</body></html>')
                db.execute('INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(?,1,?,?,?)',
                           (item, f'Book {item}', f'book{item}', str(folder)))
                db.execute("INSERT INTO items(id,series_id,library_id,title,sort_title,file_path,file_hash,file_size,file_format) VALUES(?,?,1,?,?,?,?,?,'epub')",
                           (item, item, f'Book {item}', f'book{item}', str(archive), str(item), archive.stat().st_size))
            db.execute("INSERT INTO reading_progress(user_id,item_id,status,current_page,current_location,progress_percent) VALUES(1,102,'reading',0,'elidx:196',98)")
            db.execute("INSERT INTO novels(id,title,source_url,sync_status,active) VALUES(100,'Illustrated novel','https://example.invalid/novel','idle',0)")
            db.execute('INSERT INTO volumes(id,novel_id,number) VALUES(100,100,1)')
            content = '<p><img src="data:image/png;base64,' + base64.b64encode(PNG_1PX).decode() + '" alt="Illustration"></p>' + paragraphs(0)
            db.execute('INSERT INTO chapters(id,novel_id,volume_id,number,title,url,order_index,content_html,content_size) VALUES(100,100,100,1,?,?,0,?,?)',
                       ('Final chapter', 'https://example.invalid/chapter', content, len(content)))

    def row(self, sql):
        with sqlite3.connect(self.fixture.data / 'library.db') as db:
            db.row_factory = sqlite3.Row
            row = db.execute(sql).fetchone()
            return dict(row) if row else None

    def setUp(self):
        if self.browser == 'chrome':
            options = ChromeOptions()
            options.binary_location = ARGS.chrome_binary
            for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage'):
                options.add_argument(flag)
            self.driver = webdriver.Chrome(options=options, service=ChromeService(ARGS.chromedriver))
            self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': '(' + self.probe + ')()'})
        else:
            options = FirefoxOptions()
            options.binary_location = ARGS.firefox_binary
            options.add_argument('-headless')
            options.enable_bidi = True
            self.driver = webdriver.Firefox(options=options, service=FirefoxService(ARGS.geckodriver,
                log_output=str(self.fixture.fixture / 'geckodriver.log'),
                env={**os.environ, 'MOZ_DISABLE_CONTENT_SANDBOX': '1',
                     'XDG_CACHE_HOME': str(self.fixture.fixture / 'cache')}))
            self.driver.script.add_preload_script(self.probe)
        self.addCleanup(self.driver.quit)
        self.driver.set_window_size(self.viewport_width, 900)
        if self.viewport_width < 500:
            if self.browser == 'chrome':
                self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                    'width': self.viewport_width, 'height': 740,
                    'deviceScaleFactor': 1, 'mobile': False})
            else:
                self.driver.browsing_context.set_viewport(
                    context=self.driver.current_window_handle,
                    viewport={'width': self.viewport_width, 'height': 740})
            self.assertEqual(self.js('return innerWidth'), self.viewport_width)
        session = requests.Session()
        session.post(self.base + '/login', data={
            'username': 'probe', 'password': 'disposable-browser-test-password'}).raise_for_status()
        self.driver.get(self.base + '/login')
        for name, value in session.cookies.items():
            self.driver.add_cookie({'name': name, 'value': value})

    def js(self, code):
        return self.driver.execute_script(code)

    def wait(self, expression):
        return WebDriverWait(self.driver, 20).until(lambda _: self.js('return ' + expression))

    def boosted(self, path):
        self.driver.execute_script("const a=document.createElement('a');a.id='audit-nav';a.href=arguments[0];a.textContent='Go';document.body.append(a);htmx.process(a)", path)
        self.driver.find_element('id', 'audit-nav').click()
        self.wait('location.pathname===' + repr(path))

    def frames(self):
        self.driver.execute_async_script('const done=arguments[0];requestAnimationFrame(()=>requestAnimationFrame(()=>done(true)))')

    def test_a_delayed_series_response_cannot_overwrite_another_series(self):
        self.driver.get(self.base + '/series/1')
        self.wait("document.getElementById('library-events')?.libraryEventsBound")
        marker = self.js('return audit.document')
        self.js("audit.hold=true;document.getElementById('library-events').dispatchEvent(new CustomEvent('item_updated',{detail:{data:JSON.stringify({type:'item_updated',item_id:1,series_id:1})}}))")
        self.wait('audit.held')
        self.boosted('/series/2')
        self.wait("document.getElementById('series-page')?.dataset.seriesId==='2'")
        self.js('audit.release()')
        self.frames()
        self.assertEqual(self.js('return audit.document'), marker)
        self.assertEqual(self.js("return document.getElementById('items-grid').dataset.reorderUrl"),
                         '/series/2/manual-sort/reorder')
        self.assertEqual(self.js("return [...document.querySelectorAll('#items-grid [data-item-id]')].map(e=>e.dataset.itemId)"), ['2'])
        self.assertEqual(self.js('return audit.errors'), [])

    def open_reader(self, path):
        self.js("localStorage.setItem('text-reader-prefs',JSON.stringify({flow:'paginated',font:'original',fontSize:100,lineHeight:1.6,width:720,bottomGap:0}))")
        self.driver.get(self.base + path)
        self.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
        ActionChains(self.driver).move_to_element(self.driver.find_element('css selector', '#paginated-viewer iframe')).click().perform()
        WebDriverWait(self.driver, 10).until(lambda _: self.driver.find_element('id', 'btn-next').is_displayed())

    def end_visible(self):
        return self.js("return [...document.querySelectorAll('#reader-root div')].some(e=>e.textContent==='End of series')")

    def next_page(self):
        self.js("audit.oldFrame=document.querySelector('#paginated-viewer iframe')")
        self.driver.find_element('id', 'btn-next').click()
        self.wait("[...document.querySelectorAll('#reader-root div')].some(e=>e.textContent==='End of series') || (document.querySelector('#paginated-viewer iframe')!==audit.oldFrame && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible')")

    def read_to_end(self):
        for _ in range(80):
            if self.end_visible():
                return
            self.next_page()
        self.fail('Reader did not reach its end')


    def test_html_server_timing_survives_cache_revalidation(self):
        self.driver.get(self.base + '/settings')
        navigation = self.js("return performance.getEntriesByType('navigation')[0].serverTiming.map(t=>({name:t.name,duration:t.duration}))")
        self.assertTrue(any(t['name'] == 'app' and t['duration'] >= 0 for t in navigation),
                        'Navigation must expose application timing to DevTools')
        result = self.driver.execute_async_script("""const done=arguments[0];
            (async()=>{
                const first=await fetch('/settings',{cache:'no-store'}),body=await first.text();
                const etag=first.headers.get('etag');
                const cached=await fetch('/settings',{cache:'no-store',headers:{'If-None-Match':etag}});
                const empty=await cached.text();
                const asset=await fetch('/static/js/ui-prefs.js');await asset.text();
                done({status:first.status,timing:first.headers.get('server-timing'),etag,
                    cachedStatus:cached.status,cachedTiming:cached.headers.get('server-timing'),
                    cachedEtag:cached.headers.get('etag'),policy:cached.headers.get('cache-control'),
                    length:body.length,cachedLength:empty.length,assetTiming:asset.headers.get('server-timing')});
            })().catch(error=>done({error:String(error)}));""")
        self.assertEqual(result['status'], 200)
        self.assertGreater(result['length'], 0)
        self.assertEqual(result['cachedStatus'], 304)
        self.assertEqual(result['cachedLength'], 0)
        self.assertEqual(result['etag'], result['cachedEtag'])
        self.assertEqual(result['policy'], 'private, no-cache')
        for key in ('timing', 'cachedTiming'):
            self.assertRegex(result[key] or '', r'^app;dur=[0-9]+\.[0-9]{3}$',
                             'Every HTML response must include application timing')
        self.assertIsNone(result['assetTiming'])

    def test_epub_stylesheets_resolve_without_extra_requests(self):
        from tests.test_archives import PNG_1PX
        with sqlite3.connect(self.fixture.data / 'library.db') as db:
            original = db.execute('SELECT file_path FROM items WHERE id=100').fetchone()[0]
            with zipfile.ZipFile(original) as book:
                files = {name: book.read(name) for name in book.namelist()}
            files['OEBPS/content.opf'] = files['OEBPS/content.opf'].replace(b'href="ch0.xhtml"', b'href="Text/ch0.xhtml"')
            links = (b'<link rel="stylesheet" href="../Styles/styles.css">'
                     b'<link rel="stylesheet" href="../Styles/with%20space.css">'
                     b'<link rel="stylesheet" href="/reader/103/resource/OEBPS/Styles/absolute.css">'
                     b'<link rel="stylesheet"><link rel="stylesheet" href="http://[invalid">'
                     b'<base href="/ignored-book-base/">')
            files['OEBPS/Text/ch0.xhtml'] = files.pop('OEBPS/ch0.xhtml').replace(b'<head>', b'<head>' + links)
            files['OEBPS/Styles/styles.css'] = b'@import "nested/import.css";p{text-align:justify;background-image:url("../Images/dot.png")}'
            files['OEBPS/Styles/nested/import.css'] = b'p{letter-spacing:2px}'
            files['OEBPS/Styles/with space.css'] = b'p{border-top:7px solid red}'
            files['OEBPS/Styles/absolute.css'] = b'p{word-spacing:3px}'
            files['OEBPS/Images/dot.png'] = PNG_1PX
            archive = self.fixture.fixture / 'stylesheet-fixture.epub'
            with zipfile.ZipFile(archive, 'w') as book:
                for name, content in files.items():
                    book.writestr(name, content)
            db.execute("INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(103,1,'Styles','styles',?)", (str(archive.parent),))
            db.execute("INSERT INTO items(id,series_id,library_id,title,sort_title,file_path,file_hash,file_size,file_format) VALUES(103,103,1,'Styles','styles',?,'styles',?,'epub')", (str(archive), archive.stat().st_size))
        log = self.fixture.fixture / 'server.log'
        start = len(log.read_text())
        self.open_reader('/reader/103')
        state = self.js("""const f=document.querySelector('#paginated-viewer iframe'),d=f.contentDocument;
            const s=f.contentWindow.getComputedStyle(d.querySelector('p'));
            return {styles:[s.textAlign,s.letterSpacing,s.borderTopWidth,s.wordSpacing],
                links:[...d.querySelectorAll('link[rel~=stylesheet]')].map(l=>l.getAttribute('href'))};""")
        self.assertEqual(state['styles'], ['justify', '2px', '7px', '3px'])
        self.assertIn(None, state['links'], 'A link without href must not acquire a URL')
        self.assertIn('http://[invalid', state['links'], 'Malformed URLs must not prevent reading')
        logs = log.read_text()[start:]
        self.assertNotIn('404 Not Found', logs, 'Stylesheets must not request a path outside the EPUB resource directory')
        for path in ('Styles/styles.css', 'Styles/nested/import.css', 'Styles/with%20space.css', 'Styles/absolute.css', 'Images/dot.png'):
            self.assertIn('/reader/103/resource/OEBPS/' + path, logs)

    def test_bulk_completion_reopens_epub_at_the_beginning(self):
        self.open_reader('/reader/101')
        for _ in range(40):
            self.next_page()
            position = self.row('SELECT current_page,current_location FROM reading_progress WHERE item_id=101')
            if position and position['current_page'] == 1 and int(position['current_location'].split(':')[1]) >= 3:
                break
        else:
            self.fail('Did not save a position in the second chapter')
        self.js('audit.holdExit=true')
        self.driver.find_element('link text', '← Back').click()
        self.wait("location.pathname==='/series/101'")
        self.driver.find_element('css selector', 'form[action="/series/101/mark-all-read"] button').click()
        self.wait('audit.exitReleased')
        self.assertEqual(self.row('SELECT status FROM reading_progress WHERE item_id=101'),
                         {'status': 'completed'}, 'A late exit save must not undo bulk completion')
        self.open_reader('/reader/101')
        self.assertEqual(self.js("return document.getElementById('reader-root').dataset.startCfi"), '')
        self.assertIn('Chapter 0 paragraph 1.', self.js("return document.querySelector('#paginated-viewer iframe').contentDocument.body.textContent"))
        self.assertEqual(self.js('return audit.errors'), [])


    def test_displaying_final_page_does_not_complete_before_next(self):
        self.open_reader('/reader/102')
        self.next_page()
        self.assertFalse(self.end_visible())
        WebDriverWait(self.driver, 5).until(lambda _: self.row('SELECT current_location FROM reading_progress WHERE item_id=102') != {'current_location': 'elidx:196'})
        position = self.row('SELECT status,progress_percent FROM reading_progress WHERE item_id=102')
        self.assertGreaterEqual(position['progress_percent'], 98)
        self.assertEqual(position['status'], 'reading')
        self.read_to_end()
        WebDriverWait(self.driver, 5).until(lambda _: self.row('SELECT status FROM reading_progress WHERE item_id=102') == {'status': 'completed'})

    def test_late_epub_page_save_does_not_undo_completion_or_rereading(self):
        self.open_reader('/reader/104')
        self.js("""const original=fetch;
            audit.progressOrder={};window.fetch=async(...args)=>{
                if(String(args[0])!=='/reader/104/progress')return original(...args);
                const payload=JSON.parse(args[1].body),probe=audit.progressOrder;
                const hold=payload.status==='reading'&&!probe.held;
                if(hold){probe.held=true;probe.oldPayload=payload;
                    await new Promise(resolve=>probe.release=resolve);}
                const response=await original(...args);await response.clone().text();
                if(hold)probe.lateSaved=response.ok;
                if(payload.status==='completed')probe.completionSaved=response.ok;
                return response;
            };""")
        self.next_page()
        self.wait('audit.progressOrder.held')
        self.read_to_end()
        self.wait('audit.progressOrder.completionSaved')
        self.assertEqual(self.row('SELECT status FROM reading_progress WHERE item_id=104'),
                         {'status': 'completed'})
        self.js('audit.progressOrder.release()')
        self.wait('audit.progressOrder.lateSaved')
        self.assertEqual(self.row('SELECT status FROM reading_progress WHERE item_id=104'),
                         {'status': 'completed'}, 'A late page save must not undo completion')
        self.driver.find_element('xpath', "//div[text()='End of series']").click()
        self.js("audit.oldFrame=document.querySelector('#paginated-viewer iframe')")
        self.driver.find_element('id', 'btn-prev').click()
        self.wait("document.querySelector('#paginated-viewer iframe')!==audit.oldFrame && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
        WebDriverWait(self.driver, 5).until(lambda _: self.row(
            'SELECT status FROM reading_progress WHERE item_id=104') == {'status': 'reading'})
        self.assertLess(self.row('SELECT progress_percent FROM reading_progress WHERE item_id=104')['progress_percent'], 100,
                        'A newer backward turn must remain a valid reading action')
        self.assertEqual(self.js('return audit.errors'), [])


    def test_explicit_end_completes_epub_and_survives_exit(self):
        self.open_reader('/reader/100')
        self.assertIsNone(self.row('SELECT status FROM reading_progress WHERE item_id=100'))
        self.read_to_end()
        WebDriverWait(self.driver, 5).until(lambda _: self.row('SELECT status FROM reading_progress WHERE item_id=100') == {'status': 'completed'})
        self.driver.find_element('link text', 'Back to the series').click()
        self.wait("location.pathname==='/series/100'")
        self.assertEqual(self.row('SELECT status,progress_percent FROM reading_progress WHERE item_id=100'),
                         {'status': 'completed', 'progress_percent': 100.0})


    def test_final_novel_chapter_keeps_illustration_completes_and_returns_to_novel(self):
        self.open_reader('/novels/100/read/1/1')
        self.assertTrue(self.js("return !!document.querySelector('#paginated-viewer iframe').contentDocument.querySelector('img')"))
        self.assertIsNone(self.row('SELECT chapter_id FROM novel_read_chapters WHERE chapter_id=100'))
        self.read_to_end()
        WebDriverWait(self.driver, 5).until(lambda _: self.row('SELECT chapter_id FROM novel_read_chapters WHERE chapter_id=100') == {'chapter_id': 100})
        self.driver.find_element('link text', 'Back to the series').click()
        self.wait("location.pathname==='/novels/100'")
        self.assertEqual(self.row('SELECT chapter_id FROM novel_read_chapters WHERE chapter_id=100'), {'chapter_id': 100})
        self.assertEqual(self.js('return audit.errors'), [])



class DetailedTimingChecks(AuditChecks):
    @classmethod
    def seed_readers(cls):
        super().seed_readers()
        with sqlite3.connect(cls.fixture.data / 'library.db') as db:
            db.execute("INSERT INTO page_reads(user_id,kind,started_at,seconds,cap_seconds,visit_id,pace_words,pace_seconds,pace_samples) VALUES(1,'epub',datetime('now'),300,300,'timing-pace',1000,300,3)")
        # Warm real estimates before checking stable HTML/ETags; browser
        # background ETA refreshes must not race this conditional request.
        with requests.Session() as client:
            client.post(cls.base + '/login', data={
                'username': 'probe', 'password': 'disposable-browser-test-password'},
                allow_redirects=False, timeout=15).raise_for_status()
            client.post(cls.base + '/library/reading-estimates', json={'targets':
                [{'kind': 'series', 'id': identity} for identity in (100,101,102,104)]
                + [{'kind': 'novel', 'id': 100}]}, timeout=30).raise_for_status()

    @classmethod
    def setUpClass(cls):
        previous = os.environ.get('APP_DETAILED_SERVER_TIMING')
        os.environ['APP_DETAILED_SERVER_TIMING'] = 'true'
        try:
            super().setUpClass()
        finally:
            if previous is None:
                os.environ.pop('APP_DETAILED_SERVER_TIMING', None)
            else:
                os.environ['APP_DETAILED_SERVER_TIMING'] = previous

    def test_home_phases_are_exposed_on_200_and_304(self):
        self.driver.get(self.base + '/')
        names = self.js("return performance.getEntriesByType('navigation')[0].serverTiming.map(t=>t.name)")
        expected = {'app', 'db_acquire', 'auth', 'home_data', 'render', 'revalidate', 'other',
                    'home_queries', 'home_estimator', 'home_series', 'home_novels',
                    'home_tracking', 'home_cards', 'home_reading', 'home_other',
                    'series_query', 'series_prepare', 'series_aggregate', 'series_other',
                    'items_query', 'items_positions', 'items_rules', 'items_stat',
                    'items_index', 'items_snapshots', 'items_other'}
        self.assertEqual(set(names), expected, 'Detailed home phases must be visible to DevTools')
        result = self.driver.execute_async_script("""const done=arguments[0];
            (async()=>{
                const first=await fetch('/',{cache:'no-store'});await first.text();
                const second=await fetch('/',{cache:'no-store',headers:{'If-None-Match':first.headers.get('etag')}});
                const body=await second.text();
                done({status:second.status,length:body.length,timing:second.headers.get('server-timing')});
            })().catch(error=>done({error:String(error)}));""")
        self.assertEqual(result['status'], 304)
        self.assertEqual(result['length'], 0)
        values = {name: float(value.removeprefix('dur=')) for name, value in
                  (part.strip().split(';') for part in result['timing'].split(','))}
        self.assertEqual(set(values), expected)
        self.assertTrue(all(value >= 0 for value in values.values()))
        skipped = {'series_query', 'series_aggregate', 'items_positions', 'items_rules'}
        for key in expected - {'app', 'other', 'series_other', 'items_other'} - skipped:
            self.assertGreater(values[key], 0, key)
        for key in skipped:
            self.assertEqual(values[key], 0, 'Prepared estimates must skip ' + key)
        self.assertAlmostEqual(values['series_prepare'], sum(value for key, value in values.items() if key.startswith('items_')), delta=0.01)
        self.assertAlmostEqual(values['home_series'], sum(value for key, value in values.items() if key.startswith('series_')), delta=0.01)
        self.assertAlmostEqual(values['home_data'], sum(value for key, value in values.items() if key.startswith('home_') and key != 'home_data'), delta=0.01)
        self.assertAlmostEqual(values['app'], sum(value for key, value in values.items() if key in ('db_acquire', 'auth', 'home_data', 'render', 'revalidate', 'other')), delta=0.01)
        api = self.driver.execute_async_script("""const done=arguments[0];
            (async()=>{
                const response=await fetch('/library/reading-estimates',{
                    method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({targets:[{kind:'series',id:100}]})});
                const data=await response.json();
                done({status:response.status,timing:response.headers.get('server-timing'),
                    estimates:Array.isArray(data.estimates),key:data.estimates?.[0]?.key});
            })().catch(error=>done({error:String(error)}));""")
        self.assertEqual(api['status'], 200)
        self.assertTrue(api['estimates'])
        self.assertEqual(api['key'], 'series:100:')
        self.assertIsInstance(api['timing'], str, 'API timing must be visible in both browser engines')
        values = {name:float(duration.removeprefix('dur=')) for name,duration in
                  (part.strip().split(';') for part in api['timing'].split(','))}
        roots = {'api_auth','api_slot','api_db','api_input','api_estimator','api_cache',
                 'api_fallback','api_encode','api_flush','api_other'}
        self.assertEqual(set(values), {'app',*roots,'api_validate','api_targets','api_cache_other',
                                      'api_files','api_index','api_revisions','api_validate_other'})
        self.assertTrue(all(value>=0 for value in values.values()))
        self.assertAlmostEqual(values['app'],sum(values[key] for key in roots),delta=.02)
        self.assertAlmostEqual(values['api_cache'],sum(values[key] for key in ('api_validate','api_targets','api_cache_other')),delta=.01)
        self.assertAlmostEqual(values['api_validate'],sum(values[key] for key in ('api_files','api_index','api_revisions','api_validate_other')),delta=.01)


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
        case = type(browser.title() + 'Audit', (AuditChecks,), {'browser': browser})
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
        mobile = type(browser.title() + 'MobileAudit', (AuditChecks,), {
            'browser': browser, 'viewport_width': 320})
        suite.addTest(mobile('test_bulk_completion_reopens_epub_at_the_beginning'))
        suite.addTest(mobile('test_epub_stylesheets_resolve_without_extra_requests'))
        suite.addTest(mobile('test_late_epub_page_save_does_not_undo_completion_or_rereading'))
        suite.addTest(mobile('test_html_server_timing_survives_cache_revalidation'))
        for width in (1280, 320):
            detailed = type(browser.title() + 'DetailedTiming' + str(width), (DetailedTimingChecks,), {
                'browser': browser, 'viewport_width': width})
            suite.addTest(detailed('test_home_phases_are_exposed_on_200_and_304'))
    expected = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    unittest_failures(result)
    sys.exit(not baseline_passed(result, expected))
