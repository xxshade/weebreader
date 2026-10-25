"""Real HTTP reloads on a warmed text shelf, including automatic ETA requests.

Run on Linux with the same browser flags as test_text_eta_browser.py. Native
Android/LibreWolf are not exercised. The 500ms budget applies to median page
usability across three repeats per action on the disposable local fixture.
"""
import argparse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sqlite3
import statistics
import time

import requests
import unittest
import zipfile

import test_audit_browser as audit
from browser_checks import add_browser_argument, baseline_passed, mutation_rejected, selected_browsers
from ci_report import unittest_failures

ROOT = Path(__file__).resolve().parents[1]
PROBE = """() => {
    window.reloadProbe={marker:crypto.randomUUID(),started:0,usable:null,pending:0,done:0,lastEta:0,errors:[]};
    window.addEventListener('error',e=>reloadProbe.errors.push(String(e.error||e.message)));
    window.addEventListener('unhandledrejection',e=>reloadProbe.errors.push(String(e.reason)));
    const original=fetch;
    window.fetch=async(...args)=>{
        const eta=String(args[0])==='/library/reading-estimates';
        if(eta) { reloadProbe.pending++;reloadProbe.lastEta=performance.now(); }
        try {
            const response=await original(...args);
            if(eta) await response.clone().text();
            return response;
        } finally { if(eta) { reloadProbe.pending--;reloadProbe.done++;reloadProbe.lastEta=performance.now(); } }
    };
    document.addEventListener('click',e=>{
        if(e.target.closest('a[aria-label="Library"]')) {
            reloadProbe.started=performance.now();reloadProbe.usable=null;
        }
    },true);
    let measuring=false;
    function frame() {
        const grid=document.querySelector('#series-grid');
        if(!measuring && reloadProbe.usable===null && location.pathname==='/' && grid &&
           grid.getBoundingClientRect().width>0 && window.libraryGrid?.isLive() &&
           document.querySelector('#series-search')) {
            measuring=true;
            requestAnimationFrame(()=>requestAnimationFrame(()=>{
                if(grid.isConnected) reloadProbe.usable=performance.now()-reloadProbe.started;
                measuring=false;
            }));
        }
        requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
}"""


def seed_text_shelf(data, folder):
    """151 distinct real archives and 1,993 distinct chapter bodies."""
    books = folder / 'large-epubs'
    books.mkdir()
    paragraph = '<p>'+'word '*250+'</p>'
    with closing(sqlite3.connect(data / 'library.db')) as db, db:
        db.execute("INSERT INTO libraries(id,name,root_path,media_type) VALUES(2,'Text',?,'ebook')", (str(books),))
        for index in range(151):
            identity, series = 1000+index, 1000+index//6
            db.execute("INSERT OR IGNORE INTO series(id,library_id,name,sort_name,folder_path) VALUES(?,2,?,?,?)",
                       (series,f'Text {series}',f'text{series}',str(books / str(series))))
            path = books / f'{identity}.epub'
            with zipfile.ZipFile(path, 'w') as book:
                book.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
                book.writestr('content.opf', '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="a" href="a.xhtml"/></manifest><spine><itemref idref="a"/></spine></package>')
                book.writestr('a.xhtml', f'<html><body><p>Book {identity}</p>'+paragraph*400+'</body></html>')
            db.execute("INSERT INTO items(id,library_id,series_id,title,sort_title,file_path,file_format,file_hash,file_size) VALUES(?,2,?,?,?,?,'epub',?,?)",
                       (identity,series,f'Book {identity}',f'book{identity}',str(path),str(identity),path.stat().st_size))
            if index < 10:
                db.execute("INSERT INTO reading_progress(user_id,item_id,status,current_page,current_location,progress_percent,last_read_at) VALUES(1,?,'reading',0,'elidx:1:4',1,datetime('now'))", (identity,))
        chapter = 1000
        for novel, count in ((1000,1817),(1001,176)):
            db.execute("INSERT INTO novels(id,title,source_url,active) VALUES(?,?,?,0)",
                       (novel,f'Large {novel}',f'https://example.invalid/novel/{novel}'))
            db.execute('INSERT INTO volumes(id,novel_id,number) VALUES(?,?,1)', (novel,novel))
            for index in range(count):
                source = f'<p>Chapter {chapter} '+'word '*1000+'</p>'
                db.execute("INSERT INTO chapters(id,novel_id,volume_id,number,title,url,order_index,content_html,content_size) VALUES(?,?,?,?,?,?,?,?,?)",
                           (chapter,novel,novel,index+1,f'Chapter {chapter}',f'https://example.invalid/chapter/{chapter}',index,source,len(source)))
                chapter += 1
        db.execute("INSERT INTO page_reads(user_id,kind,started_at,seconds,cap_seconds,visit_id,pace_words,pace_seconds,pace_samples) VALUES(1,'epub',datetime('now'),300,300,'reload-pace',1000,300,3)")


