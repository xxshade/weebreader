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
