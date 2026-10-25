"""Visual hyphenation, language and stable text anchors in both desktop engines."""
import argparse
import io
import json
from pathlib import Path
import sqlite3
import sys
import unittest
import zipfile

from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.support.ui import WebDriverWait

from browser_checks import add_browser_argument, selected_browsers, baseline_passed, mutation_rejected
from ci_report import unittest_failures
import test_audit_browser as audit
import test_pagination_browser as pagination

ROOT = Path(__file__).resolve().parents[1]
TEXT = ('representation internationalization extraordinary characterization cooperation ' * 60).strip()
CONTENT = '<p id="hyphenated">' + TEXT + '</p><p>Final paragraph.</p>'


class HyphenationChecks(pagination.PaginationChecks):
    @classmethod
    def seed_readers(cls):
        super().seed_readers()
        with sqlite3.connect(cls.fixture.data / 'library.db') as db:
            archive = db.execute('SELECT file_path FROM items WHERE id=100').fetchone()[0]
            with zipfile.ZipFile(archive) as book:
                files = {name: book.read(name) for name in book.namelist()}
            files['OEBPS/ch0.xhtml'] = '<html><head></head><body>' + CONTENT + '</body></html>'
            with zipfile.ZipFile(archive, 'w') as book:
                for name, content in files.items():
                    book.writestr(name, content)
            db.execute('UPDATE items SET metadata=? WHERE id=100', (json.dumps({'language': 'en-US'}),))
            db.execute('UPDATE chapters SET content_html=?,content_size=? WHERE id=100', (CONTENT, len(CONTENT)))

    def open_hyphen_reader(self, path):
        self.driver.get(self.base + '/')
        self.js("localStorage.setItem('text-reader-prefs',JSON.stringify({flow:'paginated',font:'original',fontSize:100,lineHeight:1.6,width:720,bottomGap:0}))")
        self.driver.get(self.base + path)
        self.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")

    def change(self, selector, action):
        self.js("audit.previousFrame=document.querySelector('#paginated-viewer iframe')")
        action(self.driver.find_element('id', selector))
        self.wait("document.querySelector('#paginated-viewer iframe')!==audit.previousFrame && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")

    def language(self, value):
        def enter(element):
            element.clear()
            if value:
                element.send_keys(value)
            element.send_keys(Keys.TAB)
        self.change('hyphenation-language', enter)

    def frame_state(self):
        return self.js("""const f=document.querySelector('#paginated-viewer iframe'),d=f.contentDocument;
            return {language:d.documentElement.lang,mode:d.documentElement.dataset.readerHyphenation,
                text:d.body.textContent,hyphens:d.defaultView.getComputedStyle(d.querySelector('p')).hyphens};""")

    def test_work_language_toggle_and_resume_preserve_source_anchors(self):
        for path in ('/reader/100', '/novels/100/read/1/1'):
            with self.subTest(path=path):
                self.open_hyphen_reader(path)
                self.assertFalse(self.driver.find_element('id', 'hyphenation-enabled').is_selected(),
                                 'Hyphenation must default off independently for each work')
                self.driver.find_element('id', 'btn-settings').click()
                if path.startswith('/reader/'):
                    self.assertEqual(self.frame_state()['language'], 'en-US', 'Use EPUB metadata as a fallback')
                    self.change('hyphenation-enabled', lambda element: element.click())
                else:
                    self.change('hyphenation-enabled', lambda element: element.click())
                    self.assertEqual(self.frame_state()['mode'], 'manual', 'Missing language must not guess a dictionary')
                    self.assertIn('Set a language', self.driver.find_element('id', 'hyphenation-status').text)
                    self.language('en-US')
                self.assertEqual(self.frame_state()['mode'], 'auto', 'Enabled language must activate native hyphenation')
                self.assertEqual(self.frame_state()['hyphens'], 'auto')
                self.driver.find_element('id', 'settings-close').click()
                self.show_controls()
                self.next_page()
                anchor = self.frame_state()['text'][:50]
                sql = ('SELECT current_location AS location FROM reading_progress WHERE item_id=100'
                       if path.startswith('/reader/') else
                       "SELECT 'elidx:' || paragraph_index || ':' || character_offset AS location FROM novel_reading_progress WHERE novel_id=100")
                WebDriverWait(self.driver, 20).until(lambda _: (self.row(sql) or {}).get('location', '').count(':') == 2)
                self.driver.find_element('id', 'btn-settings').click()
                self.change('hyphenation-enabled', lambda element: element.click())
                self.assertTrue(self.frame_state()['text'].startswith(anchor), 'Toggling must preserve the exact source anchor')
                self.change('hyphenation-enabled', lambda element: element.click())
                self.assertTrue(self.frame_state()['text'].startswith(anchor), 'Enabling must preserve the exact source anchor')
                before = self.row(sql)['location']
                self.driver.refresh()
                self.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
                self.assertTrue(self.frame_state()['text'].startswith(anchor), 'Resume must preserve the exact source anchor')
                self.assertEqual(self.row(sql)['location'], before)
                self.assertTrue(self.driver.find_element('id', 'hyphenation-enabled').is_selected(),
                                'Retain the preference for this work on this device')
                self.assertNotIn('\u00ad', self.frame_state()['text'], 'Visual hyphens must never rewrite text')
        print(f'{self.browser} {self.viewport_width}px: EPUB and novel toggle, metadata/manual language and resume passed', flush=True)

    def test_continuous_reflow_remeasures_without_changing_saved_position(self):
        from selenium.webdriver.support.ui import Select
        for path in ('/reader/100', '/novels/100/read/1/1'):
            with self.subTest(path=path):
                self.open_hyphen_reader(path)
                self.driver.find_element('id', 'btn-settings').click()
                self.language('en-US')
                self.change('hyphenation-enabled', lambda element: element.click())
                Select(self.driver.find_element('id', 'flow-select')).select_by_value('scrolled-doc')
                self.wait("document.querySelector('#custom-scroll iframe')?.style.visibility==='visible'")
                self.driver.find_element('id', 'settings-close').click()
                ActionChains(self.driver).scroll_from_origin(ScrollOrigin.from_element(
                    self.driver.find_element('id', 'custom-scroll')), 0, 240).perform()
                self.wait("document.getElementById('custom-scroll').scrollTop>100")
                sql = ('SELECT current_location AS location FROM reading_progress WHERE item_id=100'
                       if path.startswith('/reader/') else
                       "SELECT 'elidx:' || paragraph_index || ':' || character_offset AS location FROM novel_reading_progress WHERE novel_id=100")
                WebDriverWait(self.driver, 20).until(lambda _: (self.row(sql) or {}).get('location', '').count(':') == 2)
                saved = self.row(sql)['location']
                block, offset = map(int, saved.split(':')[1:])
                self.driver.find_element('id', 'btn-settings').click()
                self.driver.find_element('id', 'hyphenation-enabled').click()
                self.wait("!document.querySelector('#custom-scroll iframe').contentDocument.documentElement.dataset.readerHyphenation")
                state = self.driver.execute_async_script("""const [index,offset,done]=arguments;
                    requestAnimationFrame(()=>requestAnimationFrame(()=>{
                        const c=document.getElementById('custom-scroll'),f=c.querySelector('iframe'),d=f.contentDocument;
                        const blocks=readerBlocks.collect(d.body),y=readerPagination.topAt(blocks[index],offset);
                        done({top:y+f.parentElement.offsetTop-c.scrollTop,height:d.documentElement.scrollHeight,frame:f.clientHeight});
                    }));""", block, offset)
                self.assertLessEqual(state['height'], state['frame']+1, 'Continuous hyphenation reflow must remeasure frame height')
                self.assertAlmostEqual(state['top'], 0, delta=2, msg='Continuous reflow must keep the source text anchor on its line')
                self.assertEqual(self.row(sql)['location'], saved, 'Layout changes must not advance the saved reading position')
        print(f'{self.browser} {self.viewport_width}px: continuous layout and saved source anchors preserved', flush=True)

    def test_readable_offsets_skip_media_without_moving_source_anchor(self):
        self.open_hyphen_reader('/reader/100')
        source = (ROOT / 'app/static/js/reader-text.js').read_text()
        code = source[source.index('    function timingText(block) {'):source.index('    let previousTiming = null;')]
        result = self.driver.execute_script("""const source=new DOMParser().parseFromString(
            '<p>First <svg><text>caption</text></svg> second third</p>','text/html');
            const block=readerBlocks.collect(source.body)[0],dom=source.body.textContent;
            const start=dom.indexOf('second'),end=start+'second'.length;
            const passage=new Function('blocks','start','end',arguments[0]+';return timingPassage(blocks,start,end);')(
                [block],{index:0,offset:start},{index:0,offset:end});
            return {passage,source:dom,start,end};""", code)
        self.assertEqual(result['source'], 'First caption second third')
        self.assertEqual(result['start'], 14, 'The saved anchor must retain the full DOM offset')
        self.assertEqual(len(result['passage']), 1, 'Reading evidence must map DOM offsets past skipped media text')
        part = result['passage'][0]
        self.assertEqual(part['text'][part['start']:part['end']], 'second',
                         'Reading evidence must map DOM offsets past skipped media text')
        self.assertEqual((part['start'], part['end']), (7, 13))

    def layout(self, text, *, language='en-US', extra='', edge=0, direction='forward', height=96, mode='auto'):
        return self.driver.execute_async_script("""const [text,language,extra,edge,direction,height,mode,done]=arguments;
            (async()=>{
                document.getElementById('hyphenation-probe')?.remove();
                const f=document.createElement('iframe');f.id='hyphenation-probe';f.sandbox='allow-same-origin';
                f.style.cssText=`position:fixed;top:0;left:0;width:143px;height:${height}px;z-index:999999;border:0`;
                const current=document.querySelector('#paginated-viewer iframe').contentDocument;
                const css=current.getElementById('reader-theme-style').textContent;
                const loaded=new Promise(resolve=>f.onload=resolve);
                f.srcdoc='<html lang="'+language+'"'+(mode?' data-reader-hyphenation="'+mode+'"':'')+'><head><style>'+css+
                    'html,body{margin:0!important;padding:0!important}body{font:20px/24px serif!important;max-width:none!important}p{margin:0!important;'+extra+'}</style></head><body></body></html>';
                document.body.append(f);await loaded;
                const d=f.contentDocument,source=new DOMParser().parseFromString('<p>'+text+'</p>','text/html');
                const blocks=readerBlocks.collect(source.body);
                const result=await readerPagination.fill(d.body,blocks,edge,direction,height,async()=>{
                    d.body.getBoundingClientRect();await d.fonts.ready;
                });
                const markers=[...d.querySelectorAll('[data-reader-continuation-hyphen]')].map(e=>({
                    text:e.textContent,content:d.defaultView.getComputedStyle(e,'::after').content,
                    hidden:e.getAttribute('aria-hidden')}));
                done({...result,text:d.body.textContent,height:d.body.scrollHeight,viewport:f.clientHeight,markers,hyphens:d.defaultView.getComputedStyle(d.querySelector('p')).hyphens});
            })().catch(error=>done({error:String(error)}));""", text, language, extra, edge, direction, height, mode)

    def test_native_hyphenated_page_edges_keep_visible_glyphs_and_original_text(self):
        self.open_hyphen_reader('/reader/100')
        edge = 0
        pieces = []
        markers = 0
        snapshots = []
        text = ('representation extraordinary internationalization ' * 7).strip()
        for _ in range(80):
            page = self.layout(text, edge=edge)
            self.assertNotIn('error', page)
            self.assertTrue(page['text'], 'Hyphenated pagination must always advance')
            self.assertLessEqual(page['height'], page['viewport'] + 1, 'Hyphenation must not overflow the reading page')
            pieces.append(page['text'])
            snapshots.append(page)
            markers += len(page['markers'])
            for marker in page['markers']:
                self.assertEqual(marker['text'], '', 'Continuation glyph must be visual CSS only')
                self.assertEqual(marker['hidden'], 'true')
                self.assertIn('‐', marker['content'], 'A hyphenated page edge must retain its visible hyphen')
            edge = page['end']
            if edge['index'] == 1:
                break
        self.assertEqual(''.join(pieces), text, 'Every source character must occur exactly once across pages')
        self.assertGreater(markers, 0, 'The selected en-US dictionary must genuinely hyphenate a word at a page boundary')
        # Recover pages in the opposite direction from the exact saved text edge.
        reverse = []
        edge = {'index': 1, 'offset': 0}
        for _ in range(80):
            page = self.layout(text, edge=edge, direction='backward')
            self.assertNotIn('error', page)
            reverse.insert(0, page['text'])
            edge = page['start']
            if edge == {'index': 0, 'offset': 0}:
                break
        self.assertEqual(''.join(reverse), text, 'Backward pagination must preserve the original text')
        print(f'{self.browser} {self.viewport_width}px: {markers} genuine hyphenated page boundaries; source text unchanged', flush=True)

    def test_original_hyphenation_rules_and_explicit_override(self):
        self.open_hyphen_reader('/reader/100')
        original = self.layout('representation extraordinary ' * 7,
                               extra='hyphens:auto!important', mode=None)
        self.assertEqual(original['hyphens'], 'auto', 'Off must preserve the book’s original hyphenation rules')
        override = self.layout('representation extraordinary ' * 7,
                               extra='hyphens:none!important')
        self.assertEqual(override['hyphens'], 'auto', 'An explicit work preference must override original hyphenation rules')
        unknown = self.layout('representation extraordinary ' * 7, language='',
                              extra='hyphens:auto!important', mode='manual')
        self.assertEqual(unknown['hyphens'], 'manual', 'Missing language must not inherit automatic book hyphenation')

    def test_cjk_and_break_all_wrapping_do_not_invent_hyphens(self):
        self.open_hyphen_reader('/reader/100')
        for text, language, extra in (('漢字文章閱讀測試' * 20, 'zh', ''),
                                      ('representationextraordinary' * 14, 'en-US', 'word-break:break-all!important')):
            with self.subTest(language=language, extra=extra):
                page = self.layout(text, language=language, extra=extra)
                self.assertNotIn('error', page)
                self.assertEqual(page['markers'], [], 'Ordinary wrapping must never invent a hyphen glyph')
                self.assertGreater(page['end']['offset'], 0, 'The control must actually wrap across a page')


