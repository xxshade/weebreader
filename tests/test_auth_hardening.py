"""Two holes in sign-in, measured in both browsers before they were closed.

- `next=/\\evil.com` passed the "same-app path" check, and a browser reads
  "/\\evil.com" as "//evil.com": after a passkey login (which follows `next`
  with location.href) the user landed on the other site.
- The passkey endpoint shared ONE rate-limit bucket for everyone, so five
  bogus POSTs from anybody locked the owner out of passkey login for five
  minutes, renewable forever -- with nothing there to brute-force.
"""
import logging
import tempfile
import unittest
from pathlib import Path

import httpx

from app.auth.router import _safe_next_path
from app.config import settings
from app.database import close_pool, init_db

logging.getLogger("httpx").setLevel(logging.WARNING)


class NextPath(unittest.TestCase):
    def test_same_app_paths_pass(self):
        for path in ("/", "/stats?days=90", "/library/series/3#top"):
            self.assertEqual(_safe_next_path(path), path)

    def test_anything_that_can_leave_the_app_does_not(self):
        for path in ("//evil.com", "/\\evil.com", "/\\/evil.com", "http://evil.com",
                     "evil.com", "/a\nb", "/a\tb", "", None):
            self.assertEqual(_safe_next_path(path), "/", repr(path))

    def test_the_passkey_page_checks_by_origin(self):
        html = (Path(__file__).resolve().parent.parent / "app/templates/auth/login.html").read_text()
        self.assertIn("url.origin === window.location.origin", html)


class PasskeyLoginCannotBeLockedOut(unittest.IsolatedAsyncioTestCase):
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

    async def test_bogus_attempts_never_answer_429(self):
        statuses = {(await self.client.post("/login/passkey/complete", json={"id": "bogus"})).status_code
                    for _ in range(12)}
        self.assertEqual(statuses, {401})

    async def test_a_body_that_is_not_an_object_is_a_400(self):
        for body in ("[]", '"x"', "not json"):
            r = await self.client.post("/login/passkey/complete", content=body,
                                       headers={"content-type": "application/json"})
            self.assertEqual(r.status_code, 400, body)


class Refusals(PasskeyLoginCannotBeLockedOut):
    """What a signed-out request is answered with, by who is asking."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        r = await self.client.post("/setup", data={"username": "u", "password": "pw"})
        self.assertEqual(r.status_code, 302)

    async def test_a_wrong_password_is_the_login_page_saying_so(self):
        r = await self.client.post("/login", data={"username": "u", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("Invalid username or password", r.text)
        self.assertIn('value="u"', r.text)

    async def test_the_lockout_says_so_too(self):
        for _ in range(5):
            await self.client.post("/login", data={"username": "locked", "password": "nope"})
        r = await self.client.post("/login", data={"username": "locked", "password": "nope"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Too many failed login attempts", r.text)

    async def test_a_page_load_returns_to_the_page_with_its_query(self):
        r = await self.client.get("/stats?days=90", headers={"accept": "text/html"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/login?next=%2Fstats%3Fdays%3D90")

    async def test_htmx_is_told_to_go_to_login_not_handed_it_as_success(self):
        r = await self.client.post("/settings/page-time-cap", data={"page_time_cap_seconds": "60"},
                                   headers={"accept": "text/html", "hx-request": "true",
                                            "hx-current-url": "http://t/settings"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers["hx-redirect"], "/login?next=%2Fsettings")

    async def test_a_plain_post_returns_to_the_page_it_came_from(self):
        r = await self.client.post("/settings/page-time-cap", data={"page_time_cap_seconds": "60"},
                                   headers={"accept": "text/html", "referer": "http://t/settings"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/login?next=%2Fsettings")

    async def test_fetch_still_gets_a_plain_401(self):
        r = await self.client.post("/reading-time", json={"spans": []})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("hx-redirect", r.headers)

    async def test_a_reading_time_body_that_is_a_list_is_a_400(self):
        r = await self.client.post("/login", data={"username": "u", "password": "pw"})
        self.assertEqual(r.status_code, 302)
        self.client.cookies = r.cookies
        r = await self.client.post("/reading-time", json=[])
        self.assertEqual(r.status_code, 400)
