"""Measure series HTML, cover transfer and decoded first-viewport readiness.

Run from the candidate repository with --baseline-root pointing to a detached
checkout of the comparison commit. All data and accounts are disposable.
"""

import argparse
import sys
import subprocess
import json
import statistics
import unittest
import sqlite3
import random
from contextlib import closing
from PIL import Image
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_resync_browser as fixture_module
import test_audit_browser as audit
from browser_checks import add_browser_argument, selected_browsers
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline-root", type=Path, required=True)
parser.add_argument("--items", type=int, default=160)
parser.add_argument("--desktop-thumbnail", type=int, default=280)
parser.add_argument(
    "--cache-states", nargs="+", choices=("cold", "warm"), default=["cold", "warm"]
)
parser.add_argument("--rounds", type=int, default=8)
parser.add_argument("--output", type=Path, required=True)
for name, default in [
    ("chrome-binary", "/usr/bin/chromium"),
    ("chromedriver", "/usr/bin/chromedriver"),
    ("firefox-binary", "/usr/bin/firefox"),
    ("geckodriver", "/usr/bin/geckodriver"),
]:
    parser.add_argument("--" + name, default=default)
add_browser_argument(parser)
audit.ARGS = parser.parse_args()
if not 110 <= audit.ARGS.desktop_thumbnail <= 400:
    parser.error("Desktop thumbnail size must be between 110 and 400px")
if audit.ARGS.items < 2:
    parser.error("Use at least two items")
if audit.ARGS.rounds < 4:
    parser.error("Use at least four alternating pairs per condition")
if not (audit.ARGS.baseline_root / "app/main.py").is_file():
    parser.error("The baseline must be an EbookReader checkout")


class MeasurementFirefoxService(audit.FirefoxService):
    def __init__(self, *args, **kwargs):
        kwargs["service_args"] = ["--allow-system-access"]
        super().__init__(*args, **kwargs)


audit.FirefoxService = MeasurementFirefoxService
path = "/series/1"
link_selector = '#series-grid [data-series-id="1"]'
ready_selector = '#items-grid [data-item-id="1"]'


def clear_cache(driver, browser):
    if browser == "chrome":
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
    else:
        driver.set_context("chrome")
        try:
            driver.execute_script(
                'Services.cache2.clear(); Cc["@mozilla.org/image/tools;1"].getService(Ci.imgITools).getImgCacheForDocument(null).clearCache(false);'
            )
        finally:
            driver.set_context("content")


