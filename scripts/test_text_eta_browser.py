"""Live word-based ETA in real Chrome/Firefox desktop and 320px viewports."""
import argparse
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import shutil
import sys
import time
import unittest
import uuid
import zipfile

from selenium.webdriver.common.keys import Keys

import requests
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait

from browser_checks import add_browser_argument, selected_browsers, baseline_passed, mutation_rejected
from ci_report import unittest_failures
import test_audit_browser as audit

ROOT = Path(__file__).resolve().parents[1]
PROBE = """() => {
    window.etaProbe={document:crypto.randomUUID(),errors:[],connections:0,requests:0,seen:[]};
    new MutationObserver(()=>{
        const node=document.querySelector('[data-text-eta-kind=novel][data-text-eta-id=\"200\"]');
        if(node) etaProbe.seen.push(node.textContent);
    }).observe(document,{childList:true,subtree:true,characterData:true});
    window.addEventListener('error',e=>etaProbe.errors.push(String(e.error||e.message)));
    window.addEventListener('unhandledrejection',e=>etaProbe.errors.push(String(e.reason)));
    document.addEventListener('htmx:sse:after:connection',()=>etaProbe.connections++);
    const original=fetch;
    window.fetch=(...args)=>{
        if(String(args[0])==='/library/reading-estimates') {
            etaProbe.requests++;
            if(sessionStorage.getItem('hold-estimates')) return new Promise(resolve=>{
                etaProbe.release=()=>resolve(original(...args));etaProbe.held=true;
            });
            if(etaProbe.fail) return Promise.resolve(new Response('',{status:503}));
        }
        return original(...args);
    };
}"""


