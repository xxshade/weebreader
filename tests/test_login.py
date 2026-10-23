"""Signing in with a password and, when enabled, a TOTP code.

* A TOTP code signs in once. pyotp's verify accepted any code of the
  current window again, so one read over a shoulder (or from a proxy log
  of the form) was good for about a minute and a half more.
* The attempts remembered for the rate limit are bounded: every failed
  name used to be kept for good.
* argon2 runs in a thread, and a name that matches nobody still costs a
  hash, so the timing does not say which half was wrong.
"""
import asyncio
from datetime import datetime, timezone
import tempfile
import threading
import unittest
from pathlib import Path

import pyotp
from starlette.requests import Request

from app.auth import router as auth_router
from app.auth import service
from app.config import settings
from app.database import close_pool, get_connection, init_db, read_connection


class LoginCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        await init_db()
        self.secret = pyotp.random_base32()
        db = await get_connection()
        try:
            await db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'me', ?)",
                             (service.hash_password("right"),))
            await db.commit()
        finally:
            await db.close()
        auth_router._failed_login_attempts.clear()
        self.addCleanup(auth_router._failed_login_attempts.clear)

    async def enable_totp(self):
        db = await get_connection()
        try:
            await db.execute("INSERT INTO totp_credentials (user_id, secret, enabled) VALUES (1, ?, 1)",
                             (self.secret,))
            await db.commit()
        finally:
            await db.close()

    async def login(self, password="right", code=None, username="me"):
        async with read_connection() as db:
            return await auth_router.login_submit(
                _request("/login"), None, username=username, password=password, totp_code=code,
                next=None, db=db)


def _request(path):
    """A bare request for calling a handler directly: what it needs is a
    request to render the refusal page against."""
    return Request({"type": "http", "method": "POST", "path": path, "headers": [],
                    "query_string": b"", "server": ("t", 80), "scheme": "http", "root_path": ""})


class ATotpCodeSignsInOnce(LoginCase):
    async def test_the_first_use_signs_in(self):
        await self.enable_totp()
        response = await self.login(code=pyotp.TOTP(self.secret).now())
        self.assertEqual(response.status_code, 302)

    async def test_the_same_code_again_is_refused(self):
        await self.enable_totp()
        code = pyotp.TOTP(self.secret).now()
        await self.login(code=code)
        # Refused as the login page saying why (a plain POST, see login.html),
        # not as an exception htmx would have swallowed.
        self.assertEqual((await self.login(code=code)).status_code, 401)

    async def test_two_logins_racing_with_one_code_get_one_session(self):
        await self.enable_totp()
        code = pyotp.TOTP(self.secret).now()
        results = await asyncio.gather(self.login(code=code), self.login(code=code))
        self.assertEqual(sorted(r.status_code for r in results), [302, 401])

    async def test_a_wrong_code_is_refused(self):
        await self.enable_totp()
        response = await self.login(
            code="000000" if pyotp.TOTP(self.secret).now() != "000000" else "111111")
        self.assertEqual(response.status_code, 401)

    async def test_the_code_that_confirmed_the_setup_cannot_sign_in(self):
        code = pyotp.TOTP(self.secret).now()
        await auth_router.totp_setup_submit(
            _request("/settings/totp"), secret=self.secret, code=code, user={"id": 1, "username": "me"})
        self.assertEqual((await self.login(code=code)).status_code, 401)

    async def test_the_step_either_side_of_now_still_counts(self):
        totp = pyotp.TOTP(self.secret)
        now = totp.timecode(datetime.now(timezone.utc))
        self.assertEqual(service.totp_step(self.secret, totp.generate_otp(now - 1)), now - 1)
        self.assertEqual(service.totp_step(self.secret, totp.generate_otp(now + 1)), now + 1)
        self.assertIsNone(service.totp_step(self.secret, totp.generate_otp(now + 3)))


class PasswordsAreCheckedOffTheLoop(LoginCase):
    async def test_in_a_thread(self):
        where = []
        original = service.verify_password

        def recording(plain, hashed):
            where.append(threading.current_thread() is threading.main_thread())
            return original(plain, hashed)

        service.verify_password = recording
        self.addCleanup(lambda: setattr(service, "verify_password", original))
        await self.login()
        self.assertEqual(where, [False])

    async def test_an_unknown_name_is_hashed_too(self):
        calls = []
        original = service._hasher

        class Counting:
            def verify(self, *args):
                calls.append(1)
                return original.verify(*args)

        service._hasher = Counting()
        self.addCleanup(lambda: setattr(service, "_hasher", original))
        self.assertEqual((await self.login(username="nobody")).status_code, 401)
        self.assertEqual(len(calls), 1)


class TheRateLimitRemembersABoundedNumberOfNames(LoginCase):
    async def test_many_names_do_not_grow_it_past_the_cap(self):
        original = auth_router.LOGIN_TRACKED_NAMES
        auth_router.LOGIN_TRACKED_NAMES = 20
        self.addCleanup(lambda: setattr(auth_router, "LOGIN_TRACKED_NAMES", original))
        for n in range(60):
            auth_router._record_failed_login(f"name{n}")
        self.assertLessEqual(len(auth_router._failed_login_attempts), 20)
        # The most recent are the ones kept.
        self.assertIn("name59", auth_router._failed_login_attempts)

    async def test_the_real_account_still_locks(self):
        for _ in range(auth_router.LOGIN_MAX_ATTEMPTS):
            self.assertEqual((await self.login(password="wrong")).status_code, 401)
        self.assertEqual((await self.login()).status_code, 429)


if __name__ == "__main__":
    unittest.main()
