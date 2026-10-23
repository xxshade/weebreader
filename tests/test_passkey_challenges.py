"""Whose passkey challenge is whose, and how long a session lasts.

- The login challenge was ONE row for everybody: each `begin` deleted the
  pending one, so anyone could keep the owner's passkey login failing with
  unauthenticated POSTs (measured with soft-webauthn against a server: the
  owner's assertion got 401 after a stranger's begin). Each begin now has
  its own row, named by a cookie, taken out once and only while fresh.
- The session cookie was 30 days whatever `session_lifetime_hours` said.
- An expired session was deleted only when presented again.

The signature check itself is the webauthn library's, exercised end to end
by scripts/test_webauthn_flow.py; here it is replaced by a stand-in that
records which challenge it was asked to verify against.
"""
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from app.auth import router as auth_router
from app.auth import service
from app.config import settings
from app.database import close_pool, init_db, write_transaction

logging.getLogger("httpx").setLevel(logging.WARNING)


class AuthCase(unittest.IsolatedAsyncioTestCase):
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
        self.app = app
        self.owner = self._client()
        r = await self.owner.post("/setup", data={"username": "u", "password": "pw"})
        self.assertEqual(r.status_code, 302)

    def _client(self):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t")
        self.addAsyncCleanup(client.aclose)
        return client

    async def _sql(self, sql, params=()):
        async with write_transaction() as db:
            rows = await (await db.execute(sql, params)).fetchall()
            await db.commit()
        return rows


class PasskeyLogin(AuthCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self._sql(
            "INSERT INTO webauthn_credentials (user_id, credential_id, public_key, sign_count, nickname)"
            " VALUES (1, 'cred', 'key', 0, 'k')")
        self.verified_against = []

        def verify(credential_json, challenge, public_key, sign_count):
            self.verified_against.append(challenge)
            return SimpleNamespace(new_sign_count=1)

        patcher = mock.patch.object(service, "verify_authentication", verify)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _begin(self, client):
        r = await client.post("/login/passkey/begin")
        self.assertEqual(r.status_code, 200)
        return r.json()["challenge"]

    async def _complete(self, client):
        return await client.post("/login/passkey/complete", json={"id": "cred"})

    async def test_a_strangers_begin_leaves_the_owners_challenge_alone(self):
        challenge = await self._begin(self.owner)
        await self._begin(self._client())
        r = await self._complete(self.owner)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.verified_against, [challenge])
        self.assertIn("session", r.cookies)

    async def test_a_challenge_is_used_once(self):
        await self._begin(self.owner)
        self.assertEqual((await self._complete(self.owner)).status_code, 200)
        self.assertEqual((await self._complete(self.owner)).status_code, 400)

    async def test_a_browser_that_never_began_has_no_challenge(self):
        await self._begin(self.owner)
        r = await self._complete(self._client())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.verified_against, [])

    async def test_a_stale_challenge_is_refused(self):
        await self._begin(self.owner)
        await self._sql("UPDATE webauthn_challenges SET created_at = datetime('now', '-6 minutes')")
        self.assertEqual((await self._complete(self.owner)).status_code, 400)

    async def test_a_begin_sweeps_the_stale_ones(self):
        await self._begin(self.owner)
        await self._sql("UPDATE webauthn_challenges SET created_at = datetime('now', '-6 minutes')")
        await self._begin(self._client())
        rows = await self._sql("SELECT COUNT(*) AS n FROM webauthn_challenges")
        self.assertEqual(rows[0]["n"], 1)


class PasskeyRegistration(AuthCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        r = await self.owner.post("/login", data={"username": "u", "password": "pw"})
        self.owner.cookies = r.cookies

    async def test_a_body_that_is_not_an_object_is_a_400(self):
        await self.owner.post("/settings/passkey/register/begin")
        for body in ("[]", "not json"):
            r = await self.owner.post("/settings/passkey/register/complete", content=body,
                                      headers={"content-type": "application/json"})
            self.assertEqual(r.status_code, 400, body)

    async def test_a_response_that_does_not_verify_is_a_400_not_a_500(self):
        await self.owner.post("/settings/passkey/register/begin")
        r = await self.owner.post("/settings/passkey/register/complete", json={"id": "forged"})
        self.assertEqual(r.status_code, 400)

    async def test_a_stale_registration_challenge_is_refused(self):
        await self.owner.post("/settings/passkey/register/begin")
        await self._sql("UPDATE webauthn_challenges SET created_at = datetime('now', '-6 minutes')")
        with mock.patch.object(service, "verify_registration") as verify:
            r = await self.owner.post("/settings/passkey/register/complete", json={"id": "x"})
        self.assertEqual(r.status_code, 400)
        verify.assert_not_called()


class Sessions(AuthCase):
    async def test_the_cookie_lasts_as_long_as_the_session(self):
        original = settings.session_lifetime_hours
        settings.session_lifetime_hours = 2
        self.addCleanup(lambda: setattr(settings, "session_lifetime_hours", original))
        r = await self.owner.post("/login", data={"username": "u", "password": "pw"})
        self.assertIn("Max-Age=7200", r.headers["set-cookie"])

    async def test_a_login_sweeps_expired_sessions(self):
        await self._sql(
            "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (1, 'old', '2000-01-01T00:00:00+00:00')")
        await self.owner.post("/login", data={"username": "u", "password": "pw"})
        rows = await self._sql("SELECT token_hash FROM sessions")
        self.assertEqual([row["token_hash"] for row in rows if row["token_hash"] == "old"], [])
        self.assertEqual(len(rows), 1)

    async def test_both_logins_go_through_the_one_cookie_helper(self):
        source = Path(auth_router.__file__).read_text()
        self.assertEqual(source.count('set_cookie(\n        "session"'), 1)
        self.assertEqual(source.count("_set_session_cookie(response, token)"), 2)


if __name__ == "__main__":
    unittest.main()