class TextEtaChecks(audit.AuditChecks):
    probe = PROBE

    @classmethod
    def seed_readers(cls):
        # Keep the shared real-server fixture's comic covers, adding a known
        # novel with two scopes. The historical rows are fixtures, not clocks
        # injected into the reader or records from an existing account.
        with closing(sqlite3.connect(cls.fixture.data / 'library.db')) as db, db:
            db.execute("INSERT INTO novels(id,title,source_url,sync_status,active) VALUES(100,'ETA fixture','https://example.invalid/eta','idle',0)")
            db.executemany('INSERT INTO volumes(id,novel_id,number) VALUES(?,100,?)',[(100,1),(101,2)])
            for chapter,volume,words in ((100,100,1000),(101,101,1000)):
                source='<p>'+'word '*words+'</p>'
                db.execute("""INSERT INTO chapters
                    (id,novel_id,volume_id,number,title,url,order_index,content_html,content_size)
                    VALUES (?,100,?,?,'Chapter',?,?,?,?)""",
                    (chapter,volume,chapter-99,f'https://example.invalid/{chapter}',chapter-100,source,len(source)))
            db.execute("""INSERT INTO novel_reading_progress
                (user_id,novel_id,chapter_id,scroll_percent,chapter_percent,paragraph_index,character_offset,updated_at)
                VALUES (1,100,100,99,99,0,200,datetime('now'))""")
            # Sufficient words/time, but only TWO samples: every text card must
            # initially say Collecting data until an actual third visit commits.
            for index in range(2):
                db.execute("""INSERT INTO page_reads
                    (user_id,kind,started_at,seconds,cap_seconds,visit_id,words,pace_words,pace_seconds,pace_samples)
                    VALUES (1,'epub',datetime('now'),150,150,?,1500,1500,150,1)""",(f'fixture-{index}',))
            db.execute("""INSERT INTO page_reads(user_id,kind,started_at,seconds,cap_seconds)
                VALUES (1,'epub','2025-01-01',1000,180)""")

    def test_samples_refresh_estimates_and_preserve_document_covers_and_scope(self):
        self.driver.get(self.base+'/')
        self.wait('etaProbe.connections>0')
        self.wait("document.querySelector('[data-text-eta-kind=novel][data-text-eta-volume]')?.textContent==='Collecting data'")
        self.js("""etaProbe.cover=document.querySelector('#series-grid img');
            etaProbe.reading=document.querySelector('[data-text-eta-kind=novel][data-text-eta-volume]');
            etaProbe.whole=document.querySelector('#series-grid [data-text-eta-kind=novel]');""")
        marker=self.js('return etaProbe.document')
        with requests.Session() as session:
            session.cookies.update({cookie['name']:cookie['value'] for cookie in self.driver.get_cookies()})
            identity=uuid.uuid4().hex
            claim=session.post(self.base+'/reading-session',json={'session_id':identity,'claim_id':uuid.uuid4().hex},timeout=10)
            claim.raise_for_status()
            started=claim.json()['now']
            # This visit is measured against the real server clock. It completes
            # source-validated text and must itself publish the real SSE event.
            time.sleep(1.2)
            text='word '*1000
            activity={'id':uuid.uuid4().hex,'session_id':identity,'revision':1,
                'chapter_id':100,'page_index':0,'character_offset':0,
                'passage':[{'text':text,'start':0,'end':len(text)}],
                'intervals':[[started,started+1.1]],'closed':True,'completed':True}
            response=session.post(self.base+'/reading-time',json={'activities':[activity]},timeout=10)
            response.raise_for_status()
            self.assertFalse(response.json()['rejected'])
            try:
                WebDriverWait(self.driver,10).until(lambda _: self.js("return etaProbe.reading.textContent.startsWith('~')"))
            except TimeoutException:
                pass
            self.assertTrue(self.js("return etaProbe.reading.textContent.startsWith('~')"),
                            'New valid reading activity must refresh Collecting data through SSE')
            self.assertGreater(self.js('return etaProbe.requests'),0)
            self.assertNotEqual(self.js('return etaProbe.reading.textContent'),self.js('return etaProbe.whole.textContent'),
                                'Reading ETA must retain its volume scope')
            values=session.post(self.base+'/library/reading-estimates',json={'targets':[
                {'kind':'novel','id':100,'volume_id':100},{'kind':'novel','id':100}]},timeout=10).json()['estimates']
            # 200 source UTF-16 units cover 40 words: 960 remain in this
            # volume, 1960 across the novel. Displayed 99% is deliberately false.
            rate=(300+1.1)/4000
            self.assertAlmostEqual(values[0]['remaining'],960*rate,places=5)
            self.assertAlmostEqual(values[1]['remaining'],1960*rate,places=5)
            # A progress save with the same percent but a later true anchor
            # must recompute words instead of scaling the last total by 99%.
            previous=self.js('return etaProbe.reading.textContent')
            session.post(self.base+'/novels/100/progress',json={'chapter_id':100,'paragraph_index':0,
                'character_offset':2500,'chapter_percent':99,'scroll_percent':99},timeout=10).raise_for_status()
            self.wait('!etaProbe.reading.isConnected || etaProbe.reading.textContent!=='+repr(previous))
            self.assertTrue(self.js('return etaProbe.reading.isConnected'),
                            'Same-chapter progress must preserve the existing Reading card')
            previous=self.js('return etaProbe.reading.textContent')
            session.post(self.base+'/settings/text-replacements', data={
                'target':'novel:100','find_text':'word','replace_text':'word extra'},
                headers={'HX-Request':'true'},timeout=10).raise_for_status()
            try:
                WebDriverWait(self.driver,10).until(lambda _: self.js('return etaProbe.reading.textContent')!=previous)
            except TimeoutException:
                pass
            self.assertNotEqual(self.js('return etaProbe.reading.textContent'),previous,
                                'Replacement changes must refresh an available estimate')
        self.assertEqual(self.js('return etaProbe.document'),marker,'ETA refresh must not reload the document')
        self.assertTrue(self.js('return etaProbe.reading.isConnected && etaProbe.whole.isConnected && etaProbe.cover.isConnected'),
                        'ETA refresh must preserve existing cards and covers')
        self.assertEqual(self.row("SELECT seconds,cap_seconds FROM page_reads WHERE started_at='2025-01-01'"),
                         {'seconds':1000,'cap_seconds':180},'Legacy reading history must remain unchanged')
        self.assertEqual(self.js('return etaProbe.errors'),[])