fixtures = {}
try:
    for name, root in [("before", audit.ARGS.baseline_root.resolve()), ("after", ROOT)]:
        fixture_module.ROOT = root
        cls = type(
            name,
            (fixture_module.BrowserChecks, unittest.TestCase),
            {},
        )
        fixtures[name] = cls
        cls.setUpClass()
        rng = random.Random(837)
        with closing(sqlite3.connect(cls.data / "library.db")) as db, db:
            db.row_factory = sqlite3.Row
            original = dict(db.execute("SELECT * FROM items WHERE id=1").fetchone())
            db.execute("DELETE FROM items WHERE id>1")
            for i in range(1, audit.ARGS.items + 1):
                cover = f"item-{i}-fixture.webp"
                Image.frombytes("RGB", (400, 600), rng.randbytes(400 * 600 * 3)).save(
                    cls.data / "covers" / cover, quality=80
                )
                if i == 1:
                    db.execute(
                        "UPDATE items SET cover_path=?, title='Volume 001' WHERE id=1",
                        (cover,),
                    )
                else:
                    row = {
                        **original,
                        "id": i,
                        "title": f"Volume {i:03}",
                        "sort_title": f"volume{i:03}",
                        "cover_path": cover,
                        "file_path": str(cls.fixture / f"volume-{i}.cbz"),
                        "file_hash": str(i),
                    }
                    columns = list(row)
                    db.execute(
                        "INSERT INTO items ("
                        + ",".join(columns)
                        + ") VALUES ("
                        + ",".join("?" for _ in columns)
                        + ")",
                        list(row.values()),
                    )
    fixture_module.ROOT = ROOT

    rows = []
    versions = {}
    home = []
    for browser in selected_browsers(audit.ARGS):
        case = type(
            "Measure",
            (audit.AuditChecks,),
            {
                "browser": browser,
                "fixture": fixtures["after"],
                "base": fixtures["after"].base,
            },
        )("runTest")
        try:
            case.setUp()
            driver = case.driver
            versions[browser] = driver.capabilities["browserVersion"]
            for cls in fixtures.values():
                with requests.Session() as session:
                    session.post(
                        cls.base + "/login",
                        data={
                            "username": "probe",
                            "password": "disposable-browser-test-password",
                        },
                    ).raise_for_status()
                    driver.get(cls.base + "/login")
                    for name, value in session.cookies.items():
                        driver.add_cookie({"name": name, "value": value})
            # Cookies are host-scoped, not port-scoped. Accept both disposable
            # session tokens in each fixture so both origins use localhost.
            for origin in fixtures.values():
                with closing(sqlite3.connect(origin.data / "library.db")) as db:
                    sessions = db.execute(
                        "SELECT user_id,token_hash,expires_at,created_at,totp_verified FROM sessions"
                    ).fetchall()
                for target in fixtures.values():
                    with closing(sqlite3.connect(target.data / "library.db")) as db, db:
                        for session_row in sessions:
                            db.execute(
                                "INSERT OR IGNORE INTO sessions (user_id,token_hash,expires_at,created_at,totp_verified) VALUES ("
                                + ",".join("?" for _ in session_row)
                                + ")",
                                session_row,
                            )
            for width in [1280, 320]:
                if browser == "chrome":
                    driver.execute_cdp_cmd(
                        "Emulation.setDeviceMetricsOverride",
                        {
                            "width": width,
                            "height": 800,
                            "deviceScaleFactor": 1,
                            "mobile": False,
                        },
                    )
                else:
                    driver.browsing_context.set_viewport(
                        context=driver.current_window_handle,
                        viewport={"width": width, "height": 800},
                    )
                case.assertEqual(case.js("return innerWidth"), width)
                for cache_state in audit.ARGS.cache_states:
                    for trial in range(audit.ARGS.rounds):
                        for name in (
                            ["before", "after"]
                            if trial % 2 == 0
                            else ["after", "before"]
                        ):
                            cls = fixtures[name]
                            # A unique URL also excludes the browser's decoded
                            # image cache. The warm-up reuses this exact URL.
                            sample = f"{width}-{cache_state}-{trial}"
                            with closing(
                                sqlite3.connect(cls.data / "library.db")
                            ) as db, db:
                                db.execute(
                                    "UPDATE items SET cover_path='item-' || id || '-fixture.webp?sample=' || ?",
                                    (sample,),
                                )
                            driver.get(cls.base + "/")
                            clear_cache(driver, browser)
                            driver.add_cookie(
                                {
                                    "name": "thumbnail-size%3Aseries",
                                    "value": (
                                        str(audit.ARGS.desktop_thumbnail)
                                        if width == 1280
                                        else "144"
                                    ),
                                    "path": "/",
                                }
                            )
                            if cache_state == "warm":
                                driver.get(cls.base + path)
                                case.wait(
                                    "!!document.querySelector("
                                    + json.dumps(ready_selector)
                                    + ")"
                                )
                                case.wait(
                                    "[...document.querySelectorAll('#items-grid img')].filter(i=>{const r=i.getBoundingClientRect();return r.top<innerHeight && r.bottom>0}).every(i=>i.complete && i.naturalWidth)"
                                )
                            driver.get(cls.base + "/")
                            case.wait(
                                "window.libraryGrid?.isLive() && document.querySelector('[data-series-id=\"1\"]')?._htmx?.boosted && document.getElementById('library-events')?._htmx?.sse?.lastEventId"
                            )
                            driver.execute_async_script(
                                "const done=arguments[0];requestAnimationFrame(()=>requestAnimationFrame(done))"
                            )
                            home.append(
                                {
                                    "browser": browser,
                                    "width": width,
                                    "variant": name,
                                    "cache": cache_state,
                                    "ms": case.js(
                                        'return performance.getEntriesByType("navigation")[0].domContentLoadedEventEnd'
                                    ),
                                }
                            )
                            driver.execute_script(
                                r"""window.measure={requests:0}; const original=fetch;
        const path=arguments[0], selector=arguments[1];
        window.fetch=(url,...rest)=>{if(String(url)===path)measure.requests++;return original(url,...rest)};
        for(const type of ['mousedown','touchstart','click']) document.addEventListener(type,e=>{if(e.target.closest('a[href="'+path+'"]'))measure[type]=performance.now()},{capture:true});
        const frame=()=>{
            if(location.pathname===path && document.querySelector(selector)) {
                measure.html ||= performance.now();
                const visible=[...document.querySelectorAll('#items-grid img')].filter(i=>{
                    const r=i.getBoundingClientRect();return r.width>0 && r.top<innerHeight && r.bottom>0;
                });
                measure.visible=visible.length;
                if(visible.length && visible.every(i=>i.complete && i.naturalWidth)) {
                    measure.loaded=performance.now();
                    measure.visibleURLs=visible.map(i=>i.currentSrc.split('/').pop());
                    Promise.all(visible.map(i=>i.decode())).then(()=>{
                        measure.decoded=performance.now();
                        requestAnimationFrame(()=>{
                        measure.ready=performance.now();
                        measure.images=performance.getEntriesByType('resource').filter(e=>e.name.includes('/covers/item-')).map(e=>({url:e.name.split('/').pop(),start:e.startTime,response:e.responseStart,end:e.responseEnd,bytes:e.transferSize}));
                        });
                    });return;
                }
            }
            requestAnimationFrame(frame);
        };requestAnimationFrame(frame);""",
                                path,
                                ready_selector,
                            )
                            card = driver.find_element("css selector", link_selector)
                            driver.execute_script(
                                'arguments[0].scrollIntoView({block:"center"})',
                                card,
                            )
                            actions = ActionBuilder(
                                driver,
                                mouse=PointerInput(
                                    "touch" if width == 320 else "mouse",
                                    (
                                        "measure-touch"
                                        if width == 320
                                        else "measure-mouse"
                                    ),
                                ),
                                duration=0,
                            )
                            actions.pointer_action.move_to(card)
                            actions.pointer_action.pointer_down()
                            actions.pointer_action.pause(0.06)
                            actions.pointer_action.pointer_up()
                            actions.perform()
                            case.wait("!!measure.ready")
                            value = case.js("return measure")
                            rows.append(
                                {
                                    "browser": browser,
                                    "width": width,
                                    "cache": cache_state,
                                    "variant": name,
                                    "ms": value["ready"] - value["click"],
                                    "requests": value["requests"],
                                    "html_ms": value["html"] - value["click"],
                                    "visible": value["visible"],
                                    "visible_urls": value["visibleURLs"],
                                    "loaded_ms": value["loaded"] - value["click"],
                                    "decoded_ms": value["decoded"] - value["click"],
                                    "thumb_size": case.js(
                                        "return getComputedStyle(document.querySelector('main')).getPropertyValue('--thumb-size').trim()"
                                    ),
                                    "images": value["images"],
                                    "click": value["click"],
                                    "held": value["click"]
                                    - value.get(
                                        "touchstart",
                                        value.get("mousedown", value["click"]),
                                    ),
                                }
                            )
                    group = [
                        r
                        for r in rows
                        if r["browser"] == browser
                        and r["width"] == width
                        and r["cache"] == cache_state
                    ]
                    print(
                        browser,
                        width,
                        cache_state,
                        {
                            v: round(
                                statistics.median(
                                    r["ms"] for r in group if r["variant"] == v
                                ),
                                1,
                            )
                            for v in ["before", "after"]
                        },
                        "requests",
                        sorted(set(r["requests"] for r in group)),
                        flush=True,
                    )
        finally:
            case.doCleanups()
    audit.ARGS.output.parent.mkdir(parents=True, exist_ok=True)
    audit.ARGS.output.write_text(
        json.dumps(
            {
                "baseline_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=audit.ARGS.baseline_root,
                    text=True,
                ).strip(),
                "rounds": audit.ARGS.rounds,
                "items": audit.ARGS.items,
                "desktop_thumbnail": audit.ARGS.desktop_thumbnail,
                "cache_states": audit.ARGS.cache_states,
                "browsers": list(selected_browsers(audit.ARGS)),
                "browser_versions": versions,
                "cover_bytes": (fixtures["after"].data / "covers/item-1-fixture.webp")
                .stat()
                .st_size,
                "navigation": rows,
                "home": home,
            },
            indent=2,
        )
    )
    if any(row["requests"] != 1 for row in rows):
        raise AssertionError("Normal activations must issue exactly one detail request")
    for row in rows:
        expected_thumb = (
            f"{audit.ARGS.desktop_thumbnail}px" if row["width"] == 1280 else "144px"
        )
        if row["thumb_size"] != expected_thumb:
            raise AssertionError("Saved cover size was not applied")
        for url in row["visible_urls"]:
            images = [i for i in row["images"] if i["url"] == url]
            if len(images) > 1:
                raise AssertionError("A visible cover must not be requested twice")
            transferred = any(i["bytes"] > 0 for i in images)
            if transferred != (row["cache"] == "cold"):
                raise AssertionError(
                    "Cold/warm cover cache protocol was not established"
                )
finally:
    for cls in fixtures.values():
        cls.doClassCleanups()
