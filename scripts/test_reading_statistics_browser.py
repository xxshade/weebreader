"""Real text traversal, fragment accounting and durable delivery in both engines."""
import argparse

from browser_checks import add_browser_argument, selected_browsers, baseline_passed, mutation_rejected
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import sys
import time
import unittest
import uuid

from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import Select

import test_audit_browser as audit
from test_pagination_browser import PaginationChecks, check_srcdoc_commit
from ci_report import unittest_failures


def reset_position(case):
    with closing(sqlite3.connect(case.fixture.data / 'library.db')) as db, db:
        db.execute('DELETE FROM reading_progress WHERE user_id=1 AND item_id=100')
        db.execute('DELETE FROM novel_reading_progress WHERE user_id=1 AND novel_id=100')


def check_recorder_upgrade(case):
    # Existing tabs retain the head across boosted navigation. Reproduce its
    # previous recorder interface and require an upgrade in the same document.
    case.driver.get(case.base+'/')
    case.wait("typeof window.createReadingTimeTracker==='function'")
    case.js("""window.readingDocumentMarker='same-document';const original=window.createReadingTimeTracker;
        window.createReadingTimeTracker=(...args)=>{const value=original(...args);return {at:value.at,stop:value.stop}};""")
    case.boosted('/reader/100')
    try:
        WebDriverWait(case.driver,20).until(lambda _: case.js('return window.createReadingTimeTracker.protocolVersion===2'))
    except TimeoutException:
        pass
    case.assertEqual(case.js('return window.createReadingTimeTracker.protocolVersion'),2,
                     'An existing tab must upgrade its reading recorder without navigation')
    case.assertEqual(case.js('return window.readingDocumentMarker'),'same-document')
    case.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")


def words_total(case, condition):
    return case.row('SELECT COALESCE(SUM(words),0) AS n FROM page_reads WHERE user_id=1 AND '+condition)['n']


def check_paginated_measurements(case):
    for path, condition in (
        ('/reader/100', 'item_id=100 AND section_index=0'),
        ('/novels/100/read/1/1', 'chapter_id=100'),
    ):
        reset_position(case)
        before = words_total(case, condition)
        case.open_reader(path)
        for _ in range(40):
            page = case.page()['text']
            # Real elapsed time and real page turns; no synthetic clock or
            # scripts inside the production content frame.
            time.sleep(1.1)
            case.next_page()
            if page.endswith('The final paragraph.'):
                break
        else:
            case.fail('Measured reading must reach the end of the fixture')
        try:
            WebDriverWait(case.driver,20).until(lambda _: words_total(case, condition) >= before+456)
        except TimeoutException:
            pass  # The numeric assertion below is the mutation's signal.
        case.assertEqual(words_total(case, condition)-before,456,
                         'Measured words must match the traversed text across paragraph fragments')
        fragments = case.row('''SELECT COUNT(DISTINCT json_extract(identity,'$.character_offset')) AS n
            FROM reading_activity WHERE user_id=1 AND json_extract(identity,'$.page_index')=1
            AND ''' + ('item_id=100' if path.startswith('/reader') else 'chapter_id=100'))
        case.assertGreater(fragments['n'],1,'Paragraph fragments must retain distinct timing positions')


def check_checkpoint_recovery(case):
    # Persist a failed fetch and a failed beacon, then reopen the real reader.
    reset_position(case)
    case.open_reader('/reader/100')
    case.wait("!Object.keys(localStorage).filter(k=>k.startsWith('reading-activity:')).some(k=>{const v=JSON.parse(localStorage[k]);return v.closed && v.completed && !v.rejected})")
    before = words_total(case, 'item_id=100 AND section_index=0')
    case.js("""const originalFetch=window.fetch.bind(window);window.fetch=(url,...args)=>
        String(url)==='/reading-time'?Promise.reject(new Error('deliberate connection loss')):originalFetch(url,...args);
        const originalBeacon=navigator.sendBeacon.bind(navigator);navigator.sendBeacon=(url,...args)=>
        String(url)==='/reading-time'?false:originalBeacon(url,...args);""")
    time.sleep(5.2)
    case.assertTrue(case.js("return Object.keys(localStorage).filter(k=>k.startsWith('reading-activity:')).some(k=>{const v=JSON.parse(localStorage[k]);return !v.closed && v.intervals.length>0})"),
                    'An unfinished reading visit must be checkpointed before leaving the page')
    case.next_page()
    case.wait("Object.keys(localStorage).filter(k=>k.startsWith('reading-activity:')).some(k=>{const v=JSON.parse(localStorage[k]);return v.closed && v.completed && !v.rejected})")
    case.assertEqual(words_total(case, 'item_id=100 AND section_index=0'),before)
    case.driver.refresh()
    case.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
    WebDriverWait(case.driver,20).until(lambda _: words_total(case, 'item_id=100 AND section_index=0') > before)
    recovered = words_total(case, 'item_id=100 AND section_index=0')
    case.driver.refresh()
    case.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
    time.sleep(1.1)
    case.assertEqual(words_total(case, 'item_id=100 AND section_index=0'),recovered,
                     'Reopening must not count acknowledged reading activity twice')