class EstimateAvailabilityChecks(TextEtaChecks):
    @classmethod
    def seed_readers(cls):
        super().seed_readers()
        path = cls.fixture.fixture / 'estimate.epub'
        with zipfile.ZipFile(path, 'w') as book:
            book.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
            book.writestr('content.opf', '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="a" href="a.xhtml"/></manifest><spine><itemref idref="a"/></spine></package>')
            book.writestr('a.xhtml', '<p>'+'word '*1000+'</p>')
        with closing(sqlite3.connect(cls.fixture.data / 'library.db')) as db, db:
            db.execute("INSERT INTO page_reads(user_id,kind,started_at,seconds,cap_seconds,visit_id,pace_words,pace_seconds,pace_samples) VALUES(1,'epub',datetime('now'),150,150,'availability-fixture',1500,150,1)")
            db.execute("INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(200,1,'Partial ETA','partial eta','/partial')")
            for identity, source in ((200,path),(201,path.with_name('missing.epub'))):
                db.execute("INSERT INTO items(id,series_id,library_id,title,sort_title,file_path,file_format,file_hash,file_size) VALUES(?,200,1,?,?,?,'epub',?,100)",
                           (identity, f'ETA {identity}',f'eta{identity}',str(source),str(identity)))
            db.execute("INSERT INTO novels(id,title,source_url,active) VALUES(200,'Large ETA','https://example.invalid/large',0)")
            db.execute('INSERT INTO volumes(id,novel_id,number) VALUES(200,200,1)')
            for identity in range(200,265):
                db.execute("INSERT INTO chapters(id,novel_id,volume_id,title,url,content_html,content_size) VALUES(?,200,200,'Chapter',?,'<p>one two</p>',14)",
                           (identity,f'https://example.invalid/large/{identity}'))

    def test_saved_partial_estimates_and_retry(self):
        self.driver.get(self.base+'/')
        self.wait("document.querySelector('[data-text-eta-kind=novel][data-text-eta-id=\"200\"]')?.textContent.startsWith('~')")
        self.wait("document.querySelector('[data-text-eta-kind=series][data-text-eta-id=\"200\"]')?.textContent.includes('known · 1 item without estimate')")
        self.assertTrue(self.js("const node=document.querySelector('[data-text-eta-kind=series][data-text-eta-id=\"200\"]');return node.scrollWidth<=node.clientWidth"),
                        'The partial total must be readable without clipping')
        self.js("sessionStorage.setItem('hold-estimates','1')")
        self.driver.refresh()
        self.wait('etaProbe.held')
        self.assertTrue(self.js("return document.querySelector('[data-text-eta-kind=novel][data-text-eta-id=\"200\"]').textContent.startsWith('~')"),
                        'Reload must show the saved novel estimate before refresh completes')
        self.assertTrue(self.js("return etaProbe.seen.every(text=>text.startsWith('~'))"),
                        'A reload must never flash Collecting data over a saved estimate')
        self.assertIn('known · 1 item without estimate', self.js("return document.querySelector('[data-text-eta-kind=series][data-text-eta-id=\"200\"]').textContent"),
                      'Series must identify its partial known total')
        self.js("sessionStorage.removeItem('hold-estimates');etaProbe.release()")
        self.wait("document.querySelector('[data-text-eta-kind=series][data-text-eta-id=\"200\"]')?.getAttribute('role')==='button'")
        marker=self.js('return etaProbe.document')
        self.js("etaProbe.fail=true;document.querySelector('[data-text-eta-kind=series][data-text-eta-id=\"200\"]').click()")
        self.wait("document.querySelector('[data-text-eta-kind=novel][data-text-eta-id=\"200\"]').dataset.estimateState==='temporary_failure'")
        self.assertTrue(self.js("return document.querySelector('[data-text-eta-kind=novel][data-text-eta-id=\"200\"]').textContent.startsWith('~')"),
                        'Transport failure must retain a known result')
        self.js('etaProbe.fail=false')
        shutil.copyfile(self.fixture.fixture / 'estimate.epub', self.fixture.fixture / 'missing.epub')
        node=self.driver.find_element('css selector','[data-text-eta-kind=series][data-text-eta-id="200"]')
        node.send_keys(Keys.ENTER)
        self.wait("document.querySelector('[data-text-eta-kind=series][data-text-eta-id=\"200\"]').dataset.estimateState==='available'")
        self.assertNotIn('without estimate', node.text, 'Retry must include repaired content in the full total')
        self.assertEqual(self.js('return etaProbe.document'),marker,'Retry must preserve the document')
        self.assertEqual(self.js('return etaProbe.errors'),[])


