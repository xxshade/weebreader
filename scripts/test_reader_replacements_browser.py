"""Real text-replacement, activity, history and cache regressions in both engines.

Only disposable data and localhost are used. Deliberate defects run sequentially;
use a separate checkout when running this script beside another validation suite.
The fixture instruments cache observation, not replacement or position behavior.
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import unittest
import zipfile

import requests
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    ElementNotInteractableException,
    TimeoutException,
)

import test_audit_browser as audit
from test_pagination_browser import PaginationChecks, CONTENT, WORDS
from test_resync_browser import BrowserChecks
from browser_checks import (
    add_browser_argument,
    selected_browsers,
    baseline_passed,
    mutation_rejected,
)
from ci_report import unittest_failures

ROOT = Path(__file__).resolve().parents[1]


class Fixture(BrowserChecks):
    @classmethod
    def start_server(cls):
        cls.log = open(cls.fixture / "server.log", "a", encoding="utf-8")
        cls.server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.reader_replacement_server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "APP_DATA_DIR": str(cls.data),
                "APP_SCHEDULED_SCAN_CRON": "",
            },
            stdout=cls.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        for _ in range(150):
            if cls.server.poll() is not None:
                raise RuntimeError((cls.fixture / "server.log").read_text())
            try:
                if requests.get(cls.base + "/login", timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.1)
        raise RuntimeError("Reader replacement server did not start")


class ReplacementChecks(PaginationChecks):
    @classmethod
    def setUpClass(cls):
        cls.fixture = type("ReplacementFixture", (Fixture, unittest.TestCase), {})
        cls.addClassCleanup(cls.fixture.doClassCleanups)
        cls.fixture.setUpClass()
        cls.base = cls.fixture.base
        cls.seed_readers()
        with sqlite3.connect(cls.fixture.data / "library.db") as db:
            for item in (100, 104):
                path = db.execute(
                    "SELECT file_path FROM items WHERE id=?", (item,)
                ).fetchone()[0]
                with zipfile.ZipFile(path) as book:
                    files = {name: book.read(name) for name in book.namelist()}
                if item == 100:
                    files["OEBPS/content.opf"] = (
                        '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                        + "".join(
                            f'<item id="c{i}" href="ch{i}.xhtml"/>' for i in range(40)
                        )
                        + "</manifest><spine>"
                        + "".join(f'<itemref idref="c{i}"/>' for i in range(40))
                        + "</spine></package>"
                    ).encode()
                    for index in range(40):
                        files[f"OEBPS/ch{index}.xhtml"] = (
                            "<html><body>" + CONTENT + "</body></html>"
                        ).encode()
                else:
                    files["OEBPS/ch0.xhtml"] = (
                        "<html><body><table><tr><td>inside marker</td></tr>"
                        'outside marker</table><p id="long">'
                        + WORDS
                        + "</p></body></html>"
                    ).encode()
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
                    for name, value in files.items():
                        book.writestr(name, value)

    def setUp(self):
        super().setUp()
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            db.execute("DELETE FROM text_replacements")
            db.execute("DELETE FROM reading_progress WHERE item_id IN (100,104)")
            db.execute("DELETE FROM novel_reading_progress WHERE novel_id=100")
        self.driver.get(self.base + "/")

    def first_marker(self):
        page = self.page()
        match = re.search(r"word\d{4}", page["long"] or page["text"])
        self.assertIsNotNone(match, "A visible fixture passage is required")
        return match[0]

    @contextmanager
    def novel_order_fixture(self):
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            before = db.execute(
                "SELECT number,order_index,title FROM chapters WHERE id=100"
            ).fetchone()
            db.execute("DELETE FROM novel_chapter_aliases WHERE novel_id=100")
            db.execute("DELETE FROM novel_read_chapters WHERE chapter_id=100")
            db.execute("UPDATE chapters SET number=2,order_index=1 WHERE id=100")
            text = '<p>Correct next chapter</p><p id="long">' + WORDS + "</p>"
            db.execute(
                "INSERT INTO chapters(id,novel_id,volume_id,number,title,url,order_index,content_html,content_size) VALUES(101,100,100,1,'Other','https://example.invalid/other',0,?,?)",
                (text, len(text)),
            )
        try:
            yield
        finally:
            with sqlite3.connect(self.fixture.data / "library.db") as db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("DELETE FROM chapters WHERE id=101")
                db.execute("DELETE FROM novel_chapter_aliases WHERE novel_id=100")
                db.execute(
                    "UPDATE chapters SET number=?,order_index=?,title=? WHERE id=100",
                    before,
                )

    def repair_novel_order(self):
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            db.executemany(
                "INSERT OR IGNORE INTO novel_chapter_aliases VALUES(100,1,?,?)",
                [(2, 100), (1, 101)],
            )
            db.execute(
                "UPDATE chapters SET number=1,order_index=0,title='Correct first chapter' WHERE id=100"
            )
            db.execute("UPDATE chapters SET number=63,order_index=1 WHERE id=101")

    def test_novel_numeric_bookmarks_and_new_id_links_keep_their_chapter(self):
        with self.novel_order_fixture():
            self.open_reader("/novels/100/read/1/2")
            self.assertEqual(
                self.js(
                    "return document.getElementById('reader-root').dataset.startChapterId"
                ),
                "100",
            )
            self.driver.get(self.base + "/novels/100")
            self.wait(
                "document.getElementById('library-events')?.libraryEventsBound===true"
            )
            document = self.js("return audit.document")
            self.repair_novel_order()
            status = self.driver.execute_async_script(
                "const done=arguments[arguments.length-1];fetch('/novels/100/publication-status',{method:'POST',body:new URLSearchParams({publication_status:'ongoing'})}).then(r=>done(r.status)).catch(()=>done(0))"
            )
            self.assertEqual(status, 200)
            try:
                self.wait(
                    "document.querySelector('#chapter-row-100 a').textContent.includes('Ch. 1 —')"
                )
            except TimeoutException:
                self.fail("A metadata event must refresh the open chapter list")
            self.assertEqual(
                self.js("return audit.document"),
                document,
                "A metadata update must refresh the open chapter list without a reload",
            )
            self.open_reader("/novels/100/read/1/2")
            self.assertEqual(
                self.js(
                    "return document.getElementById('reader-root').dataset.startChapterId"
                ),
                "100",
                "Numeric bookmarks must retain their pre-repair chapter",
            )
            self.driver.get(self.base + "/novels/100")
            link = self.driver.find_element(
                "css selector", '#chapter-row-100 a[href="/novels/100/read/100"]'
            )
            link.click()
            self.wait(
                "document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
            )
            self.assertEqual(
                self.js("return location.pathname"),
                "/novels/100/read/100",
                "Generated chapter links must use stable IDs",
            )

    def test_novel_order_change_refreshes_at_a_chapter_boundary(self):
        with self.novel_order_fixture():
            self.open_reader("/novels/100/read/100")
            for _ in range(40):
                if self.js(
                    "return document.getElementById('reader-root').readerFixture.navigation().atEnd"
                ):
                    break
                self.next_page()
            else:
                self.fail("Fixture must reach the last page of its chapter")
            text = self.page()["text"].strip()
            match = re.search(r"word\d{4}", text)
            before = match[0] if match else text
            document = self.js("return audit.document")
            self.repair_novel_order()
            self.wait(
                "!document.getElementById('reader-root').readerFixture.navigation().busy"
            )
            self.driver.find_element("id", "btn-next").click()
            try:
                self.wait(
                    "audit.document!=="
                    + repr(document)
                    + " && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
                )
            except TimeoutException:
                self.fail("A chapter boundary must refresh a changed order")
            self.assertTrue(
                self.page()["text"].strip().startswith(before),
                "Order refresh must retain the current passage",
            )
            self.assertEqual(
                self.js(
                    "return document.getElementById('reader-root').dataset.startSpineIndex"
                ),
                "0",
                "A repaired chapter must use the new order",
            )
            self.show_controls()
            self.next_page()
            self.assertIn(
                "Correct next chapter",
                self.page()["text"],
                "Next must follow the repaired manifest",
            )

    def test_novel_history_keeps_passage_when_order_moves_backwards(self):
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.actions.wheel_input import ScrollOrigin

        with self.novel_order_fixture():
            for flow in ("paginated", "scrolled-doc"):
                with self.subTest(flow=flow):
                    with sqlite3.connect(self.fixture.data / "library.db") as db:
                        db.execute(
                            "DELETE FROM novel_reading_progress WHERE novel_id=100"
                        )
                        db.execute(
                            "UPDATE chapters SET order_index=1,number=2 WHERE id=100"
                        )
                        db.execute(
                            "UPDATE chapters SET order_index=0,number=1 WHERE id=101"
                        )
                    self.driver.get(self.base + "/")
                    self.js(
                        "localStorage.setItem('text-reader-prefs',JSON.stringify({flow:"
                        + repr(flow)
                        + ",font:'original',fontSize:100,lineHeight:1.6,width:720,bottomGap:0}))"
                    )
                    self.driver.get(self.base + "/novels/100/read/100")
                    selector = (
                        "#paginated-viewer iframe"
                        if flow == "paginated"
                        else "#custom-scroll iframe"
                    )
                    self.wait(
                        "document.querySelector("
                        + repr(selector)
                        + ")?.style.visibility==='visible'"
                    )
                    if flow == "paginated":
                        self.show_controls()
                        self.next_page()
                    else:
                        ActionChains(self.driver).scroll_from_origin(
                            ScrollOrigin.from_element(
                                self.driver.find_element("id", "custom-scroll")
                            ),
                            0,
                            400,
                        ).perform()
                    WebDriverWait(self.driver, 10).until(lambda _: self.saved(True))
                    saved = self.row(
                        "SELECT paragraph_index,character_offset FROM novel_reading_progress WHERE novel_id=100"
                    )
                    if flow == "paginated":
                        # Emulate a history snapshot from before order tokens:
                        # its ordinary replacement comparison must also refresh.
                        self.js(
                            "const r=document.getElementById('reader-root');delete r.dataset.readingManifestVersion;r.dataset.replacements=JSON.stringify(JSON.parse(r.dataset.replacements).filter(x=>x.find))"
                        )
                    self.driver.find_element(
                        "css selector", '#reader-root a[href="/novels/100"]'
                    ).click()
                    self.wait(
                        "location.pathname==='/novels/100' && !document.getElementById('reader-root')"
                    )
                    self.repair_novel_order()
                    self.driver.back()
                    try:
                        self.wait(
                            "location.pathname==='/novels/100/read/100' && document.querySelector("
                            + repr(selector)
                            + ")?.style.visibility==='visible' && document.getElementById('reader-root').dataset.startSpineIndex==='0'"
                        )
                    except TimeoutException:
                        self.fail("History must refresh an earlier chapter order")
                    self.assertEqual(
                        self.js(
                            "return document.getElementById('reader-root').dataset.startCfi"
                        ),
                        f"elidx:{saved['paragraph_index']}:{saved['character_offset']}",
                        "History must preserve the saved passage after an earlier order index",
                    )

    def test_scroll_order_change_keeps_last_confirmed_chapter(self):
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.actions.wheel_input import ScrollOrigin

        with self.novel_order_fixture():
            self.js(
                "localStorage.setItem('text-reader-prefs',JSON.stringify({flow:'scrolled-doc',font:'original',fontSize:100,lineHeight:1.6,width:720,bottomGap:0}))"
            )
            self.driver.get(self.base + "/novels/100/read/101")
            self.wait(
                "document.querySelectorAll('#custom-scroll iframe').length===2 && [...document.querySelectorAll('#custom-scroll iframe')].every(f=>f.style.visibility==='visible')"
            )
            ActionChains(self.driver).scroll_from_origin(
                ScrollOrigin.from_element(
                    self.driver.find_element("id", "custom-scroll")
                ),
                0,
                300,
            ).perform()
            WebDriverWait(self.driver, 10).until(lambda _: self.saved(True))
            self.assertEqual(
                self.row(
                    "SELECT chapter_id FROM novel_reading_progress WHERE novel_id=100"
                )["chapter_id"],
                101,
            )
            document = self.js("return audit.document")
            self.repair_novel_order()
            ActionChains(self.driver).scroll_from_origin(
                ScrollOrigin.from_element(
                    self.driver.find_element("id", "custom-scroll")
                ),
                0,
                8000,
            ).perform()
            try:
                self.wait(
                    "audit.document!=="
                    + repr(document)
                    + " && document.querySelector('#custom-scroll iframe')?.style.visibility==='visible'"
                )
            except TimeoutException:
                self.fail("Scroll must refresh before retaining an old neighbour")
            self.assertEqual(
                self.js(
                    "return document.getElementById('reader-root').dataset.startChapterId"
                ),
                "101",
                "Scroll order refresh must keep the last confirmed chapter",
            )

    def test_order_refresh_waits_for_older_progress_requests(self):
        with self.novel_order_fixture():
            self.open_reader("/novels/100/read/100")
            self.js(
                "const send=fetch;window.fetch=(url,options)=>{if(String(url)==='/novels/100/progress'&&options?.method==='POST'){if(!audit.heldProgress){audit.heldProgress=true;return new Promise(resolve=>{audit.releaseProgress=()=>{audit.progressSent=(audit.progressSent||0)+1;resolve(send(url,options));};});}audit.progressSent=(audit.progressSent||0)+1;}return send(url,options);}"
            )
            self.next_page()
            self.wait("audit.heldProgress===true")
            for _ in range(40):
                if self.js(
                    "return document.getElementById('reader-root').readerFixture.navigation().atEnd"
                ):
                    break
                self.next_page()
            else:
                self.fail("Fixture must reach its chapter boundary")
            self.wait(
                "!document.getElementById('reader-root').readerFixture.navigation().busy"
            )
            before = self.page()["text"].strip()
            document = self.js("return audit.document")
            self.repair_novel_order()
            sent = self.js("return audit.progressSent || 0")
            self.driver.find_element("id", "btn-next").click()
            self.wait(
                "audit.document!=="
                + repr(document)
                + " || document.getElementById('reader-root')?.readerFixture?.progress().refreshing"
            )
            self.assertEqual(
                self.js("return audit.progressSent || 0"),
                sent,
                "Order refresh must await older progress before saving its checkpoint",
            )
            self.assertEqual(
                self.js("return audit.document"),
                document,
                "Order refresh must await older progress before saving its checkpoint",
            )
            self.js("audit.releaseProgress()")
            try:
                self.wait(
                    "audit.document!=="
                    + repr(document)
                    + " && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
                )
            except TimeoutException:
                self.fail("Order refresh must finish after the older request settles")
            expected = re.search(r"word\d{4}", before)
            self.assertTrue(
                self.page()["text"]
                .strip()
                .startswith(expected[0] if expected else before),
                "An older request must not overwrite the refresh checkpoint",
            )

    def saved(self, novel=False):
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            table = (
                "novel_reading_progress WHERE novel_id=100"
                if novel
                else "reading_progress WHERE item_id=100"
            )
            row = db.execute("SELECT source_anchor FROM " + table).fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def settings_from_reader(self, novel=False, item=100):
        back = "/novels/100" if novel else f"/series/{item}"
        self.driver.find_element(
            "css selector", f'#reader-root a[href="{back}"]'
        ).click()
        self.wait(
            f'location.pathname==="{back}" && window.userMenuBound===true && !document.getElementById("reader-root")'
        )
        self.frames()

        def open_settings(_):
            # URL changes can precede the body morph; a live resync can also
            # replace an opened dropdown. Reacquire actual controls until the
            # requested page exists, rather than keeping a stale menu handle.
            if self.js("return location.pathname==='/settings'"):
                return bool(
                    self.driver.find_elements(
                        "css selector", "#add-replacement select[name=target]"
                    )
                )
            dropdown = self.driver.find_element("id", "user-menu-dropdown")
            if "hidden" in dropdown.get_attribute("class").split():
                self.driver.find_element("id", "user-menu-btn").click()
            else:
                dropdown.find_element("css selector", 'a[href="/settings"]').click()
            return False

        WebDriverWait(
            self.driver,
            20,
            ignored_exceptions=(
                NoSuchElementException,
                StaleElementReferenceException,
                ElementNotInteractableException,
            ),
        ).until(open_settings)

    def add_rule(self, replacement, novel=False, item=100):
        target = "novel:100" if novel else f"series:{item}"

        def choose_target(_):
            # Enhancement and the first live resync may replace this control.
            native = self.driver.find_element(
                "css selector", "#add-replacement select[name=target]"
            )
            if native.is_displayed():
                Select(native).select_by_value(target)
            else:
                combo = self.driver.find_element(
                    "css selector", "#add-replacement [role=combobox]"
                )
                combo.clear()
                combo.send_keys("Illustrated novel" if novel else f"Book {item}")
                self.driver.find_element(
                    "css selector", f'#add-replacement [data-value="{target}"]'
                ).click()
            return (
                self.js(
                    "return document.querySelector('#add-replacement select[name=target]').value"
                )
                == target
            )

        WebDriverWait(
            self.driver,
            10,
            ignored_exceptions=(
                NoSuchElementException,
                StaleElementReferenceException,
                ElementNotInteractableException,
            ),
        ).until(choose_target)
        self.driver.find_element(
            "css selector", "#add-replacement [name=find_text]"
        ).send_keys("word0000")
        self.driver.find_element(
            "css selector", "#add-replacement [name=replace_text]"
        ).send_keys(replacement)
        self.driver.find_element("css selector", "#add-replacement button").click()
        self.wait(
            "document.querySelectorAll('#replacement-rules [data-rule-id]').length===1"
        )
        time.sleep(2)  # ordinary user sequence, not a millisecond navigation race

    def resume_from_home(self, path):
        if path == "/novels/100/read/1/1":
            path = "/novels/100/read/100"
        self.driver.find_element("css selector", 'a[aria-label="Library"]').click()
        self.wait(
            f'location.pathname==="/" && !!document.querySelector(\'#reading-grid a[href="{path}"]\')'
        )
        link = self.driver.find_element(
            "css selector", f'#reading-grid a[href="{path}"]'
        )
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'})", link
        )
        link.click()
        self.wait(
            "document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
        )

    def test_replacement_preserves_epub_and_novel_passage(self):
        for novel in (False, True):
            with self.subTest(novel=novel):
                with sqlite3.connect(self.fixture.data / "library.db") as db:
                    db.execute("DELETE FROM text_replacements")
                path = "/novels/100/read/1/1" if novel else "/reader/100"
                self.open_reader(path)
                self.next_page()
                before = self.first_marker()
                WebDriverWait(self.driver, 10).until(lambda _: self.saved(novel))
                self.settings_from_reader(novel)
                self.add_rule("new</p><p>", novel)
                self.resume_from_home(path)
                self.assertEqual(
                    self.first_marker(),
                    before,
                    "Preserve the original passage after a replacement",
                )

    def test_history_refreshes_rules_without_a_newer_position(self):
        self.open_reader("/reader/100")
        self.next_page()
        WebDriverWait(self.driver, 10).until(lambda _: self.saved())
        before = self.first_marker()
        self.driver.refresh()  # render the already saved position into the history baseline
        self.wait(
            "document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
        )
        self.settings_from_reader()
        self.add_rule("Renamed character")
        self.driver.back()
        self.wait("location.pathname==='/series/100'")
        self.driver.back()
        self.wait(
            "location.pathname==='/reader/100' && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
        )
        self.wait(
            "document.querySelector('#reader-root')?.dataset.replacements && JSON.parse(document.querySelector('#reader-root').dataset.replacements).filter(r=>r.find).length===1 && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
        )
        self.assertEqual(self.first_marker(), before)
        self.assertIn(
            "Renamed character",
            self.js(
                "return document.querySelector('#reader-root').dataset.replacements"
            ),
            "History must refresh the currently displayed rules",
        )

    def test_pending_activity_survives_replacement_edit(self):
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            db.execute(
                "INSERT INTO reading_progress(user_id,item_id,status,current_page,current_location,progress_percent,replacement_version) VALUES(1,100,'reading',0,'elidx:0:0',10,2)"
            )
        self.open_reader("/reader/100")
        self.js("""window.pendingActivityProbe={blocked:true,responses:[]};
            const original=window.fetch,beacon=navigator.sendBeacon.bind(navigator);
            window.fetch=async(...args)=>{
                if(String(args[0])==='/reading-time'&&pendingActivityProbe.blocked)return new Response('',{status:503});
                const response=await original(...args);
                if(String(args[0])==='/reading-time')pendingActivityProbe.responses.push(await response.clone().json());
                return response;
            };
            navigator.sendBeacon=(url,body)=>String(url)==='/reading-time'&&pendingActivityProbe.blocked?false:beacon(url,body);
        """)
        time.sleep(1.2)
        self.settings_from_reader(item=100)
        identities = self.js(
            "return Object.keys(localStorage).filter(k=>k.startsWith('reading-activity:v1:')).map(k=>JSON.parse(localStorage[k])).filter(v=>!v.rejected&&v.item_id===100).map(v=>v.id)"
        )
        self.assertTrue(identities, "The first delivery must really remain pending")
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            missing = sum(
                not db.execute(
                    "SELECT 1 FROM reading_activity WHERE id=?", (identity,)
                ).fetchone()
                for identity in identities
            )
        self.assertGreater(
            missing, 0, "The held delivery must not have reached the server"
        )
        self.add_rule("replacement0000", item=100)
        self.js("pendingActivityProbe.blocked=false")
        self.resume_from_home("/reader/100")
        self.wait("pendingActivityProbe.responses.length>0")
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            recorded = sum(
                bool(
                    db.execute(
                        "SELECT 1 FROM reading_activity WHERE id=?", (identity,)
                    ).fetchone()
                )
                for identity in identities
            )
        self.assertEqual(
            recorded,
            len(identities),
            "Pending activity must be recorded after replacement edits",
        )
        self.assertFalse(
            self.js(
                "return pendingActivityProbe.responses.some(r=>r.rejected.length>0)||!!document.getElementById('reading-sync-status')"
            ),
            "A valid old passage must not show the rejection warning",
        )
        self.assertTrue(
            self.js(
                "return document.querySelector('#paginated-viewer iframe').contentDocument.body.textContent.includes('replacement0000')"
            ),
            "Resume must show the current replacement",
        )

    def test_exit_delivery_and_rejected_activity_boundaries(self):
        rows = self.driver.execute_async_script("""const done=arguments[0];(async()=>{
            const original=window.fetch,beacon=navigator.sendBeacon.bind(navigator),rows=[];
            const account=document.querySelector('main[data-reading-account]').dataset.readingAccount;
            const prefix=`reading-activity:v1:${account}:`,rejectedId='fixture-rejected-00000000';
            localStorage.setItem(prefix+rejectedId,JSON.stringify({id:rejectedId,revision:1,rejected:true}));
            let sentRejected=false;
            try {
                for(const [scenario,text] of [['false','one two'],['bytes','漢'.repeat(30000)],['true','one two']]){
                    let requests=0,calls=0,keepalive=null;
                    window.fetch=async(url,options)=>{
                        if(url!=='/fixture-reading-time')return original(url,options);
                        requests++;keepalive=options.keepalive;
                        const activities=JSON.parse(options.body).activities;
                        sentRejected ||= activities.some(a=>a.id===rejectedId);
                        return new Response(JSON.stringify({accepted:activities.map(a=>({id:a.id,revision:a.revision})),rejected:[]}),{status:200});
                    };
                    navigator.sendBeacon=(url,body)=>{
                        if(url!=='/fixture-reading-time')return beacon(url,body);
                        calls++;return scenario==='true';
                    };
                    const tracker=createReadingTimeTracker({endpoint:'/fixture-reading-time'});
                    await tracker.activate();
                    tracker.at({item_id:100,page_index:0,character_offset:0,passage:[{text,start:0,end:text.length}]});
                    await new Promise(r=>setTimeout(r,100));tracker.stop();
                    if(scenario!=='true'){
                        const until=performance.now()+1500;
                        while(!requests&&performance.now()<until)await new Promise(r=>setTimeout(r,20));
                    }
                    await new Promise(requestAnimationFrame);
                    const retained=Object.keys(localStorage).filter(k=>k.startsWith(prefix)).map(k=>JSON.parse(localStorage[k])).filter(a=>!a.rejected).length;
                    rows.push({scenario,requests,calls,keepalive,retained});
                    await tracker.flush();await new Promise(requestAnimationFrame);
                }
                return {rows,sentRejected,rejectedRetained:JSON.parse(localStorage.getItem(prefix+rejectedId))?.rejected};
            }finally{window.fetch=original;navigator.sendBeacon=beacon;}
        })().then(done,e=>done({error:String(e)}));""")
        self.assertNotIn("error", rows)
        self.assertFalse(
            rows["sentRejected"], "Previously rejected activity must remain excluded"
        )
        self.assertTrue(rows["rejectedRetained"])
        short, large, accepted = rows["rows"]
        self.assertEqual(
            short["requests"], 1, "Refused beacon must use acknowledged delivery"
        )
        self.assertTrue(short["keepalive"])
        self.assertEqual(large["calls"], 0, "Beacon quota must use UTF-8 bytes")
        self.assertEqual(large["requests"], 1)
        self.assertFalse(
            large["keepalive"], "Oversized delivery must not use keepalive"
        )
        self.assertEqual(
            accepted["requests"],
            0,
            "An accepted beacon must not duplicate delivery immediately",
        )
        self.assertGreater(
            accepted["retained"], 0, "Accepted beacon must await acknowledgment"
        )

    def test_old_cached_reader_refreshes_the_activity_protocol(self):
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            db.execute(
                "INSERT INTO reading_progress(user_id,item_id,status,current_page,current_location,progress_percent,replacement_version) VALUES(1,100,'reading',0,'elidx:0:0',10,2)"
            )
        self.open_reader("/reader/100")
        before = self.js("return audit.document")
        self.js("""const root=document.getElementById('reader-root');
            root.dataset.replacements=JSON.stringify(JSON.parse(root.dataset.replacements).filter(r=>!r.activity_protocol));
            delete root.dataset.activityRuleSet;""")
        self.driver.find_element(
            "css selector", '#reader-root a[href="/series/100"]'
        ).click()
        self.wait(
            "location.pathname==='/series/100'&&!document.getElementById('reader-root')"
        )
        self.driver.back()
        try:
            WebDriverWait(self.driver, 10).until(
                lambda _: self.js("return audit.document") != before
            )
        except TimeoutException:
            pass
        self.assertNotEqual(
            self.js("return audit.document"),
            before,
            "Old cached reader must refresh the activity protocol",
        )
        self.wait(
            "document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
        )
        self.assertEqual(
            self.js(
                "return document.getElementById('reader-root').dataset.activityRuleSet.length"
            ),
            64,
        )
        self.assertTrue(
            self.js(
                "return JSON.parse(document.getElementById('reader-root').dataset.replacements).some(r=>r.activity_protocol===1)"
            )
        )

    def test_natural_html5_activity_survives_settings_and_resume(self):
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            db.execute(
                "INSERT INTO reading_progress(user_id,item_id,status,current_page,current_location,progress_percent) VALUES(1,104,'reading',0,'elidx:0:0',10)"
            )
        self.open_reader("/reader/104")
        time.sleep(1.2)
        self.settings_from_reader(item=104)
        self.add_rule("replacement0000", item=104)
        self.resume_from_home("/reader/104")
        time.sleep(1)
        self.assertFalse(
            self.js("return !!document.querySelector('#reading-sync-status')"),
            "Valid HTML5 reading must not produce an activity warning",
        )
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            count = db.execute(
                "SELECT COUNT(*) FROM reading_activity WHERE item_id=104"
            ).fetchone()[0]
        self.assertGreater(count, 0, "Valid HTML5 activity must be recorded")

    def test_an_old_boosted_tab_loads_the_new_source_mapper(self):
        self.driver.get(self.base + "/series/100")
        self.js(
            "delete window.readerSourceAnchor;document.querySelector('script[src*=\"/reader-source-anchor.js\"]').remove()"
        )
        before = self.js("return audit.document")
        self.driver.find_element("css selector", 'a[href="/reader/100"]').click()
        self.wait(
            "document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
        )
        self.assertNotEqual(
            self.js("return audit.document"),
            before,
            "An old boosted tab must acquire the new head dependency",
        )
        self.assertTrue(self.js("return !!window.readerSourceAnchor"))

    def test_queued_activity_drains_after_the_tracker_stops(self):
        result = self.driver.execute_async_script("""const done=arguments[0];(async()=>{
            const original=window.fetch, endpoint='/fixture-reading-time';
            let requests=0,release;
            window.fetch=async (url,options)=>{
                if(url!==endpoint)return original(url,options);
                const activities=JSON.parse(options.body).activities;
                requests++;
                if(requests===1)await new Promise(resolve=>release=resolve);
                return new Response(JSON.stringify({accepted:activities.map(a=>({id:a.id,revision:a.revision})),rejected:[]}),{status:200});
            };
            const tracker=createReadingTimeTracker({endpoint});
            try {
                await tracker.activate();
                tracker.at({item_id:100,page_index:0,character_offset:0,passage:[{text:'word0000',start:0,end:8}]});
                await new Promise(resolve=>setTimeout(resolve,1100));
                tracker.at({item_id:100,page_index:0,character_offset:9,passage:[{text:'word0001',start:0,end:8}]});
                if(!release)throw Error('Expected a held successful activity response');
                tracker.stop();release();
                const until=performance.now()+1500;
                while(requests<2&&performance.now()<until)await new Promise(resolve=>setTimeout(resolve,25));
                return {requests};
            } finally {window.fetch=original;}
        })().then(done,e=>done({error:String(e)}));""")
        self.assertNotIn("error", result)
        self.assertGreaterEqual(
            result["requests"],
            2,
            "Stopped tracker must drain activity queued during an in-flight response",
        )

    def test_structural_mapping(self):
        self.driver.execute_script(
            (ROOT / "scripts/fixtures/reader_source_anchor_cases.js").read_text()
        )

    def test_prepared_cache_is_bounded_and_departed_reader_is_released(self):
        self.open_reader("/reader/100")
        self.js("delete audit.beforeControls; delete audit.oldFrame")
        value = self.driver.execute_async_script("""const done=arguments[0];(async()=>{
            const r=document.querySelector('#reader-root');
            for(let i=0;i<40;i++)await r.readerFixture.prepare(i);
            return r.readerFixture.sizes();
        })().then(done,e=>done({error:String(e)}));""")
        self.assertNotIn("error", value)
        self.assertLessEqual(
            value["chapters"], 4, "Prepared chapter cache must remain bounded"
        )
        self.assertLessEqual(
            value["maps"], 4, "Source maps must follow the bounded chapter cache"
        )
        self.driver.execute_async_script("""const done=arguments[0];
            document.querySelector('#reader-root').readerFixture.show(1).then(done);""")
        self.next_page()
        WebDriverWait(self.driver, 10).until(
            lambda _: (self.saved() or {}).get("section") == 1
        )
        # DOM click avoids WebDriver handles retaining nodes during memory observation.
        self.js(
            "document.querySelector('#reader-root a[href=\"/series/100\"]').click()"
        )
        self.wait("location.pathname==='/series/100'")
        self.assertFalse(
            self.js("return readerResume.fixtureHasWatch()"),
            "Departed reader watcher must be released",
        )
        self.assertFalse(
            self.js("return readerFullscreen.fixtureHasButton()"),
            "Departed fullscreen button must be released",
        )

    @contextmanager
    def space_document(self, html, rules):
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            path = Path(
                db.execute("SELECT file_path FROM items WHERE id=100").fetchone()[0]
            )
            chapter = db.execute(
                "SELECT content_html,content_size FROM chapters WHERE id=100"
            ).fetchone()
            original = path.read_bytes()
            with zipfile.ZipFile(path) as book:
                files = {name: book.read(name) for name in book.namelist()}
            files["OEBPS/ch0.xhtml"] = html.encode()
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
                for name, value in files.items():
                    book.writestr(name, value)
            db.execute(
                "UPDATE chapters SET content_html=?,content_size=? WHERE id=100",
                (html, len(html)),
            )
            db.execute("DELETE FROM text_replacements")
            for rule in rules:
                for column in ("series_id", "novel_id"):
                    db.execute(
                        "INSERT INTO text_replacements(user_id,"
                        + column
                        + ",find_text,replace_text) VALUES(1,100,?,?)",
                        (rule["find"], rule["replace"]),
                    )
        try:
            yield
        finally:
            path.write_bytes(original)
            with sqlite3.connect(self.fixture.data / "library.db") as db:
                db.execute(
                    "UPDATE chapters SET content_html=?,content_size=? WHERE id=100",
                    chapter,
                )
                db.execute("DELETE FROM text_replacements")

    def test_equivalent_spaces_render_and_record_epub_and_novel(self):
        source = (
            '<html><body><p id="space-match">Mr. Dentelle Mr.\u00a0Dentelle Mr.&nbsp;Dentelle Mr.&#160;Dentelle</p><p id="long">'
            + WORDS
            + "</p></body></html>"
        )
        with self.space_document(
            source, [{"find": "Mr. Dentelle", "replace": "Dentelle-san"}]
        ):
            for flow in ("paginated", "scrolled-doc"):
                for novel in (False, True):
                    with self.subTest(flow=flow, novel=novel):
                        with sqlite3.connect(self.fixture.data / "library.db") as db:
                            before = {
                                row[0]
                                for row in db.execute("SELECT id FROM reading_activity")
                            }
                            db.execute("DELETE FROM reading_progress WHERE item_id=100")
                            db.execute(
                                "DELETE FROM novel_reading_progress WHERE novel_id=100"
                            )
                        self.driver.get(self.base + "/")
                        self.driver.execute_script(
                            "localStorage.setItem('text-reader-prefs',JSON.stringify({flow:arguments[0],font:'original',fontSize:100,lineHeight:1.6,width:720,bottomGap:0}))",
                            flow,
                        )
                        self.driver.get(
                            self.base
                            + ("/novels/100/read/1/1" if novel else "/reader/100")
                        )
                        selector = (
                            "#custom-scroll iframe"
                            if flow == "scrolled-doc"
                            else "#paginated-viewer iframe"
                        )
                        self.wait(
                            "document.querySelector("
                            + repr(selector)
                            + ")?.style.visibility==='visible'"
                        )
                        text = self.driver.execute_script(
                            "return document.querySelector(arguments[0]).contentDocument.getElementById('space-match').textContent",
                            selector,
                        )
                        self.assertEqual(
                            text.count("Dentelle-san"),
                            4,
                            "Every equivalent-space occurrence must be replaced",
                        )
                        self.assertNotIn("Mr.", text)
                        time.sleep(1.1)
                        # Opening alone deliberately does not rewrite progress.
                        # Navigate with the real controls/gesture before checking it.
                        if flow == "paginated":
                            self.show_controls()
                            self.next_page()
                        else:
                            from selenium.webdriver.common.action_chains import (
                                ActionChains,
                            )
                            from selenium.webdriver.common.actions.wheel_input import (
                                ScrollOrigin,
                            )

                            ActionChains(self.driver).scroll_from_origin(
                                ScrollOrigin.from_element(
                                    self.driver.find_element(
                                        "css selector", "#custom-scroll iframe"
                                    )
                                ),
                                0,
                                400,
                            ).perform()
                            time.sleep(0.4)
                        self.driver.get(
                            self.base + ("/novels/100" if novel else "/series/100")
                        )
                        table = (
                            "novel_reading_progress WHERE novel_id=100"
                            if novel
                            else "reading_progress WHERE item_id=100"
                        )

                        def saved(_):
                            with sqlite3.connect(
                                self.fixture.data / "library.db"
                            ) as db:
                                row = db.execute(
                                    "SELECT replacement_version,source_anchor FROM "
                                    + table
                                ).fetchone()
                                visits = [
                                    r
                                    for r in db.execute(
                                        "SELECT id,identity,word_count FROM reading_activity"
                                    )
                                    if r[0] not in before
                                ]
                            return row and row[0] == 2 and visits and (row, visits)

                        row, visits = WebDriverWait(self.driver, 10).until(saved)
                        self.assertEqual(json.loads(row[1])["matchingVersion"], 2)
                        self.assertTrue(
                            all(
                                json.loads(r[1]).get("replacement_version") == 2
                                for r in visits
                            )
                        )
                        self.assertTrue(
                            any(r[2] >= 8 for r in visits),
                            "Displayed replacement activity must be accepted",
                        )

    def test_matching_upgrade_preserves_legacy_resume_inside_unchanged_replacement(
        self,
    ):
        source = (
            '<html><body><p id="long">😀 Mr.&nbsp;Dentelle name target '
            + WORDS
            + "</p></body></html>"
        )
        rules = [
            {"find": "Mr. Dentelle", "replace": "D"},
            {"find": "name", "replace": "expanded name"},
        ]
        with self.space_document(source, rules):
            for novel in (False, True):
                with self.subTest(novel=novel):
                    with sqlite3.connect(self.fixture.data / "library.db") as db:
                        if novel:
                            db.execute(
                                "INSERT OR REPLACE INTO novel_reading_progress(user_id,novel_id,chapter_id,paragraph_index,character_offset,replacement_version) VALUES(1,100,100,0,20,1)"
                            )
                        else:
                            db.execute(
                                "INSERT OR REPLACE INTO reading_progress(user_id,item_id,status,current_page,current_location,replacement_version) VALUES(1,100,'reading',0,'elidx:0:20',1)"
                            )
                    self.driver.get(self.base + "/")
                    self.js(
                        "localStorage.setItem('text-reader-prefs',JSON.stringify({flow:'paginated',font:'original',fontSize:100,lineHeight:1.6,width:720,bottomGap:0}))"
                    )
                    path = "/novels/100/read/1/1" if novel else "/reader/100"
                    expected = None
                    for repeat in range(2):
                        self.driver.get(self.base + path)
                        self.wait(
                            "document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
                        )
                        if expected is None:
                            self.assertTrue(
                                self.page()["long"].startswith("nded name target"),
                                "Legacy matching upgrade must retain the exact unchanged inserted passage",
                            )
                            self.show_controls()
                            self.next_page()
                            expected = self.first_marker()
                        else:
                            self.assertEqual(
                                self.first_marker(),
                                expected,
                                "A saved matching upgrade must not be applied twice",
                            )
                        self.driver.get(
                            self.base + ("/novels/100" if novel else "/series/100")
                        )

                        def version_saved(_):
                            with sqlite3.connect(
                                self.fixture.data / "library.db"
                            ) as db:
                                row = db.execute(
                                    "SELECT replacement_version FROM "
                                    + (
                                        "novel_reading_progress WHERE novel_id=100"
                                        if novel
                                        else "reading_progress WHERE item_id=100"
                                    )
                                ).fetchone()
                                return row and row[0] == 2

                        WebDriverWait(self.driver, 10).until(version_saved)

    def test_old_boosted_matching_modules_refresh_before_reading(self):
        self.driver.get(self.base + "/series/100")
        before = self.js("return audit.document")
        self.js("readerReplacements.version=1")
        self.driver.find_element("css selector", 'a[href="/reader/100"]').click()
        self.wait(
            "document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
        )
        self.assertNotEqual(
            self.js("return audit.document"),
            before,
            "Stale matching modules must refresh together",
        )
        self.assertEqual(
            self.js("return [readerReplacements.version,readerSourceAnchor.version]"),
            [2, 2],
        )

    def test_settings_save_feedback_duplicate_and_resync(self):
        self.driver.get(self.base + "/settings")
        self.wait(
            "!!document.querySelector('#add-replacement [role=combobox]') && !!window.libraryResync"
        )
        combo = self.driver.find_element(
            "css selector", "#add-replacement [role=combobox]"
        )
        combo.send_keys("Book 100")
        self.driver.find_element(
            "css selector", '#add-replacement [data-value="series:100"]'
        ).click()
        self.js("""window.replacementProbe={calls:0};const original=window.fetch;window.fetch=async(...args)=>{
            if(String(args[0]).endsWith('/settings/text-replacements') && args[1]?.method?.toUpperCase()==='POST') {
                replacementProbe.calls++;if(replacementProbe.hold) await new Promise(r=>replacementProbe.release=r);
                if(replacementProbe.fail){replacementProbe.fail=false;throw new TypeError('test network failure');}
            }return original(...args);
        };replacementProbe.hold=true;""")

        def fill(find, replace):
            for name, value in [("find_text", find), ("replace_text", replace)]:
                box = self.driver.find_element(
                    "css selector", "#add-replacement [name=" + name + "]"
                )
                box.clear()
                box.send_keys(value)

        def submit():
            self.js("document.querySelector('#add-replacement').requestSubmit()")

        fill("fixture find", "fixture replacement")
        submit()
        self.wait("!!replacementProbe.release")
        self.assertTrue(
            self.js(
                "return document.querySelector('#add-replacement button').disabled"
            ),
            "Pending replacement must disable Add",
        )
        self.assertEqual(
            self.js(
                "return document.querySelector('#replacement-save-status').textContent"
            ),
            "Saving…",
        )
        submit()
        self.assertEqual(
            self.js("return replacementProbe.calls"),
            1,
            "Pending duplicate submit must not issue a second request",
        )
        self.js("replacementProbe.hold=false;replacementProbe.release()")
        self.wait(
            "document.querySelector('#replacement-save-status').textContent==='Saved.'"
        )
        self.wait(
            "document.querySelectorAll('#replacement-rules [data-rule-id]').length===1"
        )
        self.assertEqual(
            self.js(
                "return document.querySelector('#add-replacement [name=find_text]').value"
            ),
            "",
        )
        fill("fixture find", "fixture replacement")
        submit()
        self.wait("!!document.querySelector('#replacement-rules [role=alert]')")
        self.assertEqual(
            self.js(
                "return document.querySelectorAll('#replacement-rules [data-rule-id]').length"
            ),
            1,
        )
        self.assertEqual(
            self.js(
                "return document.querySelector('#add-replacement [name=find_text]').value"
            ),
            "fixture find",
        )
        fill("another find", "draft preserved")
        self.driver.execute_async_script(
            "const done=arguments[0];libraryResync().then(()=>done(true))"
        )
        self.wait("!!document.querySelector('#add-replacement [role=combobox]')")
        self.assertEqual(
            self.js(
                "return document.querySelector('#add-replacement select[name=target]').value"
            ),
            "series:100",
            "Resync must preserve the chosen target",
        )
        self.assertEqual(
            self.js(
                "return document.querySelector('#add-replacement [name=replace_text]').value"
            ),
            "draft preserved",
        )
        self.assertEqual(
            self.js(
                "return document.querySelectorAll('#add-replacement [role=combobox]').length"
            ),
            1,
        )
        combo = self.driver.find_element(
            "css selector", "#add-replacement [role=combobox]"
        )
        combo.click()
        combo.send_keys(Keys.CONTROL, "a")
        combo.send_keys("Book 101")
        self.assertTrue(
            self.driver.find_element(
                "css selector", '#add-replacement [data-value="series:101"]'
            ).is_displayed(),
            "Resynced combobox must filter another target",
        )
        self.driver.find_element(
            "css selector", '#add-replacement [data-value="series:101"]'
        ).click()
        self.assertEqual(
            self.js(
                "return document.querySelector('#add-replacement select[name=target]').value"
            ),
            "series:101",
        )
        self.js("replacementProbe.fail=true")
        submit()
        self.wait(
            "document.querySelector('#replacement-save-status').textContent==='Could not save. Try again.'"
        )
        self.assertFalse(
            self.js("return document.querySelector('#add-replacement button').disabled")
        )
        self.assertEqual(
            self.js(
                "return document.querySelector('#add-replacement [name=find_text]').value"
            ),
            "another find",
        )
        submit()
        self.wait(
            "document.querySelector('#replacement-save-status').textContent==='Saved.'"
        )
        self.wait(
            "document.querySelectorAll('#replacement-rules [data-rule-id]').length===2"
        )

        fill("third find", "third replacement")
        self.js("replacementProbe.hold=true;replacementProbe.release=null")
        submit()
        self.wait("!!replacementProbe.release")
        fill("next draft", "next replacement")
        self.js("replacementProbe.hold=false;replacementProbe.release()")
        self.wait(
            "document.querySelector('#replacement-save-status').textContent==='Saved.'"
        )
        self.wait(
            "document.querySelectorAll('#replacement-rules [data-rule-id]').length===3"
        )
        self.assertEqual(
            self.js(
                "return document.querySelector('#add-replacement [name=find_text]').value"
            ),
            "next draft",
            "Save must retain a draft typed while pending",
        )
        self.assertEqual(
            self.js(
                "return document.querySelector('#add-replacement [name=replace_text]').value"
            ),
            "next replacement",
        )
        identity = self.js(
            "return [...document.querySelectorAll('#replacement-rules [data-rule-id]')].at(-1).dataset.ruleId"
        )
        selector = '#replacement-rules [data-rule-id="' + identity + '"]'
        self.driver.find_element("css selector", selector + " [data-rule-edit]").click()
        for name, value in [
            ("find_text", "another find"),
            ("replace_text", "draft preserved"),
        ]:
            box = self.driver.find_element(
                "css selector", selector + " [data-rule-edit-form] [name=" + name + "]"
            )
            box.clear()
            box.send_keys(value)
        self.driver.execute_script(
            'document.querySelector(arguments[0]+" [data-rule-edit-form]").requestSubmit()',
            selector,
        )
        self.wait("!!document.querySelector('#replacement-rules [role=alert]')")
        self.assertTrue(
            self.driver.find_element(
                "css selector", selector + " [data-rule-edit-form]"
            ).is_displayed(),
            "Rejected duplicate edit must stay open",
        )
        self.assertEqual(
            self.driver.find_element(
                "css selector", selector + " [name=find_text]"
            ).get_attribute("value"),
            "another find",
            "Rejected duplicate edit must retain typed text",
        )
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            self.assertEqual(
                db.execute(
                    "SELECT find_text FROM text_replacements WHERE id=?", (identity,)
                ).fetchone()[0],
                "third find",
            )

        self.driver.find_element(
            "css selector", selector + " [data-rule-cancel]"
        ).click()
        self.driver.find_element("css selector", selector + " [data-rule-edit]").click()
        self.assertEqual(
            self.driver.find_element(
                "css selector", selector + " [name=find_text]"
            ).get_attribute("value"),
            "third find",
            "Cancel must restore the saved value after refusal",
        )

    def test_amoled_text_contrast_preserves_readable_palette(self):
        with sqlite3.connect(self.fixture.data / "library.db") as db:
            path = Path(
                db.execute("SELECT file_path FROM items WHERE id=100").fetchone()[0]
            )
        original = path.read_bytes()
        try:
            with zipfile.ZipFile(path) as book:
                files = {name: book.read(name) for name in book.namelist()}
            files["OEBPS/ch0.xhtml"] = PALETTE_SAMPLE.encode()
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
                for name, value in files.items():
                    book.writestr(name, value)
            for flow in ["paginated", "scrolled-doc"]:
                with self.subTest(flow=flow):
                    self.driver.get(self.base + "/")
                    self.driver.execute_script(
                        "localStorage.setItem('text-reader-prefs',JSON.stringify({flow:arguments[0],theme:'amoled',font:'original',fontSize:75,lineHeight:1.4,width:720,bottomGap:0}))",
                        flow,
                    )
                    self.driver.get(self.base + "/reader/100")
                    selector = (
                        "#paginated-viewer iframe"
                        if flow == "paginated"
                        else "#custom-scroll iframe"
                    )
                    self.wait(
                        "document.querySelector("
                        + repr(selector)
                        + ")?.style.visibility==='visible'"
                    )
                    data = self.driver.execute_script(PALETTE_SNAPSHOT, selector)
                    rows = {r["id"]: r for r in data["rows"]}
                    self.assertFalse(any(r.get("missing") for r in rows.values()))
                    for key in [
                        "baseline",
                        "author",
                        "inline",
                        "priority",
                        "fill",
                        "system",
                        "whitebox",
                        "parent",
                        "nested",
                        "emphasis",
                        "link",
                        "lowColor",
                    ]:
                        self.assertGreaterEqual(
                            rows[key]["contrast"], 4.5, "Legible painted prose: " + key
                        )
                    for key, color in [
                        ("whitebox", "rgb(0, 0, 0)"),
                        ("nested", "rgb(0, 0, 0)"),
                        ("emphasis", "rgb(224, 112, 112)"),
                        ("link", "rgb(118, 185, 255)"),
                        ("transparent", "rgba(0, 0, 0, 0)"),
                    ]:
                        self.assertEqual(rows[key]["paint"], color)
                    self.assertEqual(rows["hidden"]["opacity"], "0")
                    self.assertEqual(
                        data["svg"],
                        dict(fill="rgb(238, 136, 68)", nestedFill="rgb(0, 0, 0)"),
                    )
                    for background, expected in [
                        ("white", "rgb(0, 0, 0)"),
                        ("black", "rgb(208, 208, 208)"),
                    ]:
                        self.driver.execute_script(
                            "const d=document.querySelector(arguments[0]).contentDocument,l=d.createElement('link');l.rel='stylesheet';l.href='data:text/css,'+encodeURIComponent('#author{background:'+arguments[1]+'!important}');d.head.append(l)",
                            selector,
                            background,
                        )
                        self.wait(
                            "(()=>{const f=document.querySelector("
                            + repr(selector)
                            + ");return f.contentWindow.getComputedStyle(f.contentDocument.getElementById('author')).webkitTextFillColor==="
                            + repr(expected)
                            + "})()"
                        )
                    self.assertEqual(
                        self.driver.execute_script(
                            "return document.querySelector(arguments[0]).contentDocument.body.textContent",
                            selector,
                        ),
                        data["text"],
                    )
        finally:
            path.write_bytes(original)


PALETTE_SAMPLE = r"""<html><head><style>
p {margin:2px 0!important} .black {color:#000} .priority {color:#000!important}
.fill {color:#d0d0d0;-webkit-text-fill-color:#000}
</style></head><body>
<p id="baseline">Ordinary inherited text.</p>
<p id="author" class="black">Black authored prose.</p>
<p id="inline" style="color:#000!important">Black inline prose.</p>
<p id="priority" class="priority">Black important prose.</p>
<p id="fill" class="fill">Black fill prose.</p>
<p id="system" style="color:CanvasText">System text color.</p>
<p id="whitebox" style="background:#fff;color:#000">Black on white.</p>
<p id="parent" class="black">Parent black on black. <span id="nested" style="background:#fff">Inherited black on white.</span><svg id="nestedGraphic" width="10" height="10"><rect id="nestedRect" width="10" height="10" fill="currentColor"/></svg></p>
<p id="emphasis" style="color:#e07070">Readable colored prose.</p>
<p><a id="link" href="#baseline" style="color:#76b9ff">Readable link.</a></p>
<p id="lowColor" style="color:#202080">Low contrast blue.</p>
<p id="transparent" style="color:transparent">Intentionally transparent.</p>
<p id="hidden" style="opacity:0;color:#000">Intentionally hidden.</p>
<svg id="graphic" width="20" height="10" color="#ee8844"><rect id="rect" width="20" height="10" fill="currentColor"/></svg>
</body></html>"""
PALETTE_SNAPSHOT = r"""const f=document.querySelector(arguments[0]),d=f.contentDocument,w=f.contentWindow;
const rgba=v=>{const n=v.match(/[\d.]+/g)?.map(Number);return n&&n.length>=3?n:null;};
const lum=c=>c.slice(0,3).map(n=>{n/=255;return n<=.04045?n/12.92:((n+.055)/1.055)**2.4}).reduce((a,n,i)=>a+n*[.2126,.7152,.0722][i],0);
const bg=el=>{for(let e=el;e;e=e.parentElement){const c=rgba(w.getComputedStyle(e).backgroundColor);if(c&&(c.length===3||c[3]===1))return c;}return [0,0,0];};
return {rows:['baseline','author','inline','priority','fill','system','whitebox','parent','nested','emphasis','link','lowColor','transparent','hidden'].map(id=>{
 const el=d.getElementById(id);if(!el)return {id,missing:true};const s=w.getComputedStyle(el),paint=s.webkitTextFillColor||s.color,c=rgba(paint),b=bg(el);
 return {id,color:s.color,paint,background:b,opacity:s.opacity,contrast:c?(Math.max(lum(c),lum(b))+.05)/(Math.min(lum(c),lum(b))+.05):null};
}),svg:{fill:w.getComputedStyle(d.getElementById('rect')).fill,nestedFill:w.getComputedStyle(d.getElementById('nestedRect')).fill},text:d.body.textContent};"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    for name, default in [
        ("chrome-binary", "/usr/bin/chromium"),
        ("chromedriver", "/usr/bin/chromedriver"),
        ("firefox-binary", "/usr/bin/firefox"),
        ("geckodriver", "/usr/bin/geckodriver"),
    ]:
        parser.add_argument("--" + name, default=default)
    parser.add_argument(
        "--test",
        action="append",
        choices=[
            name for name in ReplacementChecks.__dict__ if name.startswith("test_")
        ],
    )
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="Skip deliberate-defect controls for focused diagnosis",
    )
    audit.ARGS = parser.parse_args()
    browsers = selected_browsers(audit.ARGS)
    names = (
        audit.ARGS.test
        if audit.ARGS.test
        else [name for name in ReplacementChecks.__dict__ if name.startswith("test_")]
    )

    def suite(tests=names, widths=(1280, 320)):
        return unittest.TestSuite(
            type(
                browser.title() + "Replacements" + str(width),
                (ReplacementChecks,),
                {"browser": browser, "viewport_width": width},
            )(test)
            for browser in browsers
            for width in widths
            for test in tests
        )

    result = unittest.TextTestRunner(verbosity=2).run(suite())
    unittest_failures(result)
    if not baseline_passed(result, len(browsers) * 2 * len(names)):
        return 1
    if audit.ARGS.baseline_only or audit.ARGS.test:
        return 0
    for filename, before, after, test, signal in [
        (
            "app/stats/activity_rules.py",
            "if basis.identity is None:\n        return\n    rules_json",
            "if True:\n        return\n    rules_json",
            "test_pending_activity_survives_replacement_edit",
            "Pending activity must be recorded after replacement edits",
        ),
        (
            "app/static/js/reader-text.js",
            "...(activityRuleSet ? {rule_set_ref:activityRuleSet} : {})",
            "...{}",
            "test_pending_activity_survives_replacement_edit",
            "Pending activity must be recorded after replacement edits",
        ),
        (
            "app/static/js/reading-time.js",
            "if (navigator.sendBeacon(endpoint, beaconBody)) return;",
            "navigator.sendBeacon(endpoint, beaconBody); return;",
            "test_exit_delivery_and_rejected_activity_boundaries",
            "Refused beacon must use acknowledged delivery",
        ),
        (
            "app/static/js/reading-time.js",
            "if (useBeacon && beaconBody.size < 60000 && navigator.sendBeacon)",
            "if (useBeacon && body.length < 60000 && navigator.sendBeacon)",
            "test_exit_delivery_and_rejected_activity_boundaries",
            "Beacon quota must use UTF-8 bytes",
        ),
        (
            "app/static/js/reading-time.js",
            "keepalive: !!(useBeacon && beaconBody.size < 60000)",
            "keepalive: !!(useBeacon && body.length < 60000)",
            "test_exit_delivery_and_rejected_activity_boundaries",
            "Oversized delivery must not use keepalive",
        ),
        (
            "app/static/js/reading-time.js",
            "if (navigator.sendBeacon(endpoint, beaconBody)) return;",
            "if (navigator.sendBeacon(endpoint, beaconBody)) { for (const v of activities) acknowledge(v); return; }",
            "test_exit_delivery_and_rejected_activity_boundaries",
            "Accepted beacon must await acknowledgment",
        ),
        (
            "app/static/js/reading-time.js",
            "const value = JSON.parse(localStorage.getItem(key));",
            "const value = JSON.parse(localStorage.getItem(key)); if(value)delete value.rejected;",
            "test_exit_delivery_and_rejected_activity_boundaries",
            "Previously rejected activity must remain excluded",
        ),
        (
            "app/text_replacements.py",
            ' + [{"activity_protocol": 1}]',
            "",
            "test_old_cached_reader_refreshes_the_activity_protocol",
            "Old cached reader must refresh the activity protocol",
        ),
        (
            "app/static/js/library-events.js",
            "if (location.pathname === `/novels/${data.novel_id}`) window.libraryResync?.();",
            "/* deliberate novel detail refresh defect */",
            "test_novel_numeric_bookmarks_and_new_id_links_keep_their_chapter",
            "A metadata event must refresh the open chapter list",
        ),
        (
            "app/static/js/reader-text.js",
            "if (pgPageEnd.index >= blocks.length && await window.readerResume?.checkOrder()) return;",
            "/* deliberate chapter boundary defect */",
            "test_novel_order_change_refreshes_at_a_chapter_boundary",
            "A chapter boundary must refresh a changed order",
        ),
        (
            "app/static/js/reader-resume.js",
            "window.location.replace(saved.resume_url);",
            "/* deliberate history refresh defect */",
            "test_novel_history_keeps_passage_when_order_moves_backwards",
            "History must refresh an earlier chapter order",
        ),
        (
            "app/static/js/reader-text.js",
            "const measured = lastCustomPosition || measureCustomPosition();",
            "const measured = measureCustomPosition();",
            "test_scroll_order_change_keeps_last_confirmed_chapter",
            "Scroll order refresh must keep the last confirmed chapter",
        ),
        (
            "app/static/js/reader-text.js",
            "await Promise.all([...progressRequests]);",
            "/* deliberate pending progress defect */",
            "test_order_refresh_waits_for_older_progress_requests",
            "Order refresh must await older progress before saving its checkpoint",
        ),
        (
            "app/static/js/reader-text.js",
            "api.version !== 2 ||",
            "false ||",
            "test_old_boosted_matching_modules_refresh_before_reading",
            "Stale matching modules must refresh together",
        ),
        (
            "app/static/js/reader-replacements.js",
            'const space = "[ \\u00a0]";',
            'const space = "[ ]";',
            "test_equivalent_spaces_render_and_record_epub_and_novel",
            "Every equivalent-space occurrence must be replaced",
        ),
        (
            "app/static/js/reader-text.js",
            "return chapter?.upgradedPosition || chapter?.anchorMap?.toDisplay(saved?.position) || fallback;",
            "return fallback;",
            "test_matching_upgrade_preserves_legacy_resume_inside_unchanged_replacement",
            "Legacy matching upgrade must retain the exact unchanged inserted passage",
        ),
        (
            "app/templates/_replacement_rules.html",
            "{% set retry = replacement_edit if replacement_edit and replacement_edit.id == rule.id else none %}",
            "{% set retry = none %}",
            "test_settings_save_feedback_duplicate_and_resync",
            "Rejected duplicate edit must stay open",
        ),
        (
            "app/static/js/text-replacements.js",
            "if (saved && submitted && Object.entries(submitted).every(([name, value]) => form.elements[name].value === value)) {",
            "if (saved && submitted) {",
            "test_settings_save_feedback_duplicate_and_resync",
            "Save must retain a draft typed while pending",
        ),
        (
            "app/static/js/reader-text.js",
            "        applyTextPalette(doc);",
            "        /* deliberate contrast defect */",
            "test_amoled_text_contrast_preserves_readable_palette",
            "Legible painted prose: author",
        ),
        (
            "app/static/js/text-replacements.js",
            "button.disabled = true;",
            "button.disabled = false;",
            "test_settings_save_feedback_duplicate_and_resync",
            "Pending replacement must disable Add",
        ),
        (
            "app/static/js/library-events.js",
            "window.textReplacementSettings?.restore(replacementDraft);",
            "/* deliberate draft restoration defect */",
            "test_settings_save_feedback_duplicate_and_resync",
            "Resync must preserve the chosen target",
        ),
        (
            "app/static/js/reading-time.js",
            "if (sending) { flushAgain = true; return; }",
            "if (sending) return;",
            "test_queued_activity_drains_after_the_tracker_stops",
            "Stopped tracker must drain activity queued during an in-flight response",
        ),
        (
            "app/static/js/reader-text.js",
            "return chapter.anchorMap?.toDisplay(saved.position) || fallback;",
            "return fallback;",
            "test_replacement_preserves_epub_and_novel_passage",
            "Preserve the original passage after a replacement",
        ),
        (
            "app/static/js/reader-text.js",
            "trimPreparedCaches();",
            "/* deliberate cache-bound defect */",
            "test_prepared_cache_is_bounded_and_departed_reader_is_released",
            "Prepared chapter cache must remain bounded",
        ),
        (
            "app/static/js/reader-resume.js",
            "unwatch(root) { if (watched?.root === root) watched = null; }",
            "unwatch(root) {}",
            "test_prepared_cache_is_bounded_and_departed_reader_is_released",
            "Departed reader watcher must be released",
        ),
        (
            "app/static/js/reader-text.js",
            'window.readerFullscreen.releaseButton?.(root.querySelector("#btn-fullscreen"));',
            "",
            "test_prepared_cache_is_bounded_and_departed_reader_is_released",
            "Departed fullscreen button must be released",
        ),
    ]:
        path = ROOT / filename
        original = path.read_text()
        if original.count(before) != 1:
            raise AssertionError(
                "Deliberate defect must match exactly one production statement"
            )
        try:
            path.write_text(original.replace(before, after))
            result = unittest.TextTestRunner(verbosity=2).run(suite([test], (320,)))
        finally:
            path.write_text(original)
        if not mutation_rejected(result, browsers, "Replacements320", signal):
            raise AssertionError("Every selected browser must detect " + signal)
        print("Selected browsers detected: " + signal, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
