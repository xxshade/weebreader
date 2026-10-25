"""Successful account mutations invalidate anticipated pages after commit."""
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app.account_events import AccountChangeNotifications
from app.database import close_pool
from app.auth.dependencies import get_current_user
from app.auth.router import router as auth_router
from app.stats.router import router as stats_router
from app.tracking.router import router as tracking_router
from app.tracking import store
from tests.test_progress_validation import ProgressCase


class AccountUpdates(ProgressCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.addAsyncCleanup(close_pool)
        self.user['page_time_cap_seconds'] = 180
        self.app = FastAPI()
        self.app.include_router(auth_router)
        self.app.include_router(stats_router)
        self.app.include_router(tracking_router)
        self.app.add_middleware(AccountChangeNotifications)
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    async def test_settings_notification_observes_the_committed_value(self):
        committed = []
        def published(event):
            with closing(sqlite3.connect(self._tmp.name + '/library.db')) as db:
                committed.append((event, db.execute(
                    'SELECT text_time_cap_seconds FROM users WHERE id=1').fetchone()[0]))
        with patch('app.account_events.events.publish', side_effect=published):
            response = await self.client.post('/settings/text-time-cap',
                data={'cap_seconds': 240}, headers={'HX-Request': 'true'})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(committed, [({'type': 'account_updated'}, 240)])

    async def test_text_replacement_commit_refreshes_open_estimates(self):
        from app.library.router import router as library_router
        self.app.include_router(library_router)
        committed = []
        def published(event):
            with closing(sqlite3.connect(self._tmp.name + '/library.db')) as db:
                committed.append((event['type'], db.execute(
                    "SELECT replace_text FROM text_replacements WHERE user_id=1").fetchone()[0]))
        with patch('app.account_events.events.publish', side_effect=published):
            response = await self.client.post('/settings/text-replacements', data={
                'target':'novel:1', 'find_text':'one', 'replace_text':'two words'}, headers={'HX-Request':'true'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(committed, [('account_updated','two words'),('reading_estimate_updated','two words')],
                         'Replacement changes must refresh estimates after the rule commits')

    async def test_confirmed_reading_time_notifies_after_commit(self):
        committed = []
        def published(event):
            with closing(sqlite3.connect(self._tmp.name + '/library.db')) as db:
                committed.append((event, db.execute('SELECT SUM(seconds) FROM page_reads').fetchone()[0]))
        with patch('app.account_events.events.publish', side_effect=published):
            response = await self.client.post('/reading-time', json={
                'spans': [{'item_id': 1, 'page_index': 0, 'seconds': 5, 'ago': 0}]})
        self.assertEqual(response.json(), {'stored': 1})
        self.assertEqual(committed, [({'type': 'account_updated'}, 5)])

    async def test_failed_submissions_and_get_requests_do_not_notify(self):
        with patch('app.account_events.events.publish') as publish:
            response = await self.client.post('/reading-time', json={'spans': 'invalid'})
            self.assertEqual(response.status_code, 400)
            response = await self.client.get('/reading-time')
            self.assertEqual(response.status_code, 405)
            publish.assert_not_called()

    async def test_successful_oauth_get_callback_notifies_after_commit(self):
        await store.save_pending_oauth(self.db, 1, 'myanimelist', state='state', verifier='verifier')
        await self.db.commit()
        committed = []
        def published(event):
            with closing(sqlite3.connect(self._tmp.name + '/library.db')) as db:
                committed.append((event, db.execute(
                    "SELECT username FROM tracker_accounts WHERE tracker='myanimelist'").fetchone()[0]))
        with patch('app.tracking.router.myanimelist.exchange_code',
                   new=AsyncMock(return_value={'access_token': 'disposable', 'expires_in': 3600})), \
             patch('app.tracking.router.myanimelist.whoami', new=AsyncMock(return_value='Reader')), \
             patch('app.account_events.events.publish', side_effect=published):
            response = await self.client.get('/settings/tracking/myanimelist/callback?code=code&state=state')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(committed, [({'type': 'account_updated'}, 'Reader')])
        with patch('app.account_events.events.publish') as publish:
            response = await self.client.get('/settings/tracking/myanimelist/callback?code=code&state=state')
            self.assertIn('bad-callback', response.headers['location'])
            publish.assert_not_called()
