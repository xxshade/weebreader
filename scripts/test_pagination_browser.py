"""Fragment pagination through the real EPUB and web-novel readers, both engines."""
import argparse

from browser_checks import add_browser_argument, selected_browsers, baseline_passed
import sqlite3
import sys
import unittest
import zipfile

import test_audit_browser as audit
from ci_report import unittest_failures
from selenium.webdriver.support.ui import Select, WebDriverWait

WORDS = ' '.join(f'word{i:04d}' for i in range(450))
CONTENT = '<p id="intro">A short introduction.</p><p id="long" style="text-indent:2em">' + WORDS[:WORDS.index('word0200')] + '<em>' + WORDS[WORDS.index('word0200'):WORDS.index('word0300')] + '</em>' + WORDS[WORDS.index('word0300'):WORDS.index('word0330')] + '<a href="#ending" title="Note">' + WORDS[WORDS.index('word0330'):WORDS.index('word0430')] + '</a>' + WORDS[WORDS.index('word0430'):] + '</p><p id="ending">The final paragraph.</p>'


def check_srcdoc_commit(case):
    # Replay an initial blank-document load before srcdoc navigation commits.
    # The probe runs in the parent; no script is injected into book content.
    probe = """() => {
        const descriptor=Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype,'srcdoc');
        Object.defineProperty(HTMLIFrameElement.prototype,'srcdoc',{...descriptor,set(value){
            const frame=this;
            if (!frame.hasAttribute('sandbox')) {descriptor.set.call(frame,value);return;}
            frame.addEventListener('load',()=>{
                if (frame.contentDocument?.URL==='about:srcdoc') frame.dataset.testCommitted='1';
            });
            setTimeout(()=>{
                frame.dataset.testEarlyDocument=frame.contentDocument?.URL || 'missing';
                frame.dispatchEvent(new Event('load'));
            },20);
            setTimeout(()=>descriptor.set.call(frame,value),200);
        }});
    }"""
    if case.browser in ('chrome', 'vivaldi'):
        case.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': '(' + probe + ')()'})
    else:
        case.driver.script.add_preload_script(probe)
    for path in ('/reader/100', '/novels/100/read/1/1'):
        for flow, selector in (('paginated', '#paginated-viewer iframe'), ('scrolled-doc', '#custom-scroll iframe')):
            case.driver.get(case.base + '/')
            case.driver.execute_script("localStorage.setItem('text-reader-prefs',JSON.stringify({flow:arguments[0],font:'original',fontSize:100,lineHeight:1.6,width:720,bottomGap:0}))", flow)
            case.driver.get(case.base + path)
            frame = 'document.querySelector(' + repr(selector) + ')'
            case.wait(frame + "?.dataset.testCommitted==='1' && " + frame + "?.style.visibility==='visible'")
            state = case.js('const f=' + frame + ';return {text:f.contentDocument.body.textContent,early:f.dataset.testEarlyDocument,height:f.clientHeight,contentHeight:f.contentDocument.documentElement.scrollHeight}')
            case.assertEqual(state['early'], 'about:blank', 'The load-race probe must reach the initial blank document')
            case.assertIn('A short introduction.', state['text'],
                          'A late srcdoc commit must not erase the rendered chapter')
            if flow == 'paginated':
                case.show_controls()
                case.assertTrue(case.page()['text'], 'The committed chapter must retain its tap handler after reflow')
            else:
                case.assertIn('word0449', state['text'], 'Continuous reading must wait for its chapter document')
                case.assertLessEqual(state['contentHeight'], state['height'] + 1,
                                     'Continuous reading must measure the committed chapter without clipping text')


