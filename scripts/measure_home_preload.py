"""Compare home detail navigation against an unchanged checkout using real pointer actions.

Run from the candidate repository with --baseline-root pointing to a detached
checkout of the comparison commit. All data and accounts are disposable.
"""

import argparse
import os
import sys
import subprocess
import time
import json
import statistics
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_resync_browser as fixture_module
import test_audit_browser as audit
import requests
from browser_checks import add_browser_argument, selected_browsers
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline-root", type=Path, required=True)
parser.add_argument(
    "--destination", choices=("series", "stats", "settings"), default="series"
)
parser.add_argument(
    "--cache-states", nargs="+", choices=("cold", "warm"), default=["warm"]
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
if audit.ARGS.rounds < 4:
    parser.error("Use at least four alternating pairs per condition")
if not (audit.ARGS.baseline_root / "app/main.py").is_file():
    parser.error("The baseline must be an EbookReader checkout")


@classmethod
def start_server(cls):
    server_module = cls.fixture / "preload_measure_app.py"
    server_module.write_text("""import asyncio
from http.cookies import SimpleCookie
from app.main import app as application
async def app(scope,receive,send):
    if scope['type']=='http' and scope['path'] in ('/series/1','/stats','/settings'):
        cookie=SimpleCookie(dict(scope['headers']).get(b'cookie',b'').decode())
        delay=cookie.get('measure-delay')
        if delay: await asyncio.sleep(int(delay.value)/1000)
    await application(scope,receive,send)
""")
    cls.log = open(cls.fixture / "server.log", "a")
    cls.server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "preload_measure_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(cls.port),
        ],
        cwd=fixture_module.ROOT,
        env={
            **os.environ,
            "APP_DATA_DIR": str(cls.data),
            "APP_SCHEDULED_SCAN_CRON": "",
            "PYTHONPATH": str(cls.fixture) + ":" + str(fixture_module.ROOT),
        },
        stdout=cls.log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(150):
        try:
            if requests.get(cls.base + "/login", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.1)
    raise RuntimeError("Server failed")


class MeasurementFirefoxService(audit.FirefoxService):
    def __init__(self, *args, **kwargs):
        kwargs["service_args"] = ["--allow-system-access"]
        super().__init__(*args, **kwargs)


audit.FirefoxService = MeasurementFirefoxService
DESTINATIONS = {
    "series": (
        "/series/1",
        '#series-grid [data-series-id="1"]',
        '#items-grid [data-item-id="1"]',
    ),
    "stats": ("/stats", '#user-menu-dropdown a[href="/stats"]', "main .viz"),
    "settings": ("/settings", '#user-menu-dropdown a[href="/settings"]', "#profile"),
}
path, link_selector, ready_selector = DESTINATIONS[audit.ARGS.destination]


def clear_cache(driver, browser):
    if browser == "chrome":
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
    else:
        driver.set_context("chrome")
        try:
            driver.execute_script("Services.cache2.clear()")
        finally:
            driver.set_context("content")


fixtures = {}
try:
    for name, root in [("before", audit.ARGS.baseline_root.resolve()), ("after", ROOT)]:
        fixture_module.ROOT = root
        cls = type(
            name,
            (fixture_module.BrowserChecks, unittest.TestCase),
            {"start_server": start_server},
        )
        fixtures[name] = cls
        cls.setUpClass()
    fixture_module.ROOT = ROOT
    fixtures["before"].base = fixtures["before"].base.replace("localhost", "127.0.0.1")
    rows = []
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
                for cache_state in audit.ARGS.cache_states:
                    for delay in [0, 120, 400]:
                        for trial in range(audit.ARGS.rounds):
                            for name in (
                                ["before", "after"]
                                if trial % 2 == 0
                                else ["after", "before"]
                            ):
                                cls = fixtures[name]
                                driver.get(cls.base + "/")
                                clear_cache(driver, browser)
                                driver.add_cookie(
                                    {"name": "measure-delay", "value": "0"}
                                )
                                if cache_state == "warm":
                                    driver.get(cls.base + path)
                                    case.wait(
                                        "!!document.querySelector("
                                        + json.dumps(ready_selector)
                                        + ")"
                                    )
                                driver.get(cls.base + "/")
                                driver.add_cookie(
                                    {"name": "measure-delay", "value": str(delay)}
                                )
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
                                    """window.measure={requests:0}; const original=fetch;
            const path=arguments[0], selector=arguments[1];
            window.fetch=(url,...rest)=>{if(String(url)===path)measure.requests++;return original(url,...rest)};
            for(const type of ['mousedown','touchstart','click']) document.addEventListener(type,e=>{if(e.target.closest('a[href="'+path+'"]'))measure[type]=performance.now()},{capture:true});
            const frame=()=>{if(location.pathname===path && document.querySelector(selector)) {measure.ready=performance.now();return;} requestAnimationFrame(frame)};requestAnimationFrame(frame);""",
                                    path,
                                    ready_selector,
                                )
                                if audit.ARGS.destination != "series":
                                    driver.find_element("id", "user-menu-btn").click()
                                card = driver.find_element(
                                    "css selector", link_selector
                                )
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
                                        "delay": delay,
                                        "cache": cache_state,
                                        "variant": name,
                                        "ms": value["ready"] - value["click"],
                                        "requests": value["requests"],
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
                            and r["delay"] == delay
                            and r["cache"] == cache_state
                        ]
                        print(
                            browser,
                            width,
                            cache_state,
                            delay,
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
                "destination": audit.ARGS.destination,
                "cache_states": audit.ARGS.cache_states,
                "browsers": list(selected_browsers(audit.ARGS)),
                "navigation": rows,
                "home": home,
            },
            indent=2,
        )
    )
    if any(row["requests"] != 1 for row in rows):
        raise AssertionError("Normal activations must issue exactly one detail request")
finally:
    for cls in fixtures.values():
        cls.doClassCleanups()