def suite(*, mobile=True, availability=None):
    return unittest.TestSuite(type(browser.title()+'TextEta'+str(width)+test,(checks,),
        {'browser':browser,'viewport_width':width})(test)
        for browser in selected_browsers(ARGS) for width in ((1280,320) if mobile else (1280,))
        for checks,test in ((TextEtaChecks,'test_samples_refresh_estimates_and_preserve_document_covers_and_scope'),
                            (EstimateAvailabilityChecks,'test_saved_partial_estimates_and_retry'))
        if availability is None or (checks is EstimateAvailabilityChecks) == availability)



if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    for name,default in (('chrome-binary','/usr/bin/chromium'),('chromedriver','/usr/bin/chromedriver'),
                         ('firefox-binary','/usr/bin/firefox'),('geckodriver','/usr/bin/geckodriver')):
        parser.add_argument('--'+name,default=default)
    ARGS=parser.parse_args()
    audit.ARGS=ARGS
    result=unittest.TextTestRunner(verbosity=2).run(suite())
    unittest_failures(result)
    if not baseline_passed(result,len(selected_browsers(ARGS))*4):
        sys.exit(1)
    for filename,before,after,signal in (
        ('app/account_events.py', "if path.startswith('/settings/text-replacements'):", 'if False:',
         'Replacement changes must refresh an available estimate'),
        ('app/static/js/library-events.js','reading_estimate_updated: () => scheduleTextEstimates(),',
         'reading_estimate_updated: () => {},','New valid reading activity must refresh Collecting data through SSE'),
        ('app/static/js/library-events.js',
         'if (el.dataset.chapterId !== undefined && data.chapter_id !== undefined && el.dataset.chapterId !== String(data.chapter_id)) {',
         'if (data.chapter_id !== undefined && el.dataset.chapterId !== String(data.chapter_id)) {',
         'Same-chapter progress must preserve the existing Reading card'),
        ('app/library/router.py','parsed[key] = (kind, identity, volume)','parsed[key] = (kind, identity, None)',
         'Reading ETA must retain its volume scope'),
        ('app/stats/estimates.py', 'if saved is not None:', 'if False and saved is not None:',
         'Reload must show the saved novel estimate before refresh completes'),
        ('app/templates/library/index.html', '{% if card.eta_missing %}', '{% if false %}',
         'Series must identify its partial known total'),
        ('app/static/js/library-events.js', 'if (!node.textContent.startsWith("~")) node.textContent = "Estimate failed · retry";',
         'node.textContent = "Estimate failed · retry";', 'Transport failure must retain a known result'),
        ('app/templates/library/index.html', 'class="text-xs text-neutral-500" data-text-eta-kind=',
         'class="text-xs text-neutral-500 truncate" data-text-eta-kind=',
         'The partial total must be readable without clipping'),
    ):
        path=ROOT/filename
        original=path.read_text()
        if original.count(before)!=1 and before != 'if saved is not None:':
            raise ValueError('Text ETA mutation must match exactly once')
        try:
            changed=original.replace(before,after)
            if 'prepare_novel' in before:
                changed=changed.replace('estimator.for_novel(identity, volume)','estimator.for_novel(identity, None)')
                changed=changed.replace('estimator.novel_states.get((identity, volume)', 'estimator.novel_states.get((identity, None)')
            path.write_text(changed)
            output=io.StringIO()
            control=unittest.TextTestRunner(stream=output,verbosity=2).run(suite(mobile=False, availability=signal in (
                "Reload must show the saved novel estimate before refresh completes",
                "Series must identify its partial known total", "Transport failure must retain a known result",
                "The partial total must be readable without clipping")))
        finally:
            path.write_text(original)
        if not mutation_rejected(control,selected_browsers(ARGS),'TextEta',signal):
            print(output.getvalue())
            raise AssertionError('Each selected browser must reject the text ETA defect')
        print(' and '.join(browser.title() for browser in selected_browsers(ARGS))+' rejected: '+signal,flush=True)

    from test_reload_performance_browser import run as run_reload_checks
    run_reload_checks(ARGS)