def suite(*, controls=False, test=None):
    names = [test] if test else [name for name in HyphenationChecks.__dict__ if name.startswith('test_')]
    widths = (1280,) if controls else (1280, 320)
    return unittest.TestSuite(type(browser.title() + 'Hyphenation', (HyphenationChecks,), {
        'browser': browser, 'viewport_width': width})(name)
        for browser in selected_browsers(ARGS) for width in widths for name in names)


def run_mutation_controls():
    controls = (
        ('app/static/js/reader-text.js', 'const from = readable.offsetAt(i === start.index ? start.offset || 0 : 0);',
         'const from = Math.min(readable.text.length, i === start.index ? start.offset || 0 : 0);',
         'test_readable_offsets_skip_media_without_moving_source_anchor', 'Reading evidence must map DOM offsets'),
        ('app/static/js/reader-text.js', "hyphenation: saved.hyphenation === true", 'hyphenation: true',
         'test_work_language_toggle_and_resume_preserve_source_anchors', 'Hyphenation must default off'),
        ('app/static/js/reader-pagination.js', 'if (hyphenCharacter && to < size)', 'if (false)',
         'test_native_hyphenated_page_edges_keep_visible_glyphs_and_original_text', 'genuinely hyphenate'),
    )
    for filename, before, after, test, signal in controls:
        path = ROOT / filename
        original = path.read_text()
        if original.count(before) != 1:
            raise ValueError('Hyphenation mutation must match exactly once.')
        try:
            path.write_text(original.replace(before, after))
            output = io.StringIO()
            result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite(controls=True, test=test))
        finally:
            path.write_text(original)
        if not mutation_rejected(result, selected_browsers(ARGS), 'Hyphenation', signal):
            print(output.getvalue())
            raise AssertionError('Every browser must reject the intended hyphenation defect.')
        print(' and '.join(selected_browsers(ARGS)) + ' rejected: ' + signal, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    parser.add_argument('--chrome-binary', default='/usr/bin/chromium')
    parser.add_argument('--chromedriver', default='/usr/bin/chromedriver')
    parser.add_argument('--firefox-binary', default='/usr/bin/firefox')
    parser.add_argument('--geckodriver', default='/usr/bin/geckodriver')
    parser.add_argument('--test', choices=[name for name in HyphenationChecks.__dict__ if name.startswith('test_')])
    parser.add_argument('--no-mutations', action='store_true', help='Only for focused local diagnosis; full validation includes controls')
    ARGS = parser.parse_args()
    audit.ARGS = ARGS
    selected = suite(test=ARGS.test)
    expected = selected.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(selected)
    unittest_failures(result)
    if not baseline_passed(result, expected):
        sys.exit(1)
    if ARGS.no_mutations or ARGS.test:
        sys.exit(0)
    run_mutation_controls()
