"""Changing a status has to reach the OTHER screens.

Both statuses a card can wear are decided somewhere the card is not: the
publication status and the tracking status are set on a series' or a
novel's own page, and the badge that shows them -- plus the header filter
and the hidden-statuses setting that act on them -- live on the home grid.
Without an event, a home page open in another tab went on showing the old
badge until it was reloaded, which is the same shape of bug the tracking
dot had.

What is pinned here is the publishing, not the value: a tracking status is
a row per USER and the event reaches every connected browser, so the
event carries only which card changed and each browser refetches its own
(`/library/{kind}/{id}/card`). An event carrying the status would tell
every other reader what this one is reading.
"""
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.datastructures import FormData
from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.tracking import service as tracking_service
from app.library import events as library_events
from app.library.router import set_series_publication_status, set_series_status
from app.novels.router import (
    clear_novel_status, set_novel_publication_status, set_novel_status,
)


def _request(form):
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
        "headers": [(b"hx-request", b"true")],
        "client": ("test", 1), "server": ("test", 80),
    }
    request = Request(scope)
    request._form = FormData(form)
    return request


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        # Setting any status now queues a tracker push (nothing linked
        # here, so it ends at once), and it must end before this test's
        # database is deleted.
        self.addAsyncCleanup(tracking_service.drain)
        self.user = {"id": 1}
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'M','/m','comic')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'Serie', 'serie', '/f')")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url, status)"
            " VALUES (1, 'N', 'https://x/1', 'Ongoing')")
        await self.db.commit()

        self.published = []
        original_publish = library_events.publish
        library_events.publish = self.published.append
        self.addCleanup(lambda: setattr(library_events, "publish", original_publish))

    async def _close(self):
        await self.db.close()

    def _events(self, kind):
        return [e for e in self.published if e["type"] == kind]


class APublicationStatusReachesTheGrid(_Case):
    async def test_setting_a_series_publishes(self):
        await set_series_publication_status(
            1, _request({"publication_status": "hiatus"}), self.user, self.db)
        self.assertEqual(self._events("series_updated"),
                         [{"type": "series_updated", "series_id": 1}])

    async def test_setting_a_novel_publishes(self):
        # Its own event kind, because the grid looks the two kinds up by
        # different attributes -- only a real series carries
        # data-series-id.
        await set_novel_publication_status(
            1, _request({"publication_status": "canceled"}), self.user, self.db)
        self.assertEqual(self._events("novel_updated"),
                         [{"type": "novel_updated", "novel_id": 1}])

    async def test_clearing_a_novels_override_publishes_too(self):
        # Going back to following the source changes the badge exactly as
        # much as setting one did.
        await set_novel_publication_status(
            1, _request({"publication_status": ""}), self.user, self.db)
        self.assertEqual(self._events("novel_updated"),
                         [{"type": "novel_updated", "novel_id": 1}])

    async def test_a_refused_value_publishes_nothing(self):
        # The write never happened, so announcing a change would send every
        # browser to refetch a card that did not move.
        for endpoint, target in ((set_series_publication_status, "series_updated"),
                                 (set_novel_publication_status, "novel_updated")):
            with self.assertRaises(HTTPException):
                await endpoint(1, _request({"publication_status": "nonsense"}),
                               self.user, self.db)
            self.assertEqual(self._events(target), [])


class ATrackingStatusReachesTheGrid(_Case):
    async def test_setting_a_series_status_publishes(self):
        await set_series_status(1, _request({"status": "reading"}), self.user, self.db)
        self.assertEqual(self._events("series_updated"),
                         [{"type": "series_updated", "series_id": 1}])

    async def test_clearing_a_series_status_publishes(self):
        # The badge going away is a change to the card, and so is the card
        # coming BACK from behind a hidden status.
        await set_series_status(1, _request({"status": "reading"}), self.user, self.db)
        self.published.clear()
        await set_series_status(1, _request({"status": ""}), self.user, self.db)
        self.assertEqual(self._events("series_updated"),
                         [{"type": "series_updated", "series_id": 1}])

    async def test_the_series_event_does_not_carry_the_status(self):
        # It is a row per user and this reaches every connected browser.
        await set_series_status(1, _request({"status": "completed"}), self.user, self.db)
        self.assertNotIn("status", self._events("series_updated")[0])

    async def test_an_invalid_series_status_publishes_nothing(self):
        with self.assertRaises(HTTPException):
            await set_series_status(1, _request({"status": "ongoing!"}), self.user, self.db)
        self.assertEqual(self._events("series_updated"), [])

    async def test_a_novel_status_names_the_novel_it_changed(self):
        # The event already existed for the "Reading" strip, which needs no
        # id; the card refresh does, so this is what the JS now reads.
        await set_novel_status(1, _request({"status": "reading"}), self.user, self.db)
        self.assertEqual([e["novel_id"] for e in self._events("novel_status_updated")], [1])

    async def test_clearing_a_novel_names_it_too(self):
        await clear_novel_status(1, _request({}), self.user, self.db)
        self.assertEqual([e["novel_id"] for e in self._events("novel_status_updated")], [1])
