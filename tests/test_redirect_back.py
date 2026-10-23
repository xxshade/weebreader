"""A plain form POST goes back to the page it came from -- in this app.

Sixteen routes redirected to the raw Referer header; they now all go
through `redirect_back` / `back_target`, which keep only its path and query
when that is a path this app may redirect to (the same rule as `next`).
"""
import logging
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.requests import Request

from app.config import settings
from app.database import close_pool, init_db
from app.http_utils import back_target, same_app_path

logging.getLogger("httpx").setLevel(logging.WARNING)
ROOT = Path(__file__).resolve().parent.parent


def _request(referer=None):
    headers = [(b"referer", referer.encode())] if referer is not None else []
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers,
                    "query_string": b""})


class TheTarget(unittest.TestCase):
    def test_the_page_it_came_from(self):
        self.assertEqual(back_target(_request("http://t/series/4?sort=x"), "/"), "/series/4?sort=x")

    def test_another_site_keeps_only_a_path_of_this_app(self):
        self.assertEqual(back_target(_request("https://evil.example/x"), "/"), "/x")

    def test_nothing_usable_is_the_fallback(self):
        for referer in (None, "", "https://evil.example//evil.example/", "http://t/\\evil"):
            self.assertEqual(back_target(_request(referer), "/series/1"), "/series/1", referer)

    def test_same_app_path_drops_the_host(self):
        self.assertEqual(same_app_path("https://a.example/b?c=1"), "/b?c=1")
        self.assertIsNone(same_app_path(None))


class NoRouteReadsTheHeaderItself(unittest.TestCase):
    def test_only_the_helper_reads_the_referer(self):
        readers = [
            str(path.relative_to(ROOT))
            for path in (ROOT / "app").rglob("*.py")
            if re.search(r"""headers\.get\(\s*["']referer["']""", path.read_text())
        ]
        self.assertEqual(sorted(readers), ["app/http_utils.py", "app/main.py"])


class ARoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        (settings.data_dir / "covers").mkdir()
        await init_db()
        from app.main import app
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        self.addAsyncCleanup(self.client.aclose)
        await self.client.post("/setup", data={"username": "u", "password": "pw"})
        r = await self.client.post("/login", data={"username": "u", "password": "pw"})
        self.client.cookies = r.cookies
        # These tests check redirects. Do not leave a real reindex task
        # writing after the disposable database has been closed.
        scheduler = patch('app.library.router._run_background', side_effect=lambda coro: coro.close())
        scheduler.start()
        self.addCleanup(scheduler.stop)

    async def test_stays_in_the_app(self):
        r = await self.client.post("/library/reindex-numbers",
                                   headers={"referer": "https://evil.example/library/issues"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/library/issues")

    async def test_goes_back_where_it_came_from(self):
        r = await self.client.post("/library/reindex-numbers", headers={"referer": "http://t/settings"})
        self.assertEqual(r.headers["location"], "/settings")


if __name__ == "__main__":
    unittest.main()