class ReloadPerformance(audit.AuditChecks):
    probe = PROBE

    @classmethod
    def seed_readers(cls):
        seed_text_shelf(cls.fixture.data, cls.fixture.fixture)
        # Include the whole text shelf in real application startup; no novel
        # sync is active and no scheduled scan runs during the measurements.
        cls.fixture.stop_server()
        cls.fixture.start_server()

    def cpu_seconds(self):
        fields = Path(f'/proc/{self.fixture.server.pid}/stat').read_text().rsplit(')',1)[1].split()
        return (int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')

    def snapshots(self):
        with closing(sqlite3.connect(self.fixture.data / 'cache' / 'reading-estimates.sqlite3')) as db:
            return db.execute('SELECT scope,signature,payload,rowid FROM snapshots ORDER BY scope').fetchall()

    def settled(self, previous=0):
        self.wait(f'''window.reloadProbe && reloadProbe.usable!==null && reloadProbe.pending===0 &&
            reloadProbe.done>{previous} && performance.now()-Math.max(reloadProbe.lastEta,reloadProbe.started)>650''')

    def test_warm_reload_and_home_navigation_keep_work_and_latency_bounded(self):
        # Cold preparation is separate from the warm navigation budget. This
        # authenticates through real HTTP and warms the entire shelf, not just
        # the first visible scope. The browser still performs its own ETA POST.
        with requests.Session() as session:
            session.cookies.update({cookie['name']:cookie['value'] for cookie in self.driver.get_cookies()})
            targets = [{'kind':'series','id':1000+i} for i in range(26)] + [
                {'kind':'novel','id':1000}, {'kind':'novel','id':1001}]
            started = time.perf_counter()
            response = session.post(self.base+'/library/reading-estimates', json={'targets':targets}, timeout=120)
            response.raise_for_status()
            self.assertTrue(all(value['remaining'] is not None for value in response.json()['estimates']))
            print(json.dumps({'cold_preparation_ms':round(1000*(time.perf_counter()-started)),
                              'browser':self.browser,'viewport':self.viewport_width}),flush=True)
        self.driver.get(self.base+'/')
        self.settled()
        self.wait("[...document.querySelectorAll('[data-text-eta-kind]')].every(n=>n.textContent.startsWith('~'))")
        expected = response.json()
        cookies = {cookie['name']:cookie['value'] for cookie in self.driver.get_cookies()}
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown, wait=True)
        results=[]
        for action in ('reload','hard_reload','home_from_series','home_during_eta'):
            for repeat in range(3):
                if action in ('home_from_series','home_during_eta'):
                    self.boosted('/series/1000')
                    self.wait('reloadProbe.pending===0')
                before = self.snapshots()
                cpu = self.cpu_seconds()
                eta_before = self.js('return reloadProbe.done') if action in ('home_from_series','home_during_eta') else 0
                log_before = (self.fixture.fixture / 'server.log').read_text()
                overlap = None
                if action=='home_during_eta':
                    overlap = pool.submit(requests.post, self.base+'/library/reading-estimates',
                                          json={'targets':targets}, cookies=cookies, timeout=120)
                    time.sleep(.01)  # Dispatch ETA before activating the real Home link.
                if action in ('home_from_series','home_during_eta'):
                    previous = self.js('return reloadProbe.done')
                    self.driver.find_element('css selector','a[aria-label="Library"]').click()
                    self.settled(previous)
                else:
                    if action=='hard_reload':
                        if self.browser=='chrome':
                            self.driver.execute_cdp_cmd('Network.enable',{})
                            self.driver.execute_cdp_cmd('Network.setCacheDisabled',{'cacheDisabled':True})
                        else:
                            self.driver.network.set_cache_behavior('bypass')
                    try:
                        if action=='hard_reload' and self.browser=='chrome':
                            marker=self.js('return reloadProbe.marker')
                            self.driver.execute_cdp_cmd('Page.reload',{'ignoreCache':True})
                            self.wait('window.reloadProbe && reloadProbe.marker!=='+json.dumps(marker))
                        else:
                            self.driver.refresh()
                        self.settled()
                    finally:
                        if action=='hard_reload':
                            if self.browser=='chrome':
                                self.driver.execute_cdp_cmd('Network.setCacheDisabled',{'cacheDisabled':False})
                            else:
                                self.driver.network.set_cache_behavior('default')
                if overlap is not None:
                    completed = overlap.result(timeout=120)
                    completed.raise_for_status()
                    self.assertEqual(completed.json(),expected)
                cpu_ms = 1000*(self.cpu_seconds()-cpu)
                self.assertTrue(before==self.snapshots(), 'Unchanged reloads must not rewrite estimate snapshots')
                self.assertEqual(self.js('return reloadProbe.errors'),[])
                self.assertEqual(self.js('return innerWidth'),self.viewport_width, 'Measured viewport must remain fixed')
                if action=='hard_reload':
                    self.assertGreater((self.fixture.fixture / 'server.log').read_text().count('GET /static/'),
                                       log_before.count('GET /static/'), 'Cache bypass must really refetch static resources')
                # Verify that the controls whose initialization marks usability
                # actually respond, independently of the timing instrument.
                search=self.driver.find_element('id','series-search')
                self.driver.execute_script('arguments[0].focus()',search)
                self.assertEqual(self.js('return document.activeElement.id'),'series-search')
                results.append({'action':action,'repeat':repeat,'usable_ms':self.js('return reloadProbe.usable'),
                                'server_cpu_ms':round(cpu_ms,2),
                                'eta_requests':self.js('return reloadProbe.done')-eta_before,
                                'static_requests':(self.fixture.fixture / 'server.log').read_text().count('GET /static/')-log_before.count('GET /static/'),
                                'browser':self.browser,'viewport':self.viewport_width})
        print(json.dumps({'reload_performance':results}),flush=True)
        for action in ('reload','hard_reload','home_from_series','home_during_eta'):
            values=[row for row in results if row['action']==action]
            self.assertLessEqual(statistics.median(row['usable_ms'] for row in values),500,
                                 'Warm page usability median must meet the 500ms budget')