def check_continuous_measurements(case):
    # Use a real scroll gesture; changing scrollTop alone is not reading.
    reset_position(case)
    case.open_reader('/reader/100')
    activate = case.tap if hasattr(case,'touch') else lambda selector: case.driver.find_element('css selector',selector).click()
    activate('#btn-settings')
    Select(case.driver.find_element('id','flow-select')).select_by_value('scrolled-doc')
    case.wait("document.querySelector('#custom-scroll iframe')?.contentDocument.body.textContent.includes('word0449')")
    activate('#btn-settings')
    time.sleep(1.2)
    before = words_total(case, 'item_id=100 AND section_index=0')
    before_scroll = case.js("return document.getElementById('custom-scroll').scrollTop")
    if hasattr(case,'touch'):
        size = case.js('return {w:innerWidth,h:innerHeight}')
        # Native Android input exercises GeckoView's actual pan/zoom path.
        case.touch(size['w']/2,size['h']*.75,(size['w']/2,size['h']*.3),duration=700)
    else:
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
        ActionChains(case.driver).scroll_from_origin(ScrollOrigin.from_element(case.driver.find_element('id','custom-scroll')),0,120).perform()
    case.wait("document.getElementById('custom-scroll').scrollTop>" + str(before_scroll))
    try:
        WebDriverWait(case.driver,20).until(lambda _: words_total(case, 'item_id=100 AND section_index=0') > before)
    except TimeoutException:
        pass
    measured = words_total(case, 'item_id=100 AND section_index=0')
    details = ''
    if measured <= before:
        # Preserve the actual displacement and delivery state. A successful
        # native swipe is not proof that it produced an eligible text sample.
        details = repr(case.js("""const e=document.getElementById('custom-scroll');return {
            scroll:{top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight},
            pending:Object.keys(localStorage).filter(k=>k.startsWith('reading-activity:')).map(k=>{
                const v=JSON.parse(localStorage[k]);return {closed:v.closed,completed:v.completed,
                    rejected:v.rejected,intervals:v.intervals,traversedEnd:v.traversed_end};})};"""))
    case.assertGreater(measured,before,
                       'Continuous reading must record words after a real scroll gesture; '
                       + f'initialScroll={before_scroll}; {details}')


def check_reading_priority(case):
    reset_position(case)
    case.open_reader('/reader/100')
    # Confirm the current reader has received its claim response and persisted
    # activity before creating the competing session. A database row alone can
    # appear while that response is still in flight.
    claim = WebDriverWait(case.driver,20).until(lambda _: case.row(
        'SELECT session_id FROM reading_claims WHERE user_id=1 AND ended IS NULL'))
    session = claim['session_id']
    case.wait("Object.keys(localStorage).filter(k=>k.startsWith('reading-activity:')).some(k=>JSON.parse(localStorage[k]).session_id===" + repr(session) + ")")
    other_session, other_claim = uuid.uuid4().hex, uuid.uuid4().hex
    ok = case.driver.execute_async_script("""const done=arguments[arguments.length-1];
        fetch('/reading-session',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:arguments[0],claim_id:arguments[1]})}).then(r=>done(r.ok));""",other_session,other_claim)
    case.assertTrue(ok)
    # Await actual requests triggered by reconnecting, then inspect ownership.
    case.driver.execute_async_script("""const done=arguments[0],original=window.fetch,requests=[];
        window.fetch=(...args)=>{const promise=original(...args);requests.push(promise);return promise};
        window.dispatchEvent(new Event('online'));
        Promise.all(requests).then(()=>{window.fetch=original;done(true)});""")
    case.assertEqual(case.row('SELECT session_id FROM reading_claims WHERE user_id=1 AND ended IS NULL')['session_id'],other_session,
                     'Reconnecting must not take reading priority without user activity')
    case.driver.get(case.base+'/stats')
    case.assertIn('Text words traversed',case.driver.find_element('tag name','body').text)
    case.assertIn('Comic pages',case.driver.find_element('tag name','body').text)
    case.assertEqual(len(case.driver.find_elements('css selector','[data-reading-pace]')),1)
    case.assertIn('Collecting data',case.driver.find_element('css selector','[data-reading-pace="text"]').text)