class PaginationChecks(audit.AuditChecks):
    @classmethod
    def seed_readers(cls):
        super().seed_readers()
        with sqlite3.connect(cls.fixture.data / 'library.db') as db:
            archive = db.execute('SELECT file_path FROM items WHERE id=100').fetchone()[0]
            with zipfile.ZipFile(archive) as book:
                files = {name: book.read(name) for name in book.namelist()}
            files['OEBPS/ch0.xhtml'] = '<html><head></head><body>' + CONTENT + '</body></html>'
            with zipfile.ZipFile(archive, 'w') as book:
                for name, data in files.items():
                    book.writestr(name, data)
            db.execute('UPDATE chapters SET content_html=?,content_size=? WHERE id=100', (CONTENT, len(CONTENT)))

    def open_reader(self, path):
        self.js("localStorage.setItem('text-reader-prefs',JSON.stringify({flow:'paginated',font:'original',fontSize:100,lineHeight:1.6,width:720,bottomGap:0}))")
        self.driver.get(self.base + path)
        self.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
        self.show_controls()

    def show_controls(self):
        from selenium.webdriver.common.action_chains import ActionChains
        self.js("audit.beforeControls=document.querySelector('#paginated-viewer iframe')")
        ActionChains(self.driver).move_to_element(self.driver.find_element('css selector', '#paginated-viewer iframe')).click().perform()
        self.wait("document.querySelector('#paginated-viewer iframe') !== audit.beforeControls && document.querySelectorAll('#paginated-viewer iframe').length === 1 && document.querySelector('#paginated-viewer iframe').style.visibility === 'visible'")

    def page(self):
        return self.js("""const f=document.querySelector('#paginated-viewer iframe');const d=f.contentDocument;
            return {text:d.body.textContent, height:d.body.scrollHeight, viewport:f.clientHeight,
                long:d.getElementById('long')?.textContent, indent:d.getElementById('long') && d.defaultView.getComputedStyle(d.getElementById('long')).textIndent};""")

    def test_fragments_preserve_content_and_resume(self):
        self.driver.set_window_size(460, 740)
        for path in ('/reader/100', '/novels/100/read/1/1'):
            with self.subTest(path=path):
                self.open_reader(path)
                first = self.page()
                self.assertTrue(first['long'], 'Ordinary pages should use the space after the introduction')
                self.assertLess(len(first['long']), len(WORDS), 'A long paragraph must span pages')
                self.assertLessEqual(first['height'], first['viewport'] + 1)
                self.next_page()
                second = self.page()
                self.assertEqual(second['indent'], '0px', 'A continuation must not acquire another indent')
                # Rendering a page precedes its asynchronous save. Refresh only
                # after the displayed fragment is committed, not a prior anchor.
                offset = WORDS.index(second['long'][:60])
                if path.startswith('/reader/'):
                    saved = lambda: self.row('SELECT current_location FROM reading_progress WHERE item_id=100') == {'current_location': f'elidx:1:{offset}'}
                else:
                    saved = lambda: self.row('SELECT paragraph_index,character_offset FROM novel_reading_progress WHERE novel_id=100') == {'paragraph_index': 1, 'character_offset': offset}
                WebDriverWait(self.driver, 20).until(lambda _: saved())
                self.driver.refresh()
                self.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
                self.assertTrue(self.page()['text'].startswith(second['text'][:60]), 'Resume inside the paragraph')
                # Restoring the controls reflows from the saved text, not a page number.
                self.show_controls()
                pages = [first['text']]
                emphasis = []
                links = []
                for _ in range(80):
                    current = self.page()
                    self.assertLessEqual(current['height'], current['viewport'] + 1)
                    pages.append(current['text'])
                    emphasis.append(self.js("return document.querySelector('#paginated-viewer iframe').contentDocument.querySelector('em')?.textContent || ''"))
                    link = self.js("const a=document.querySelector('#paginated-viewer iframe').contentDocument.querySelector('a');return a && {href:a.getAttribute('href'),title:a.title,text:a.textContent}")
                    if link and link['text']:
                        self.assertEqual((link['href'], link['title']), ('#ending', 'Note'))
                        links.append(link['text'])
                    if current['text'].endswith('The final paragraph.'):
                        break
                    self.next_page()
                else:
                    self.fail('Pagination did not advance to the end')
                self.assertEqual(''.join(pages), 'A short introduction.' + WORDS + 'The final paragraph.')
                self.assertEqual(''.join(emphasis), WORDS[WORDS.index('word0200'):WORDS.index('word0300')])
                self.assertEqual(''.join(links), WORDS[WORDS.index('word0330'):WORDS.index('word0430')])
                reverse = [self.page()['text']]
                for _ in range(80):
                    if reverse[0].startswith('A short introduction.'):
                        break
                    self.turn_back()
                    current = self.page()
                    self.assertLessEqual(current['height'], current['viewport'] + 1)
                    reverse.insert(0, current['text'])
                self.assertEqual(''.join(reverse), ''.join(pages), 'Backward pages preserve the same text')
                self.assertEqual(self.js('return audit.errors'), [])

    def turn_back(self):
        self.js("audit.previousFrame=document.querySelector('#paginated-viewer iframe')")
        self.driver.find_element('id', 'btn-prev').click()
        self.wait("document.querySelector('#paginated-viewer iframe') !== audit.previousFrame && document.querySelector('#paginated-viewer iframe')?.style.visibility === 'visible'")

    def test_reflow_modes_and_small_cross_device_advance(self):
        self.driver.set_window_size(460, 740)
        with sqlite3.connect(self.fixture.data / 'library.db') as db:
            db.execute("INSERT OR REPLACE INTO reading_progress(user_id,item_id,status,current_page,current_location,progress_percent) VALUES(1,100,'reading',0,'elidx:1',1)")
        self.open_reader('/reader/100')
        self.assertTrue(self.page()['text'].startswith('word0000'), 'Legacy anchor keeps its block')
        self.next_page()
        text = self.page()['text']
        self.js("audit.previousFrame=document.querySelector('#paginated-viewer iframe')")
        self.driver.set_window_size(640, 900)
        self.wait("document.querySelector('#paginated-viewer iframe') !== audit.previousFrame && document.querySelector('#paginated-viewer iframe')?.style.visibility === 'visible'")
        self.assertTrue(self.page()['text'].startswith(text[:60]), 'Reflow keeps the text point')
        self.driver.find_element('id', 'btn-settings').click()
        self.js("audit.previousFrame=document.querySelector('#paginated-viewer iframe')")
        self.driver.find_element('id', 'font-size-inc').click()
        self.wait("document.querySelector('#paginated-viewer iframe') !== audit.previousFrame && document.querySelector('#paginated-viewer iframe')?.style.visibility === 'visible'")
        self.assertTrue(self.page()['text'].startswith(text[:60]), 'Font change keeps the text point')
        Select(self.driver.find_element('id', 'flow-select')).select_by_value('scrolled-doc')
        self.wait("document.querySelector('#custom-scroll iframe')?.contentDocument.body.textContent.includes('word0449')")
        self.frames()
        Select(self.driver.find_element('id', 'flow-select')).select_by_value('paginated')
        self.wait("!document.getElementById('paginated-viewer').classList.contains('hidden') && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
        # The line containing the anchor can begin a few words earlier in scroll mode.
        self.assertIn(text.split()[0], self.page()['text'][:160])
        self.driver.find_element('id', 'btn-settings').click()
        # A visible iframe does not mean its asynchronous progress POST has
        # committed. Observe that acknowledgement before simulating a second
        # device; an older DB anchor plus nine characters can be BEHIND this tab.
        self.js('''const original=fetch;
            audit.progressPending=0;audit.confirmedLocation=null;
            window.fetch=async(url,options)=>{
                if(String(url)!=='/reader/100/progress' || options?.method!=='POST')
                    return original(url,options);
                audit.progressPending++;
                try {
                    const response=await original(url,options);
                    if(response.ok) audit.confirmedLocation=JSON.parse(options.body).current_location;
                    return response;
                } finally { audit.progressPending--; }
            };''')
        self.next_page()
        self.wait('audit.progressPending===0 && !!audit.confirmedLocation')
        row = self.row('SELECT current_location,progress_percent FROM reading_progress WHERE item_id=100')
        self.assertEqual(row['current_location'], self.js('return audit.confirmedLocation'))
        block, offset = map(int, row['current_location'].split(':')[1:])
        ahead = offset + 9  # one word, much less than 1% of this book
        with sqlite3.connect(self.fixture.data / 'library.db') as db:
            db.execute('UPDATE reading_progress SET current_location=? WHERE item_id=100', (f'elidx:{block}:{ahead}',))
        self.js("window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
        self.wait("document.getElementById('reader-root')?.dataset.startCfi === " + repr(f'elidx:{block}:{ahead}'))
        self.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
        self.assertTrue(self.page()['text'].startswith(WORDS[ahead:ahead+50]))
        self.assertEqual(self.row('SELECT current_location FROM reading_progress WHERE item_id=100')['current_location'], f'elidx:{block}:{ahead}', 'Leaving the stale tab must not overwrite the newer position')

    def test_readable_breaks_and_scrollable_atomic_content(self):
        self.open_reader('/reader/100')
        # Exercise the production layout algorithm in a deterministic reading
        # area. Explicit line breaks make widow/orphan checks independent of fonts.
        def layout(html, edge=0, direction='forward', height=200):
            return self.driver.execute_async_script("""
                const [html, edge, direction, height, done] = arguments;
                (async()=>{
                    document.getElementById('layout-probe')?.remove();
                    const f=document.createElement('iframe'); f.id='layout-probe';
                    f.style.cssText=`position:fixed;left:0;top:0;width:340px;height:${height}px;border:0;z-index:99999`;
                    f.sandbox='allow-same-origin';
                    f.srcdoc='<html><head><style>html,body{margin:0;overflow-y:auto}body{font:16px/20px Arial}p,h2{font:inherit;margin:0}table{border-collapse:collapse}td{padding:0}</style></head><body></body></html>';
                    const loaded=new Promise(resolve=>f.onload=resolve);document.body.append(f);await loaded;
                    const source=new DOMParser().parseFromString(html,'text/html');
                    const blocks=readerBlocks.collect(source.body);
                    const result=await readerPagination.fill(f.contentDocument.body,blocks,edge,direction,height,async()=>{f.contentDocument.body.getBoundingClientRect();await f.contentDocument.fonts.ready});
                    done({...result,html:f.contentDocument.body.innerHTML,text:f.contentDocument.body.textContent,height:f.contentDocument.body.scrollHeight});
                })().catch(error=>done({error:String(error)}));
            """, html, edge, direction, height)
        lines = lambda count: '<br>'.join(f'Line {i}' for i in range(count))
        heading = '<p>' + lines(9) + '</p><h2>Heading</h2><p>Following text</p>'
        page = layout(heading)
        self.assertNotIn('Heading', page['text'], 'A heading cannot be stranded below earlier prose')
        self.assertIn('HeadingFollowing text', layout(heading, page['end'])['text'])
        headings = '<p>' + lines(8) + '</p><h2>Heading</h2><h2>Subtitle</h2><p>Following text</p>'
        self.assertNotIn('Heading', layout(headings)['text'])
        reverse_heading = '<h2>Heading</h2><p>' + lines(10) + '</p>'
        reverse = layout(reverse_heading, 2, 'backward')
        self.assertGreater(reverse['start']['offset'], 0)
        self.assertIn('HeadingLine 0', layout(reverse_heading, reverse['start'], 'backward')['text'])

        paragraph = '<p>' + lines(7) + '</p><p id="split">' + lines(4) + '</p>'
        page = layout(paragraph)
        self.assertEqual(page['end'], {'index': 1, 'offset': 12}, 'Leave two lines on each side instead of a widow')
        self.assertLessEqual(page['height'], 200)
        kept = '<p>' + lines(6) + '</p><p style="break-inside:avoid">' + lines(6) + '</p>'
        self.assertEqual(layout(kept)['end'], {'index': 1, 'offset': 0})
        oversized = '<p style="break-inside:avoid">' + lines(30) + '</p>'
        page = layout(oversized)
        self.assertGreater(page['end']['offset'], 0, 'An impossible keep-together rule must still advance')
        self.assertLessEqual(page['height'], 200)
        explicit = '<p>Before</p><p style="break-before:page">After</p>'
        self.assertEqual(layout(explicit)['text'], 'Before')
        self.assertEqual(layout(explicit, 2, 'backward')['text'], 'After')
        self.assertEqual(layout('<p>Before</p><section style="break-before:page"><p>After</p></section>')['text'], 'Before')
        grouped = '<p>' + lines(6) + '</p><section style="break-inside:avoid"><p>' + lines(3) + '</p><p>' + lines(3) + '</p></section>'
        self.assertEqual(layout(grouped)['end'], {'index': 1, 'offset': 0})
        table = '<table><tbody>' + ''.join(f'<tr><td>Row {i}</td></tr>' for i in range(30)) + '</tbody></table><p>After table</p>'
        page = layout(table)
        self.assertEqual(page['html'].count('<tr>'), 30)
        self.assertEqual(page['end'], {'index': 1, 'offset': 0})
        self.assertGreater(page['height'], 200)
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
        ActionChains(self.driver).scroll_from_origin(ScrollOrigin.from_element(self.driver.find_element('id', 'layout-probe')), 0, 240).perform()
        self.wait("document.getElementById('layout-probe').contentDocument.scrollingElement.scrollTop > 0")
        self.assertEqual(layout(table, page['end'])['text'], 'After table')
        picture = '<figure style="height:500px"><svg width="80" height="440"><rect width="80" height="440"/></svg><figcaption>Caption</figcaption></figure><p>After picture</p>'
        page = layout(picture)
        self.assertEqual(page['html'].count('<figure'), 1)
        self.assertIn('<figcaption>Caption</figcaption>', page['html'])
        self.assertEqual(page['end'], {'index': 1, 'offset': 0})
        unicode_text = '漢字 é 🌿 👨‍👩‍👧‍👦 ' * 90
        html = '<p>' + unicode_text + '</p>'
        page = layout(html)
        text = page['text']
        for _ in range(50):
            if page['end']['index'] == 1:
                break
            self.assertTrue(self.driver.execute_script("return [...new Intl.Segmenter(undefined,{granularity:'grapheme'}).segment(arguments[0])].some(s=>s.index===arguments[1])", unicode_text, page['end']['offset']), 'Split only between grapheme clusters')
            page = layout(html, page['end'])
            text += page['text']
        self.assertEqual(text, unicode_text)
        tiny = '<p>' + lines(3) + '</p>'
        edge = 0
        tiny_text = ''
        for _ in range(4):
            page = layout(tiny, edge, height=12)
            self.assertTrue(page['text'], 'Even a viewport shorter than one line must advance')
            tiny_text += page['text']
            edge = page['end']
            if edge['index'] == 1:
                break
        self.assertEqual(tiny_text, 'Line 0Line 1Line 2')




if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    parser.add_argument('--chrome-binary', default='/usr/bin/chromium')
    parser.add_argument('--chromedriver', default='/usr/bin/chromedriver')
    parser.add_argument('--firefox-binary', default='/usr/bin/firefox')
    parser.add_argument('--geckodriver', default='/usr/bin/geckodriver')
    parser.add_argument('--test', choices=[name for name in PaginationChecks.__dict__ if name.startswith('test_')])
    audit.ARGS = parser.parse_args()
    suite = unittest.TestSuite(type(browser.title() + 'Pagination', (PaginationChecks,), {'browser': browser})(name) for browser in selected_browsers(audit.ARGS) for name in PaginationChecks.__dict__ if name.startswith('test_') and (not audit.ARGS.test or audit.ARGS.test == name))
    expected = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    unittest_failures(result)
    sys.exit(not baseline_passed(result, expected))