def suite(args, *, mobile=True):
    return unittest.TestSuite(type(browser.title()+'ReloadPerformance'+str(width),(ReloadPerformance,),
        {'browser':browser,'viewport_width':width})('test_warm_reload_and_home_navigation_keep_work_and_latency_bounded')
        for browser in selected_browsers(args) for width in ((1280,320) if mobile else (1280,)))


def run(args):
    audit.ARGS=args
    result=unittest.TextTestRunner(verbosity=2).run(suite(args))
    unittest_failures(result)
    if not baseline_passed(result,len(selected_browsers(args))*2):
        raise AssertionError('Both viewport scenarios must pass the reload performance checks')
    path=ROOT/'app/stats/estimates.py'
    original=path.read_text()
    before='        if self._snapshot_rows is None:'
    if original.count(before)!=1:
        raise ValueError('Reload performance control must match once')
    try:
        path.write_text(original.replace(before,'        if not self.cached_only:\n            return None\n'+before))
        output=io.StringIO()
        control=unittest.TextTestRunner(stream=output,verbosity=2).run(suite(args,mobile=False))
    finally:
        path.write_text(original)
    if not mutation_rejected(control,selected_browsers(args),'ReloadPerformance',
                             'Unchanged reloads must not rewrite estimate snapshots'):
        print(output.getvalue())
        raise AssertionError('Both browsers must reject redundant authoritative calculations')
    print(' and '.join(selected_browsers(args))+' rejected repeated authoritative source work',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    for name,default in (('chrome-binary','/usr/bin/chromium'),('chromedriver','/usr/bin/chromedriver'),
                         ('firefox-binary','/usr/bin/firefox'),('geckodriver','/usr/bin/geckodriver')):
        parser.add_argument('--'+name,default=default)
    run(parser.parse_args())