READING_CHECKS = {
    'test_srcdoc_commit': check_srcdoc_commit,
    'test_recorder_upgrade': check_recorder_upgrade,
    'test_paginated_measurements': check_paginated_measurements,
    'test_checkpoint_recovery': check_checkpoint_recovery,
    'test_continuous_measurements': check_continuous_measurements,
    'test_reading_priority': check_reading_priority,
}


class ReadingStatisticsChecks(PaginationChecks):
    pass


def desktop_check(check):
    def run(self):
        self.driver.set_window_size(460,640)
        check(self)
    return run


for name, check in READING_CHECKS.items():
    setattr(ReadingStatisticsChecks, name, desktop_check(check))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    for name, default in (('chrome-binary','/usr/bin/chromium'),('chromedriver','/usr/bin/chromedriver'),
                          ('firefox-binary','/usr/bin/firefox'),('geckodriver','/usr/bin/geckodriver')):
        parser.add_argument('--'+name,default=default)
    parser.add_argument('--test', choices=READING_CHECKS)
    audit.ARGS=parser.parse_args()
    selected = [audit.ARGS.test] if audit.ARGS.test else READING_CHECKS
    def suite(names=selected):
        return unittest.TestSuite(type(browser.title()+'ReadingStatistics',(ReadingStatisticsChecks,),
            {'browser':browser})(name) for browser in selected_browsers(audit.ARGS) for name in names)
    result=unittest.TextTestRunner(verbosity=2).run(suite())
    unittest_failures(result)
    if not baseline_passed(result, len(selected_browsers(audit.ARGS)) * len(selected)):
        sys.exit(1)
    for filename,before,after,signal,test in (
        ('reader-text.js','doc?.URL === "about:srcdoc" && doc.body','doc?.body',
         'A late srcdoc commit must not erase the rendered chapter','test_srcdoc_commit'),
        ('reading-time.js','current.value.completed = completed;','current.value.completed = false;','Measured words must match','test_paginated_measurements'),
        ('reader-text.js','function reportContinuousTiming(normal) {','function reportContinuousTiming(normal) { if (normal) return;','Continuous reading must record words','test_continuous_measurements'),
        ('reader-text.js','script.src = trackerUrl;','resolve(); return;','An existing tab must upgrade its reading recorder without navigation','test_recorder_upgrade'),
        ('reading-time.js','persist({...current.value, intervals, revision: ++current.revision});',
         'if (current.value.closed) persist({...current.value, intervals, revision: ++current.revision});',
         'An unfinished reading visit must be checkpointed before leaving the page','test_checkpoint_recovery'),
        ('reading-time.js','function onOnline() { if (anchor === null) activate(true); flush(); }',
         'function onOnline() { activate(true); flush(); }',
         'Reconnecting must not take reading priority without user activity','test_reading_priority'),
    ):
        if test not in selected:
            continue
        source=Path(__file__).resolve().parents[1]/'app/static/js'/filename
        original=source.read_text()
        if original.count(before) != 1:
            raise ValueError('Reading measurement mutation must match exactly once.')
        try:
            source.write_text(original.replace(before,after))
            output=io.StringIO()
            control=unittest.TextTestRunner(stream=output,verbosity=2).run(suite([test]))
        finally:
            source.write_text(original)
        if not mutation_rejected(control, selected_browsers(audit.ARGS), 'ReadingStatistics', signal):
            print(output.getvalue())
            raise AssertionError('Every selected browser must reject disabled reading measurement.')
        print(' and '.join(b.title() for b in selected_browsers(audit.ARGS)) + ' rejected mutation in '+filename,flush=True)
