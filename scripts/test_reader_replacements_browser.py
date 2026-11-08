"""Real text-replacement, activity, history and cache regressions in both engines.

Only disposable data and localhost are used. Deliberate defects run sequentially;
use a separate checkout when running this script beside another validation suite.
The fixture instruments cache observation, not replacement or position behavior.
"""

import argparse
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
        self.wait(f'location.pathname==="{back}" && window.userMenuBound===true')
        self.frames()
        self.driver.find_element("id", "user-menu-btn").click()
        self.wait(
            "!document.querySelector('#user-menu-dropdown').classList.contains('hidden')"
        )
        self.driver.find_element(
            "css selector", '#user-menu-dropdown a[href="/settings"]'
        ).click()
        self.wait(
            "location.pathname==='/settings' && !!document.querySelector('#add-replacement select[name=target]')"
        )

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
            "document.querySelector('#reader-root')?.dataset.replacements && JSON.parse(document.querySelector('#reader-root').dataset.replacements).length===1 && document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'"
        )
        self.assertEqual(self.first_marker(), before)
        self.assertIn(
            "Renamed character",
            self.js(
                "return document.querySelector('#reader-root').dataset.replacements"
            ),
            "History must refresh the currently displayed rules",
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
        [audit.ARGS.test]
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
