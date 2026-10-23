"""Reporting progress to MangaUpdates.

Two things here can be wrong in a way nobody notices, so both are pinned
against a real database and a real (mocked) transport rather than against
the code's own arguments:

  * WHAT number counts as "how far I have read". It is a MAX over what is
    marked read, in the unit the shelf is numbered in -- not a count, and
    not the last thing touched.
  * WHEN that number is allowed to leave. Never below what the service
    already holds, and never a request at all when there is nothing new,
    because a tracker that can walk backwards damages data this app does
    not own and does it invisibly.
"""

import asyncio
import json
import os
import tempfile
import urllib.parse
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app.config import settings
from app.database import get_connection, init_db
from starlette.requests import Request

from app.library import events as library_events
from app.tracking import mangaupdates, models, myanimelist, router, service, store
from app.tracking.store import MANGAUPDATES, MYANIMELIST

ROOT = Path(__file__).resolve().parents[1]


def code_of(path: Path) -> str:
    """A script with whole-line `//` comments blanked out.

    These are scans for CODE, and the comment beside the handler below
    explains the rule by NAMING the event it forbids -- prose that reads
    as a breach of the rule it is documenting. Same helper, and same
    reason, as tests/test_htmx_version.py.
    """
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in path.read_text().splitlines())


def selected_status(html: str) -> str | None:
    """The value of the status select's selected option, from rendered
    markup -- None when nothing is selected at all."""
    select = html.split('name="status"', 1)[1].split("</select>", 1)[0]
    for option in select.split("<option")[1:]:
        head = option.split(">", 1)[0]
        if "selected" in head:
            return head.split('value="', 1)[1].split('"', 1)[0]
    return None


def mock_client(handler, module=mangaupdates):
    """An httpx client whose requests never leave the process."""
    def build():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return patch.object(module, "_client", build)


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(settings, "myanimelist_client_id", "fixture-client-id"))
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        self.addAsyncCleanup(service.drain)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (2,'v','x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'L','/l','comic')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def series(self, series_id=1, kind="volume"):
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path, number_kind) "
            "VALUES (?, 1, 'S', 's', 's', ?)",
            (series_id, kind),
        )
        await self.db.commit()

    async def item(self, item_id, number, *, series_id=1):
        await self.db.execute(
            "INSERT INTO items (id, series_id, library_id, title, sort_title, file_path, "
            "file_hash, file_size, file_format, number_in_series) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, 1, 'cbz', ?)",
            (item_id, series_id, f"t{item_id}", f"t{item_id}", f"/p{item_id}", f"h{item_id}",
             number),
        )
        await self.db.commit()

    async def read(self, item_id, user_id=1):
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent) "
            "VALUES (?, ?, 'completed', 100)",
            (user_id, item_id),
        )
        await self.db.commit()

    async def novel(self, novel_id=1, chapters=(1.0, 2.0, 3.0)):
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url) VALUES (?, 'N', ?)",
            (novel_id, f"https://x/{novel_id}"))
        await self.db.execute(
            "INSERT INTO volumes (id, novel_id, number) VALUES (?, ?, 1)", (novel_id, novel_id))
        for index, number in enumerate(chapters, start=1):
            await self.db.execute(
                "INSERT INTO chapters (id, novel_id, volume_id, number, title, url) "
                "VALUES (?, ?, ?, ?, 'c', ?)",
                (novel_id * 100 + index, novel_id, novel_id, number, f"https://x/{novel_id}/{index}"))
        await self.db.commit()

    async def read_chapter(self, chapter_id, user_id=1):
        await self.db.execute(
            "INSERT INTO novel_read_chapters (user_id, chapter_id) VALUES (?, ?)",
            (user_id, chapter_id))
        await self.db.commit()

    async def connect(self, user_id=1, token="tok"):
        await store.save_account(
            self.db, user_id, MANGAUPDATES, access_token=token, username="reader")
        await self.db.commit()

    async def link(self, *, series_id=None, novel_id=None, user_id=1, remote_id="99"):
        await store.save_link(
            self.db, user_id, MANGAUPDATES, series_id=series_id, novel_id=novel_id,
            remote_id=remote_id, remote_title="Remote", remote_url="https://mu/99")
        await self.db.commit()


class HowFarTheReaderHasGot(_Case):
    async def test_it_is_the_highest_number_read_and_not_a_count(self):
        # The distinction the whole feature turns on: three volumes read
        # out of order is 5, not 3. A count would walk the tracker back.
        await self.series()
        for item_id, number in ((1, 1.0), (2, 2.0), (3, 5.0), (4, 6.0)):
            await self.item(item_id, number)
        for item_id in (1, 2, 3):
            await self.read(item_id)
        progress = await store.series_progress(self.db, 1, 1)
        self.assertEqual(progress.volume, 5.0)
        # ...and nothing about chapters, which this shelf does not count.
        self.assertIsNone(progress.chapter)

    async def test_an_unread_item_in_between_does_not_lower_it(self):
        await self.series()
        await self.item(1, 1.0)
        await self.item(2, 2.0)
        await self.item(3, 3.0)
        await self.read(3)
        self.assertEqual((await store.series_progress(self.db, 1, 1)).volume, 3.0)

    async def test_nothing_read_is_nothing_rather_than_zero(self):
        await self.series()
        await self.item(1, 1.0)
        progress = await store.series_progress(self.db, 1, 1)
        self.assertIsNone(progress.volume)
        self.assertFalse(progress.anywhere)

    async def test_an_unnumbered_item_contributes_nothing(self):
        # An extras volume with no number must not become "0 read" and it
        # must not hide the numbers around it.
        await self.series()
        await self.item(1, 4.0)
        await self.item(2, None)
        await self.read(1)
        await self.read(2)
        self.assertEqual((await store.series_progress(self.db, 1, 1)).volume, 4.0)

    async def test_another_reader_does_not_move_your_number(self):
        await self.series()
        await self.item(1, 1.0)
        await self.item(2, 7.0)
        await self.read(1, user_id=1)
        await self.read(2, user_id=2)
        self.assertEqual((await store.series_progress(self.db, 1, 1)).volume, 1.0)

    async def test_the_unit_comes_from_the_series(self):
        # A chapter-per-file series reports chapters and says NOTHING about
        # volumes: sending 0 for the field it does not use would tell the
        # service the reader had read none of them.
        await self.series(kind="chapter")
        await self.item(1, 12.0)
        await self.read(1)
        progress = await store.series_progress(self.db, 1, 1)
        self.assertEqual(progress.chapter, 12.0)
        self.assertIsNone(progress.volume)

    async def test_a_web_novel_reports_both_units(self):
        # "vol 3 ch 41" is how such a novel is cited; reporting one of the
        # two throws away half the position.
        await self.novel()
        for chapter_id in (101, 102, 103):
            await self.read_chapter(chapter_id)
        progress = await store.novel_progress(self.db, 1, 1)
        self.assertEqual((progress.chapter, progress.volume, progress.total), (3.0, 1.0, 3.0))

    async def test_a_volume_is_only_reached_when_every_chapter_in_it_is_read(self):
        # The rule mark-as-read already taught this app: a volume with one
        # chapter read is not a volume anybody finished. The chapter still
        # advances -- the two disagreeing IS the reader's real position.
        await self.novel()
        await self.read_chapter(101)
        progress = await store.novel_progress(self.db, 1, 1)
        self.assertEqual(progress.chapter, 1.0)
        self.assertIsNone(progress.volume)

    async def test_an_empty_volume_is_not_a_finished_one(self):
        await self.novel()
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (9, 1, 9)")
        await self.db.commit()
        for chapter_id in (101, 102, 103):
            await self.read_chapter(chapter_id)
        self.assertEqual((await store.novel_progress(self.db, 1, 1)).volume, 1.0)


class WhenTheNumberIsAllowedToLeave(_Case):
    """service.push, with the service itself mocked."""

    def calls(self):
        """A handler that records every request and answers plausibly."""
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, request.content))
            if request.url.path.startswith("/v1/lists/series/") and request.method == "GET":
                return httpx.Response(200, json=self.remote)
            return httpx.Response(200, json={})

        self.remote = {"status": {"chapter": 0, "volume": 0}, "list_id": 0}
        return seen, handler

    async def test_an_unlinked_series_asks_nothing(self):
        await self.series()
        await self.item(1, 3.0)
        await self.read(1)
        seen, handler = self.calls()
        with mock_client(handler):
            self.assertEqual(await service.push(1, series_id=1), "unlinked")
        self.assertEqual(seen, [])

    async def test_a_linked_series_with_no_account_asks_nothing(self):
        # Something IS read here, so this really exercises the missing
        # account rather than stopping at "nothing to report" first.
        await self.series()
        await self.item(1, 3.0)
        await self.read(1)
        await self.link(series_id=1)
        seen, handler = self.calls()
        with mock_client(handler):
            self.assertEqual(await service.push(1, series_id=1), "disconnected")
        self.assertEqual(seen, [])

    async def test_nothing_marked_read_asks_nothing(self):
        await self.series()
        await self.item(1, 3.0)
        await self.connect()
        await self.link(series_id=1)
        seen, handler = self.calls()
        with mock_client(handler):
            self.assertEqual(await service.push(1, series_id=1), "nothing-read")
        self.assertEqual(seen, [])

    async def test_it_sends_the_number_in_the_series_own_unit(self):
        await self.series(kind="volume")
        await self.item(1, 4.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        seen, handler = self.calls()
        with mock_client(handler):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        update = [c for c in seen if c[1].endswith("/update")]
        self.assertEqual(len(update), 1)
        status = json.loads(update[0][2])[0]["status"]
        self.assertEqual(status, {"volume": 4})

    async def test_a_chapter_numbered_series_sends_chapters(self):
        await self.series(kind="chapter")
        await self.item(1, 12.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        seen, handler = self.calls()
        with mock_client(handler):
            await service.push(1, series_id=1)
        status = json.loads([c for c in seen if c[1].endswith("/update")][0][2])[0]["status"]
        self.assertEqual(status, {"chapter": 12})

    async def test_a_second_push_with_nothing_new_makes_no_request_at_all(self):
        await self.series()
        await self.item(1, 4.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        seen, handler = self.calls()
        with mock_client(handler):
            await service.push(1, series_id=1)
            before = len(seen)
            self.assertEqual(await service.push(1, series_id=1), "up-to-date")
        self.assertEqual(len(seen), before)

    async def test_agreeing_records_nothing_for_a_unit_this_shelf_does_not_count(self):
        # A volume-numbered series has no chapter of its own. Taking the
        # service's chapter for it recorded a 0 that was never sent, which
        # the panel then read out as "Last sent volume 5, chapter 0".
        await self.series(kind="volume")
        await self.item(1, 2.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        seen, handler = self.calls()
        self.remote = {"status": {"chapter": 0, "volume": 40}, "list_id": 0}
        with mock_client(handler):
            self.assertEqual(await service.push(1, series_id=1), "remote-ahead")
        link = await store.get_link(self.db, 1, MANGAUPDATES, series_id=1)
        self.assertIsNone(link["last_pushed_chapter"])
        self.assertEqual(link["last_pushed_volume"], 40)

    async def test_it_will_not_walk_the_service_backwards(self):
        # The reader is further ahead on the site than in this app. Their
        # position there is the one that must survive.
        await self.series()
        await self.item(1, 2.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        seen, handler = self.calls()
        self.remote = {"status": {"chapter": 0, "volume": 40}, "list_id": 0}
        with mock_client(handler):
            self.assertEqual(await service.push(1, series_id=1), "remote-ahead")
        self.assertEqual([c for c in seen if c[1].endswith("/update")], [])

    async def test_a_series_not_on_a_list_yet_is_added_first(self):
        await self.series()
        await self.item(1, 2.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        seen = []

        def handler(request):
            seen.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(404, json={})
            return httpx.Response(200, json={})

        with mock_client(handler):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        self.assertIn(("POST", "/v1/lists/series"), seen)

    async def test_a_refused_session_is_recorded_on_the_account_not_just_the_link(self):
        # Every other push will fail the same way until somebody signs in
        # again, and the settings page is the only place that can say so.
        await self.series()
        await self.item(1, 2.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        with mock_client(lambda request: httpx.Response(401, json={})):
            self.assertEqual(await service.push(1, series_id=1), "failed")
        account = await store.get_account(self.db, 1, MANGAUPDATES)
        link = await store.get_link(self.db, 1, MANGAUPDATES, series_id=1)
        self.assertIsNotNone(account["last_error"])
        self.assertIsNotNone(link["last_error"])
        self.assertIsNone(link["last_pushed_volume"])

    async def test_a_failure_leaves_the_number_unsent_so_the_next_push_retries(self):
        await self.series()
        await self.item(1, 2.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        with mock_client(lambda request: httpx.Response(500, json={})):
            await service.push(1, series_id=1)
        seen, handler = self.calls()
        with mock_client(handler):
            self.assertEqual(await service.push(1, series_id=1), "sent")

    async def test_a_web_novel_partway_through_a_volume_sends_only_the_chapter(self):
        await self.novel()
        await self.read_chapter(102)
        await self.connect()
        await self.link(novel_id=1)
        seen, handler = self.calls()
        with mock_client(handler):
            self.assertEqual(await service.push(1, novel_id=1), "sent")
        status = json.loads([c for c in seen if c[1].endswith("/update")][0][2])[0]["status"]
        self.assertEqual(status, {"chapter": 2})

    async def test_a_finished_volume_sends_both(self):
        await self.novel()
        for chapter_id in (101, 102, 103):
            await self.read_chapter(chapter_id)
        await self.connect()
        await self.link(novel_id=1)
        seen, handler = self.calls()
        with mock_client(handler):
            self.assertEqual(await service.push(1, novel_id=1), "sent")
        status = json.loads([c for c in seen if c[1].endswith("/update")][0][2])[0]["status"]
        self.assertEqual(status, {"chapter": 3, "volume": 1})

    async def test_a_chapter_only_push_does_not_forget_the_volume_already_sent(self):
        # A web novel advances in both, but rarely at the same moment:
        # after a volume lands, the next twenty pushes are chapters. If one
        # of those blanked the recorded volume, the next push would ask the
        # service about a volume it already has, forever.
        await self.novel(chapters=(1.0, 2.0, 3.0))
        for chapter_id in (101, 102, 103):
            await self.read_chapter(chapter_id)
        await self.connect()
        await self.link(novel_id=1)
        seen, handler = self.calls()
        with mock_client(handler):
            await service.push(1, novel_id=1)          # sends volume 1 + chapter 3
        link = await store.get_link(self.db, 1, MANGAUPDATES, novel_id=1)
        self.assertEqual(link["last_pushed_volume"], 1.0)

        # A new chapter in a volume that is not finished: chapter only.
        await self.db.execute(
            "INSERT INTO volumes (id, novel_id, number) VALUES (2, 1, 2)")
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url) "
            "VALUES (110, 1, 2, 4.0, 'c', 'https://x/1/4')")
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url) "
            "VALUES (111, 1, 2, 5.0, 'c', 'https://x/1/5')")
        await self.db.commit()
        await self.read_chapter(110)
        self.remote = {"status": {"chapter": 3, "volume": 1}, "list_id": 0}
        with mock_client(handler):
            self.assertEqual(await service.push(1, novel_id=1), "sent")
        status = json.loads([c for c in seen if c[1].endswith("/update")][-1][2])[0]["status"]
        self.assertEqual(status, {"chapter": 4})
        link = await store.get_link(self.db, 1, MANGAUPDATES, novel_id=1)
        self.assertEqual(link["last_pushed_volume"], 1.0)

    async def test_a_field_the_service_is_ahead_on_is_left_alone_while_the_other_moves(self):
        # The reason the comparison is per field: a whole-record check
        # would either skip the chapter or overwrite their volume.
        await self.novel()
        for chapter_id in (101, 102, 103):
            await self.read_chapter(chapter_id)
        await self.connect()
        await self.link(novel_id=1)
        seen, handler = self.calls()
        self.remote = {"status": {"chapter": 0, "volume": 12}, "list_id": 0}
        with mock_client(handler):
            self.assertEqual(await service.push(1, novel_id=1), "sent")
        status = json.loads([c for c in seen if c[1].endswith("/update")][0][2])[0]["status"]
        self.assertEqual(status, {"chapter": 3})


class TheLinkItself(_Case):
    async def test_relinking_forgets_what_was_sent_to_the_old_entry(self):
        # That number describes the OLD entry. Carried over, the first push
        # to the new one would decide there was nothing to send.
        await self.series()
        await self.link(series_id=1, remote_id="1")
        link = await store.get_link(self.db, 1, MANGAUPDATES, series_id=1)
        await store.record_push(self.db, link["id"], chapter=None, volume=12.0)
        await self.db.commit()
        await self.link(series_id=1, remote_id="2")
        relinked = await store.get_link(self.db, 1, MANGAUPDATES, series_id=1)
        self.assertEqual(relinked["remote_id"], "2")
        self.assertIsNone(relinked["last_pushed_volume"])

    async def test_signing_out_forgets_the_links_too(self):
        # A link is only meaningful against an account: kept, the series
        # page would go on saying it is tracked while nothing can be sent,
        # and signing in as somebody else would adopt these matches.
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        await store.delete_account(self.db, 1, MANGAUPDATES)
        await self.db.commit()
        self.assertIsNone(await store.get_link(self.db, 1, MANGAUPDATES, series_id=1))

    async def test_one_readers_link_is_not_anothers(self):
        await self.series()
        await self.link(series_id=1, user_id=1)
        self.assertIsNone(await store.get_link(self.db, 2, MANGAUPDATES, series_id=1))


class TheServiceClient(unittest.IsolatedAsyncioTestCase):
    """The wire format, against a transport that never leaves the process."""

    async def test_a_search_result_carries_what_tells_two_entries_apart(self):
        payload = {"results": [{"record": {
            "series_id": 12345, "title": "  Berserk  ", "year": "1989", "type": "Manga",
            "url": "https://www.mangaupdates.com/series/x",
            "image": {"url": {"thumb": "https://t/1.jpg"}},
        }}]}
        with mock_client(lambda request: httpx.Response(200, json=payload)):
            results = await mangaupdates.search("berserk")
        self.assertEqual(
            (results[0].remote_id, results[0].title, results[0].year, results[0].kind),
            ("12345", "Berserk", "1989", "Manga"),
        )

    async def test_a_result_is_cleaned_of_the_html_the_catalogue_answers_in(self):
        # MangaUpdates returns entity-escaped titles and a description with
        # tags in it. Rendered as-is, a title shows "&quot;" and a
        # description shows its markup.
        payload = {"results": [{"record": {
            "series_id": 1, "title": "Kaguya-sama &sim; Tensai-tachi &amp; Co &sim;",
            "description": "<b>Two</b> geniuses.&nbsp;Neither will confess.",
        }}]}
        with mock_client(lambda request: httpx.Response(200, json=payload)):
            result = (await mangaupdates.search("kaguya"))[0]
        self.assertEqual(result.title, "Kaguya-sama ∼ Tensai-tachi & Co ∼")
        self.assertEqual(result.description, "Two geniuses. Neither will confess.")

    async def test_a_long_description_is_cut_on_a_word_boundary(self):
        words = " ".join(["alpha"] * 200)
        payload = {"results": [{"record": {"series_id": 1, "title": "x", "description": words}}]}
        with mock_client(lambda request: httpx.Response(200, json=payload)):
            description = (await mangaupdates.search("x"))[0].description
        self.assertTrue(description.endswith("…"))
        self.assertLessEqual(len(description), models.DESCRIPTION_CHARS + 1)
        self.assertNotIn("alph…", description)

    async def test_an_entry_with_no_description_simply_has_none(self):
        payload = {"results": [{"record": {"series_id": 1, "title": "x"}}]}
        with mock_client(lambda request: httpx.Response(200, json=payload)):
            self.assertIsNone((await mangaupdates.search("x"))[0].description)

    async def test_no_results_is_an_empty_list_and_not_an_error(self):
        # The ordinary answer for a scanlation or a small novel, and the
        # panel says so rather than drawing a failure.
        with mock_client(lambda request: httpx.Response(200, json={"results": []})):
            self.assertEqual(await mangaupdates.search("nothing at all"), [])

    async def test_a_series_on_no_list_reads_as_none_rather_than_failing(self):
        with mock_client(lambda request: httpx.Response(404, json={})):
            self.assertIsNone(await mangaupdates.get_progress("t", "1"))

    async def test_a_refused_session_is_its_own_kind_of_failure(self):
        with mock_client(lambda request: httpx.Response(401, json={})):
            with self.assertRaises(models.TrackerAuthError):
                await mangaupdates.get_progress("t", "1")

    async def test_any_other_refusal_is_not(self):
        with mock_client(lambda request: httpx.Response(503, json={})):
            with self.assertRaises(models.TrackerError):
                await mangaupdates.get_progress("t", "1")

    async def test_an_unreachable_service_is_not_an_auth_failure(self):
        # Told apart because only one of the two is worth telling the
        # reader to sign in again about.
        def handler(request):
            raise httpx.ConnectError("no route")

        with mock_client(handler):
            with self.assertRaises(models.TrackerError):
                await mangaupdates.search("x")

    async def test_only_the_field_asked_for_is_sent(self):
        # A volume-numbered series has no chapter number to report, and
        # sending 0 for it would say the reader had read none.
        sent = []
        with mock_client(lambda request: (sent.append(request.content), httpx.Response(200, json={}))[1]):
            await mangaupdates.set_state("t", "1", volume=3)
        self.assertEqual(json.loads(sent[0])[0]["status"], {"volume": 3})

    async def test_a_login_returns_the_token_and_never_echoes_the_password(self):
        payload = {"context": {"session_token": "abc123", "username": "Reader"}}
        with mock_client(lambda request: httpx.Response(200, json=payload)):
            token, name = await mangaupdates.authenticate("reader", "hunter2")
        self.assertEqual((token, name), ("abc123", "Reader"))

    async def test_a_login_with_no_token_is_an_auth_failure(self):
        with mock_client(lambda request: httpx.Response(200, json={"context": {}})):
            with self.assertRaises(models.TrackerAuthError):
                await mangaupdates.authenticate("reader", "wrong")


class WhatCountsAsHavingReadIt(_Case):
    """Every path that makes a volume `completed` has to tell the tracker.

    The first version hooked only the mark-read BUTTON, and a reader who
    simply reached the last page -- which is the whole feature -- got
    nothing sent. Reported from a real install: a chapter read to the end,
    marked read on screen, and never on MangaUpdates.
    """

    async def push_paths(self):
        await self.series(kind="chapter")
        await self.item(1, 7.0)
        await self.connect()
        await self.link(series_id=1)

    async def test_the_reader_reaching_the_end_pushes(self):
        from app.reader.router import save_progress

        await self.push_paths()
        request = _JsonRequest({"current_page": 20, "progress_percent": 100, "status": "completed"})
        seen, handler = self.calls()
        with mock_client(handler):
            await save_progress(1, request, user={"id": 1}, db=self.db)
            await service.drain()
        update = [c for c in seen if c[1].endswith("/update")]
        self.assertEqual(len(update), 1)
        self.assertEqual(json.loads(update[0][2])[0]["status"], {"chapter": 7})

    async def test_an_ordinary_page_turn_does_not(self):
        # This endpoint is called on every page turn; only a completion is
        # news to a tracker.
        #
        # Something else is already read and never pushed, so a spurious
        # push here WOULD reach the service. Without that the push's own
        # "nothing read yet" guard answers first and this test passes
        # whether the completion check is there or not.
        from app.reader.router import save_progress

        await self.push_paths()
        await self.item(2, 3.0)
        await self.read(2)
        request = _JsonRequest({"current_page": 4, "progress_percent": 21, "status": "reading"})
        seen, handler = self.calls()
        with mock_client(handler):
            await save_progress(1, request, user={"id": 1}, db=self.db)
            await service.drain()
        self.assertEqual(seen, [])

    def calls(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append((request.method, request.url.path, request.content))
            if request.url.path.startswith("/v1/lists/series/") and request.method == "GET":
                return httpx.Response(200, json={"status": {"chapter": 0, "volume": 0}, "list_id": 0})
            return httpx.Response(200, json={})

        return seen, handler


class _JsonRequest:
    """The one thing save_progress wants from a Request."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class WhenTheServiceRefusesTheSignIn(unittest.IsolatedAsyncioTestCase):
    """MyAnimeList reports a refusal two ways, and BOTH were swallowed.

    A redirect back carrying `error=` and no code was read as "no callback
    here", so a failed sign-in looked like nothing happening; and a refused
    token exchange was reported as a generic "did not complete". Meanwhile
    MAL had said `invalid_client: Client authentication failed`, which
    names the actual fix.
    """

    def setUp(self):
        self.enterContext(patch.object(settings, "myanimelist_client_id", "fixture-client-id"))

    async def test_a_refused_exchange_repeats_what_the_service_said(self):
        payload = {"error": "invalid_client", "message": "Client authentication failed"}
        with mock_client(lambda request: httpx.Response(400, json=payload), myanimelist):
            with self.assertRaises(models.TrackerAuthError) as caught:
                await myanimelist.exchange_code("code", "verifier")
        self.assertIn("invalid_client", str(caught.exception))
        self.assertIn("Client authentication failed", str(caught.exception))

    async def test_a_401_is_reported_the_same_way_as_a_400(self):
        with mock_client(lambda request: httpx.Response(401, json={"error": "invalid_client"}),
                         myanimelist):
            with self.assertRaises(models.TrackerAuthError) as caught:
                await myanimelist.exchange_code("code", "verifier")
        self.assertIn("invalid_client", str(caught.exception))

    async def test_it_does_not_repeat_the_framing_the_page_adds(self):
        # The settings page renders "The service refused the sign-in: ..."
        # around this, and naming it here too read as "refused the sign-in:
        # MyAnimeList refused the sign-in: ..." (measured in a browser).
        payload = {"error": "invalid_client", "message": "Client authentication failed"}
        with mock_client(lambda request: httpx.Response(400, json=payload), myanimelist):
            with self.assertRaises(models.TrackerAuthError) as caught:
                await myanimelist.exchange_code("code", "verifier")
        self.assertNotIn("refused", str(caught.exception).lower())

    async def test_a_body_that_is_not_json_still_says_something(self):
        with mock_client(lambda request: httpx.Response(400, text="<html>nope</html>"), myanimelist):
            with self.assertRaises(models.TrackerAuthError) as caught:
                await myanimelist.exchange_code("code", "verifier")
        self.assertIn("400", str(caught.exception))


class TheClientSecret(unittest.IsolatedAsyncioTestCase):
    """MAL issues one for most app types and refuses the exchange without
    it -- which is what "connecting" failed with. An app registered with no
    secret has it empty, and sending an empty value is not the same as
    sending none."""

    def setUp(self):
        self.enterContext(patch.object(settings, "myanimelist_client_id", "fixture-client-id"))
        self._original = settings.myanimelist_client_secret
        self.addCleanup(
            lambda: setattr(settings, "myanimelist_client_secret", self._original))

    async def sent_form(self):
        seen = []

        def handler(request):
            seen.append(request.content.decode())
            return httpx.Response(200, json={"access_token": "t", "expires_in": 1})

        with mock_client(handler, myanimelist):
            await myanimelist.exchange_code("code", "verifier")
        # keep_blank_values, or an empty client_secret is dropped by the
        # parser and "sent as empty" is indistinguishable from "not sent".
        return urllib.parse.parse_qs(seen[0], keep_blank_values=True)

    async def test_it_is_sent_when_configured(self):
        settings.myanimelist_client_secret = "s3cret"
        self.assertEqual((await self.sent_form())["client_secret"], ["s3cret"])

    async def test_it_is_absent_rather_than_empty_when_not(self):
        settings.myanimelist_client_secret = ""
        form = await self.sent_form()
        self.assertNotIn("client_secret", form)
        self.assertEqual(form["client_id"], [settings.myanimelist_client_id])


class ConfiguringMyAnimeList(unittest.TestCase):
    """OAuth registration belongs to the installation, including under Compose."""

    def settings_with(self, **env):
        from app.config import Settings

        with patch.dict(os.environ, env, clear=False):
            return Settings()

    def test_client_id_requires_explicit_configuration(self):
        with patch.dict(os.environ, clear=False):
            os.environ.pop("APP_MYANIMELIST_CLIENT_ID", None)
            self.assertEqual(self.settings_with().myanimelist_client_id, "")
        for value in ("", "   "):
            with self.subTest(value=value):
                self.assertEqual(self.settings_with(APP_MYANIMELIST_CLIENT_ID=value).myanimelist_client_id, "")

    def test_redirect_requires_explicit_configuration(self):
        with patch.dict(os.environ, clear=False):
            os.environ.pop("APP_MYANIMELIST_REDIRECT_URI", None)
            self.assertEqual(self.settings_with().myanimelist_redirect_uri, "")
        for value in ("", "   "):
            with self.subTest(value=value):
                self.assertEqual(self.settings_with(
                    APP_MYANIMELIST_REDIRECT_URI=value).myanimelist_redirect_uri, "")

    def test_a_real_value_still_wins(self):
        s = self.settings_with(APP_MYANIMELIST_CLIENT_ID="mine",
                               APP_MYANIMELIST_REDIRECT_URI="https://x/cb")
        self.assertEqual(s.myanimelist_client_id, "mine")
        self.assertEqual(s.myanimelist_redirect_uri, "https://x/cb")

    def test_the_secret_has_no_default_to_fall_back_to(self):
        # An app registered without a secret legitimately has it empty.
        self.assertEqual(self.settings_with(APP_MYANIMELIST_CLIENT_SECRET="").myanimelist_client_secret, "")


class StartingMyAnimeListOAuth(_Case):
    def request(self):
        return Request({"type": "http", "method": "POST", "path": "/settings/tracking/myanimelist/connect",
                        "headers": [(b"host", b"untrusted.example")]})

    async def test_missing_redirect_does_not_start_oauth(self):
        with patch.object(settings, "myanimelist_redirect_uri", ""):
            response = await router.connect_myanimelist(self.request(), user={"id": 1})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/settings?tracker_error=not-configured#tracking")
        self.assertEqual((await (await self.db.execute(
            "SELECT COUNT(*) FROM tracker_oauth_pending")).fetchone())[0], 0)

    async def test_missing_client_id_does_not_start_oauth(self):
        with patch.object(settings, "myanimelist_client_id", ""), \
                patch.object(settings, "myanimelist_redirect_uri", "https://reader.example/cb"):
            response = await router.connect_myanimelist(self.request(), user={"id": 1})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/settings?tracker_error=not-configured#tracking")
        self.assertEqual((await (await self.db.execute(
            "SELECT COUNT(*) FROM tracker_oauth_pending")).fetchone())[0], 0)

    async def test_missing_client_id_prevents_exchange_and_refresh_requests(self):
        seen = []
        with patch.object(settings, "myanimelist_client_id", ""), \
                mock_client(lambda request: (seen.append(request), httpx.Response(200, json={"access_token": "t"}))[1], myanimelist):
            for method, args in ((myanimelist.exchange_code, ("code", "verifier")),
                                 (myanimelist.refresh, ("refresh-token",))):
                with self.subTest(method=method.__name__), self.assertRaisesRegex(
                        models.TrackerAuthError, "APP_MYANIMELIST_CLIENT_ID"):
                    await method(*args)
        self.assertEqual(seen, [])

    async def test_configured_redirect_is_used_and_state_is_saved(self):
        uri = "https://reader.example/"
        with patch.object(settings, "myanimelist_redirect_uri", uri):
            response = await router.connect_myanimelist(self.request(), user={"id": 1})
        query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
        self.assertEqual(query["client_id"], ["fixture-client-id"])
        self.assertEqual(query["redirect_uri"], [uri])
        self.assertEqual(await store.take_pending_oauth(
            self.db, 1, MYANIMELIST, query["state"][0]), query["code_challenge"][0])

    async def test_token_exchange_uses_the_same_configured_redirect(self):
        forms = []
        def handler(request):
            forms.append(urllib.parse.parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": "fixture"})
        with patch.object(settings, "myanimelist_redirect_uri", "https://reader.example/cb"), \
                mock_client(handler, myanimelist):
            await myanimelist.exchange_code("code", "verifier")
        self.assertEqual(forms[0]["redirect_uri"], ["https://reader.example/cb"])


class TheUpgradeFromOneColumnToTwo(unittest.IsolatedAsyncioTestCase):
    """`tracker_links.last_pushed_number` held whichever unit the series
    happened to be numbered in. That stopped being answerable once a web
    novel began reporting a volume AND a chapter, so it became two columns
    and a one-time migration moved each row into the field it described.

    Driven through the REAL upgrade path -- a database carrying the old
    shape, then `init_db()` -- rather than by calling the migration
    directly, because what has to hold is that a running install picks this
    up on its next start.
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await self.build_old_database()
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)

    async def _close(self):
        await self.db.close()

    async def build_old_database(self):
        """The schema as it shipped, at the version that shipped it."""
        db = await get_connection()
        await db.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT);
            CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT, media_type TEXT);
            CREATE TABLE series (id INTEGER PRIMARY KEY, library_id INTEGER, name TEXT,
                                 sort_name TEXT, folder_path TEXT, number_kind TEXT);
            CREATE TABLE novels (id INTEGER PRIMARY KEY, title TEXT, source_url TEXT);
            CREATE TABLE tracker_links (
                id INTEGER PRIMARY KEY, user_id INTEGER, tracker TEXT,
                series_id INTEGER, novel_id INTEGER, remote_id TEXT,
                remote_title TEXT, remote_url TEXT,
                last_pushed_number REAL, last_pushed_at TEXT, last_error TEXT,
                created_at TEXT);
            INSERT INTO users VALUES (1, 'u', 'x');
            INSERT INTO series VALUES (1, 1, 'By volume', 'a', '/a', 'volume');
            INSERT INTO series VALUES (2, 1, 'By chapter', 'b', '/b', 'chapter');
            INSERT INTO series VALUES (3, 1, 'No numbers', 'c', '/c', NULL);
            INSERT INTO novels VALUES (1, 'A web novel', 'https://x/1');
            INSERT INTO tracker_links (id, user_id, tracker, series_id, remote_id, remote_title,
                                       remote_url, last_pushed_number)
                 VALUES (1, 1, 'mangaupdates', 1, '1', 't', 'u', 7);
            INSERT INTO tracker_links (id, user_id, tracker, series_id, remote_id, remote_title,
                                       remote_url, last_pushed_number)
                 VALUES (2, 1, 'mangaupdates', 2, '2', 't', 'u', 120);
            INSERT INTO tracker_links (id, user_id, tracker, series_id, remote_id, remote_title,
                                       remote_url, last_pushed_number)
                 VALUES (3, 1, 'mangaupdates', 3, '3', 't', 'u', 4);
            INSERT INTO tracker_links (id, user_id, tracker, novel_id, remote_id, remote_title,
                                       remote_url, last_pushed_number)
                 VALUES (4, 1, 'mangaupdates', 1, '4', 't', 'u', 41);
            INSERT INTO tracker_links (id, user_id, tracker, series_id, remote_id, remote_title,
                                       remote_url)
                 VALUES (5, 1, 'mangaupdates', 1, '5', 't', 'u');
            PRAGMA user_version = 3;
            """
        )
        await db.commit()
        await db.close()

    async def row(self, link_id):
        return await (await self.db.execute(
            "SELECT last_pushed_chapter, last_pushed_volume FROM tracker_links WHERE id = ?",
            (link_id,))).fetchone()

    async def test_a_volume_numbered_series_had_been_holding_volumes(self):
        row = await self.row(1)
        self.assertEqual((row["last_pushed_volume"], row["last_pushed_chapter"]), (7, None))

    async def test_a_chapter_numbered_series_had_been_holding_chapters(self):
        row = await self.row(2)
        self.assertEqual((row["last_pushed_chapter"], row["last_pushed_volume"]), (120, None))

    async def test_a_series_with_no_kind_is_read_as_chapters(self):
        # What the code did before this column existed, and what
        # series_progress still does for a NULL number_kind.
        row = await self.row(3)
        self.assertEqual((row["last_pushed_chapter"], row["last_pushed_volume"]), (4, None))

    async def test_a_novels_rows_were_always_chapters(self):
        row = await self.row(4)
        self.assertEqual((row["last_pushed_chapter"], row["last_pushed_volume"]), (41, None))

    async def test_a_link_that_had_never_pushed_stays_empty(self):
        row = await self.row(5)
        self.assertEqual((row["last_pushed_chapter"], row["last_pushed_volume"]), (None, None))

    async def test_it_does_not_run_twice(self):
        # The reason it is a one-time migration and not a startup backfill:
        # re-running it after the new columns are being maintained would
        # push a link back to whatever the old column still holds.
        await self.db.execute(
            "UPDATE tracker_links SET last_pushed_volume = 99 WHERE id = 1")
        await self.db.commit()
        await init_db()
        row = await self.row(1)
        self.assertEqual(row["last_pushed_volume"], 99)


class TheMyAnimeListClient(unittest.IsolatedAsyncioTestCase):
    """Its wire format, which differs from MangaUpdates' in ways that
    matter: OAuth with PKCE, and a list-status call that REPLACES."""

    def setUp(self):
        self.enterContext(patch.object(settings, "myanimelist_client_id", "fixture-client-id"))

    def mal(self, handler):
        return mock_client(handler, myanimelist)

    def test_the_authorize_url_carries_the_verifier_as_the_challenge(self):
        # MAL supports only the `plain` challenge method, so the challenge
        # IS the verifier -- and the method parameter is left off, since
        # `plain` is what it defaults to and all it accepts.
        verifier = myanimelist.new_verifier()
        url = myanimelist.authorize_url("st4te", verifier)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["code_challenge"], [verifier])
        self.assertEqual(query["state"], ["st4te"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertNotIn("code_challenge_method", query)

    def test_a_verifier_is_long_enough_for_mal_to_accept(self):
        # MAL requires 43-128 characters and refuses anything else.
        for _ in range(20):
            self.assertTrue(43 <= len(myanimelist.new_verifier()) <= 128)

    def test_the_redirect_uri_is_the_configured_one(self):
        # It has to match a URI registered on the MAL side EXACTLY, which
        # is why it is configured and not derived from the request.
        with patch.object(settings, "myanimelist_redirect_uri", "https://reader.example/callback"):
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(myanimelist.authorize_url("s", "v")).query)
        self.assertEqual(query["redirect_uri"], ["https://reader.example/callback"])

    async def test_a_refused_code_is_an_auth_failure_though_mal_answers_400(self):
        # Not a 401, and still something only the reader can fix -- so it
        # has to be told apart from the service having a bad day.
        with self.mal(lambda request: httpx.Response(400, json={"error": "invalid_request"})):
            with self.assertRaises(models.TrackerAuthError):
                await myanimelist.exchange_code("bad", "verifier")

    async def test_an_entry_not_on_the_list_reads_as_none(self):
        # MAL answers 200 with no my_list_status, where MangaUpdates 404s.
        with self.mal(lambda request: httpx.Response(200, json={"id": 1, "num_chapters": 100})):
            self.assertIsNone(await myanimelist.get_progress("t", "1"))

    async def test_it_reads_both_counts_from_the_list_status(self):
        payload = {"id": 1, "my_list_status": {
            "num_chapters_read": 41, "num_volumes_read": 3, "status": "reading", "score": 8}}
        with self.mal(lambda request: httpx.Response(200, json=payload)):
            remote = await myanimelist.get_progress("t", "1")
        self.assertEqual((remote.chapter, remote.volume), (41.0, 3.0))
        self.assertEqual(remote.raw["status"], "reading")

    async def test_an_update_re_sends_the_status_and_score_it_is_not_moving(self):
        # PUT my_list_status REPLACES the record. Omit these and an entry
        # the reader had marked on-hold comes back plan_to_read with its
        # rating wiped -- the tracker damaging data this app does not own.
        sent = []

        def handler(request):
            sent.append(request.content.decode())
            return httpx.Response(200, json={})

        remote = models.RemoteProgress(
            chapter=1.0, volume=0.0, raw={"status": "on_hold", "score": 9})
        with self.mal(handler):
            await myanimelist.set_state("t", "1", chapter=41, remote=remote)
        form = urllib.parse.parse_qs(sent[0])
        self.assertEqual(form["status"], ["on_hold"])
        self.assertEqual(form["score"], ["9"])
        self.assertEqual(form["num_chapters_read"], ["41"])
        self.assertNotIn("num_volumes_read", form)

    async def test_an_entry_with_no_record_yet_starts_as_reading(self):
        sent = []

        def handler(request):
            sent.append(request.content.decode())
            return httpx.Response(200, json={})

        with self.mal(handler):
            await myanimelist.set_state("t", "1", chapter=1, remote=None)
        self.assertEqual(urllib.parse.parse_qs(sent[0])["status"], ["reading"])

    async def test_a_search_result_carries_what_tells_two_entries_apart(self):
        payload = {"data": [{"node": {
            "id": 2, "title": "Berserk", "media_type": "manga", "start_date": "1989-08-25",
            "synopsis": "<b>Guts</b> is a&nbsp;mercenary.",
            "main_picture": {"medium": "https://m/2.jpg"},
        }}]}
        with self.mal(lambda request: httpx.Response(200, json=payload)):
            result = (await myanimelist.search("berserk", token="t"))[0]
        self.assertEqual(
            (result.remote_id, result.title, result.year, result.kind, result.cover_url),
            ("2", "Berserk", "1989", "manga", "https://m/2.jpg"))
        self.assertEqual(result.description, "Guts is a mercenary.")
        self.assertEqual(result.url, "https://myanimelist.net/manga/2")

    async def test_searching_without_a_token_is_refused_before_a_request(self):
        # MAL has no anonymous catalogue read, unlike MangaUpdates.
        seen = []
        with self.mal(lambda request: (seen.append(1), httpx.Response(200, json={}))[1]):
            with self.assertRaises(models.TrackerAuthError):
                await myanimelist.search("x", token=None)
        self.assertEqual(seen, [])


class TheOAuthHandshake(_Case):
    async def test_the_verifier_is_returned_only_for_the_matching_state(self):
        await store.save_pending_oauth(
            self.db, 1, MYANIMELIST, state="right", verifier="v3rifi3r")
        await self.db.commit()
        self.assertIsNone(await store.take_pending_oauth(self.db, 1, MYANIMELIST, "wrong"))

    async def test_it_is_consumed_even_when_the_state_did_not_match(self):
        # A handshake gets exactly one callback; leaving the row would let
        # a replayed redirect be redeemed on a second try.
        await store.save_pending_oauth(self.db, 1, MYANIMELIST, state="right", verifier="v")
        await self.db.commit()
        await store.take_pending_oauth(self.db, 1, MYANIMELIST, "wrong")
        self.assertIsNone(await store.take_pending_oauth(self.db, 1, MYANIMELIST, "right"))

    async def test_a_matching_state_hands_back_the_verifier_once(self):
        await store.save_pending_oauth(self.db, 1, MYANIMELIST, state="s", verifier="v")
        await self.db.commit()
        self.assertEqual(await store.take_pending_oauth(self.db, 1, MYANIMELIST, "s"), "v")
        self.assertIsNone(await store.take_pending_oauth(self.db, 1, MYANIMELIST, "s"))

    async def test_a_callback_with_no_handshake_started_has_no_verifier(self):
        self.assertIsNone(await store.take_pending_oauth(self.db, 1, MYANIMELIST, "anything"))


class TheMyAnimeListSession(_Case):
    """Refreshing, which MangaUpdates has no equivalent of."""

    async def connect_mal(self, *, expires_at):
        await store.save_account(
            self.db, 1, MYANIMELIST, access_token="old", refresh_token="r3fresh",
            username="reader", expires_at=expires_at)
        await self.db.commit()

    def calls(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append((request.method, request.url.path))
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={
                    "access_token": "fresh", "refresh_token": "r3fresh2", "expires_in": 2592000})
            if "/my_list_status" in request.url.path:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"id": 1, "my_list_status": {
                "num_chapters_read": 0, "num_volumes_read": 0, "status": "reading", "score": 0}})

        return seen, handler

    async def test_an_expired_token_is_refreshed_before_the_call_not_after_a_401(self):
        # MAL tokens last about a month, so an expiry is the ordinary case
        # rather than an error; retrying every request once would double
        # the traffic of a tracker that is working fine.
        await self.series()
        await self.item(1, 4.0)
        await self.read(1)
        await self.connect_mal(expires_at="2000-01-01 00:00:00")
        await store.save_link(
            self.db, 1, MYANIMELIST, series_id=1,
            remote_id="9", remote_title="R", remote_url="u")
        await self.db.commit()
        seen, handler = self.calls()
        with mock_client(handler, myanimelist):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        self.assertIn(("POST", "/v1/oauth2/token"), seen)
        account = await store.get_account(self.db, 1, MYANIMELIST)
        self.assertEqual(account["access_token"], "fresh")
        self.assertEqual(account["refresh_token"], "r3fresh2")

    async def test_a_token_still_valid_is_not_refreshed(self):
        await self.series()
        await self.item(1, 4.0)
        await self.read(1)
        await self.connect_mal(expires_at="2999-01-01 00:00:00")
        await store.save_link(
            self.db, 1, MYANIMELIST, series_id=1,
            remote_id="9", remote_title="R", remote_url="u")
        await self.db.commit()
        seen, handler = self.calls()
        with mock_client(handler, myanimelist):
            await service.push(1, series_id=1)
        self.assertNotIn(("POST", "/v1/oauth2/token"), seen)

    async def test_the_expiry_is_stored_a_minute_early(self):
        # A token that expires while a request is in flight fails, and one
        # refresh a minute early costs nothing.
        stamp = service.expiry_for(3600)
        self.assertIsNotNone(stamp)
        gap = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc) - datetime.now(timezone.utc)
        # Comfortably BELOW the nominal hour, not merely under it: with no
        # margin the gap lands a fraction of a second short of 3600, which
        # a plain "less than 3600" would happily accept.
        self.assertLess(gap.total_seconds(), 3600 - 30)
        self.assertGreater(gap.total_seconds(), 3600 - 90)

    async def test_a_service_with_no_refresh_token_is_used_as_it_stands(self):
        self.assertIsNone(service.expiry_for(None))

    async def search_panel(self):
        return await router.search_tracker(
            MYANIMELIST, WhichKindOfThingIsBeingLinked._request(), q="Berserk",
            series_id=1, novel_id=None, all_types=False, user={"id": 1}, db=self.db)

    async def test_search_refreshes_an_expired_session_and_saves_rotated_tokens(self):
        await self.series()
        await self.connect_mal(expires_at="2000-01-01 00:00:00")
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if request.url.path.endswith("/token"):
                self.assertEqual(urllib.parse.parse_qs(request.content.decode())["refresh_token"],
                                 ["r3fresh"])
                return httpx.Response(200, json={"access_token": "fresh",
                    "refresh_token": "rotated", "expires_in": 3600})
            self.assertEqual(request.headers["Authorization"], "Bearer fresh")
            return httpx.Response(200, json={"data": [{"node": {"id": 9, "title": "Berserk"}}]})

        with mock_client(handler, myanimelist):
            response = await self.search_panel()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tracker_results"][0].title, "Berserk")
        self.assertEqual(seen, ["/v1/oauth2/token", "/v2/manga"])
        account = await store.get_account(self.db, 1, MYANIMELIST)
        self.assertEqual(account["refresh_token"], "rotated")
        self.assertGreater(account["expires_at"], service._now())

    async def test_panel_recovers_when_mal_rejects_a_nominally_valid_token(self):
        await self.series()
        await self.connect_mal(expires_at="2999-01-01 00:00:00")
        await store.save_link(self.db, 1, MYANIMELIST, series_id=1,
                              remote_id="9", remote_title="R", remote_url="u")
        await self.db.commit()
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
            if request.headers["Authorization"] == "Bearer old":
                return httpx.Response(401)
            return httpx.Response(200, json={"my_list_status": {
                "num_chapters_read": 12, "status": "reading"}})

        with mock_client(handler, myanimelist):
            state = await service.remote_state(1, MYANIMELIST, series_id=1)
        self.assertIsNone(state["error"])
        self.assertEqual(state["state"].chapter, 12)
        self.assertEqual(seen, ["/v2/manga/9", "/v1/oauth2/token", "/v2/manga/9"])

    async def test_search_shows_a_refused_refresh_instead_of_raising_a_500(self):
        await self.series()
        await self.connect_mal(expires_at="2000-01-01 00:00:00")
        with mock_client(lambda r: httpx.Response(400, json={"error": "invalid_grant"}), myanimelist):
            response = await self.search_panel()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tracker_message"], "invalid_grant")
        self.assertEqual(response.context["tracker_results"], [])
        account = await store.get_account(self.db, 1, MYANIMELIST)
        self.assertEqual(account["refresh_token"], "r3fresh")

    async def test_concurrent_requests_refresh_once_and_reuse_the_saved_session(self):
        await self.connect_mal(expires_at="2000-01-01 00:00:00")
        account = await store.get_account(self.db, 1, MYANIMELIST)
        seen = []

        async def handler(request):
            seen.append(request.url.path)
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"access_token": "fresh",
                "refresh_token": "rotated", "expires_in": 3600})

        with mock_client(handler, myanimelist):
            tokens = await asyncio.gather(*(service._usable_token(myanimelist, account, 1)
                                           for _ in range(3)))
            self.assertEqual(await service._usable_token(myanimelist, account, 1,
                rejected_token="old"), "fresh")
        self.assertEqual(tokens, ["fresh"] * 3)
        self.assertEqual(seen, ["/v1/oauth2/token"])

    async def test_search_stops_after_one_retry_when_the_replacement_is_refused(self):
        await self.series()
        await self.connect_mal(expires_at="2999-01-01 00:00:00")
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
            return httpx.Response(401)

        with mock_client(handler, myanimelist):
            response = await self.search_panel()
        self.assertEqual(response.context["tracker_message"], "MyAnimeList rejected the session")
        self.assertEqual(seen, ["/v2/manga", "/v1/oauth2/token", "/v2/manga"])

    async def test_a_403_does_not_trigger_refresh(self):
        await self.series()
        await self.connect_mal(expires_at="2999-01-01 00:00:00")
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(403)

        with mock_client(handler, myanimelist):
            response = await self.search_panel()
        self.assertEqual(response.context["tracker_message"], "MyAnimeList rejected the session")
        self.assertEqual(seen, ["/v2/manga"])

    async def test_search_without_an_account_renders_the_authentication_error(self):
        await self.series()
        response = await self.search_panel()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tracker_message"],
                         "MyAnimeList search needs a connected account")



class BothTrackersAtOnce(_Case):
    """A reader can keep the same series on both, and the two are
    independent all the way down."""

    def calls(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append((str(request.url.host), request.method, request.url.path))
            if request.url.path.startswith("/v1/lists/series/") and request.method == "GET":
                return httpx.Response(200, json={"status": {"chapter": 0, "volume": 0}, "list_id": 0})
            if request.url.host == "api.myanimelist.net" and request.method == "GET":
                return httpx.Response(200, json={"id": 1, "my_list_status": {
                    "num_chapters_read": 0, "num_volumes_read": 0, "status": "reading", "score": 0}})
            return httpx.Response(200, json={})

        return seen, handler

    async def link_both(self):
        await self.series()
        await self.item(1, 4.0)
        await self.read(1)
        await self.connect()
        await store.save_account(
            self.db, 1, MYANIMELIST, access_token="mal", refresh_token=None,
            username="reader", expires_at=None)
        for tracker in (MANGAUPDATES, MYANIMELIST):
            await store.save_link(
                self.db, 1, tracker, series_id=1,
                remote_id="9", remote_title="R", remote_url="u")
        await self.db.commit()

    async def test_one_push_reaches_both(self):
        await self.link_both()
        seen, handler = self.calls()
        with mock_client(handler), mock_client(handler, myanimelist):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        hosts = {host for host, _, _ in seen}
        self.assertEqual(hosts, {"api.mangaupdates.com", "api.myanimelist.net"})

    async def test_one_service_failing_does_not_stop_the_other(self):
        # The reason the loop catches per link: a signed-out or broken
        # service must not cost the reader the one that works.
        await self.link_both()
        seen, handler = self.calls()
        with mock_client(lambda request: httpx.Response(500, json={})), \
                mock_client(handler, myanimelist):
            self.assertEqual(await service.push(1, series_id=1), "failed")
        self.assertTrue(any(host == "api.myanimelist.net" for host, _, _ in seen))
        mal_link = await store.get_link(self.db, 1, MYANIMELIST, series_id=1)
        mu_link = await store.get_link(self.db, 1, MANGAUPDATES, series_id=1)
        self.assertEqual(mal_link["last_pushed_volume"], 4.0)
        self.assertIsNone(mu_link["last_pushed_volume"])
        self.assertIsNotNone(mu_link["last_error"])

    async def test_unlinking_one_leaves_the_other(self):
        await self.link_both()
        await store.delete_link(self.db, 1, MANGAUPDATES, series_id=1)
        await self.db.commit()
        links = await store.links_for(self.db, 1, series_id=1)
        self.assertEqual([row["tracker"] for row in links], [MYANIMELIST])


class WhichKindOfThingIsBeingLinked(_Case):
    """A catalogue indexes a manga and the light novel it came from under
    the same title, and linking the wrong one ties a shelf to an entry no
    push will ever move. The shelf itself is what says which is wanted.
    """

    @staticmethod
    def _request():
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/tracking/mangaupdates/search", "raw_path": b"/", "query_string": b"",
            "root_path": "", "headers": [(b"hx-request", b"true")],
            "client": ("test", 1), "server": ("test", 80),
        }
        return Request(scope)

    def test_the_word_a_catalogue_uses_lands_in_one_of_two_families(self):
        # Both services' vocabularies, as they actually answer. The rule is
        # "contains novel", not a list, because neither service asked
        # anyone before adding a word.
        for word in ("Manga", "manga", "Manhwa", "manhua", "Doujinshi", "one_shot",
                     "Artbook", "oel", "Manhua"):
            self.assertEqual(models.kind_family(word), models.COMIC, word)
        for word in ("Novel", "novel", "light_novel", "Light Novel"):
            self.assertEqual(models.kind_family(word), models.NOVEL, word)

    def test_an_entry_that_does_not_say_is_not_evidence_against_itself(self):
        # None is kept by every filter: dropping the one entry that never
        # declared a type would hide it with nothing on screen saying why.
        for missing in (None, "", 0):
            self.assertIsNone(models.kind_family(missing))

    async def test_the_family_comes_from_the_library_for_a_series(self):
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (2,'B','/b','ebook')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (3,'X','/x','mixed')")
        await self.db.commit()
        await self.series(1)
        for series_id, library_id, expected in ((2, 2, models.NOVEL), (3, 3, None)):
            await self.db.execute(
                "INSERT INTO series (id, library_id, name, sort_name, folder_path) "
                "VALUES (?, ?, 'S', 's', 's')", (series_id, library_id))
        await self.db.commit()

        self.assertEqual(await router._local_kind(self.db, 1, None), models.COMIC)
        self.assertEqual(await router._local_kind(self.db, 2, None), models.NOVEL)
        # 'mixed' holds both, so there is nothing to filter by and the
        # panel offers no filter rather than a guessed one.
        self.assertIsNone(await router._local_kind(self.db, 3, None))

    async def test_a_web_novel_is_prose_by_construction(self):
        await self.novel()
        self.assertEqual(await router._local_kind(self.db, None, 1), models.NOVEL)

    def _catalogue(self):
        """A mixed answer, which is what a real search returns."""
        rows = [
            {"series_id": 1, "title": "A", "type": "Manga"},
            {"series_id": 2, "title": "A (Novel)", "type": "Novel"},
            {"series_id": 3, "title": "A Untyped"},
        ]
        return lambda request: httpx.Response(200, json={"results": [{"record": r} for r in rows]})

    async def _search(self, *, series_id=None, novel_id=None, all_types=False):
        with mock_client(self._catalogue()):
            response = await router.search_tracker(
                MANGAUPDATES, self._request(), q="A", all_types=all_types,
                series_id=series_id, novel_id=novel_id, user={"id": 1}, db=self.db)
        return response.context

    async def test_a_comic_shelf_is_not_offered_the_novel(self):
        await self.series()
        await self.connect()
        context = await self._search(series_id=1)
        self.assertEqual([r.title for r in context["tracker_results"]], ["A", "A Untyped"])
        # Counted, not implied: "nothing matches" and "everything there was
        # of the other kind" look identical on screen otherwise.
        self.assertEqual(context["tracker_filtered_out"], 1)
        self.assertEqual(context["tracker_kind"], models.COMIC)

    async def test_a_novel_shelf_is_not_offered_the_manga(self):
        await self.novel()
        await self.connect()
        context = await self._search(novel_id=1)
        self.assertEqual([r.title for r in context["tracker_results"]], ["A (Novel)", "A Untyped"])
        self.assertEqual(context["tracker_filtered_out"], 1)

    async def test_asking_for_every_type_drops_nothing(self):
        # Which is what the box did before the filter existed: the
        # catalogue's opinion about a type is sometimes wrong, and this is
        # the way back out.
        await self.series()
        await self.connect()
        context = await self._search(series_id=1, all_types=True)
        self.assertEqual(len(context["tracker_results"]), 3)
        self.assertEqual(context["tracker_filtered_out"], 0)
        self.assertTrue(context["tracker_all_types"])

    async def test_a_mixed_library_filters_nothing(self):
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (2,'X','/x','mixed')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path) "
            "VALUES (7, 2, 'S', 's', 's')")
        await self.db.commit()
        await self.connect()
        context = await self._search(series_id=7)
        self.assertEqual(len(context["tracker_results"]), 3)
        self.assertIsNone(context["tracker_kind"])


class WhatArrivesWithTheLink(_Case):
    """A link is not only an address. The entry already knows how far the
    reader got and what the series is about, and both of those are answers
    this app could not produce on its own."""

    def _service(self, *, volume=None, chapter=None, description="From the catalogue"):
        def handler(request: httpx.Request):
            if request.url.path.startswith("/v1/lists/series/"):
                if volume is None and chapter is None:
                    return httpx.Response(404, json={})
                return httpx.Response(200, json={
                    "status": {"chapter": chapter or 0, "volume": volume or 0}, "list_id": 0})
            if request.url.path.startswith("/v1/series/"):
                return httpx.Response(200, json={"description": description})
            return httpx.Response(200, json={})
        return handler

    async def _shelf(self, kind="volume", numbers=(1.0, 2.0, 3.0)):
        await self.series(kind=kind)
        for index, number in enumerate(numbers, start=1):
            await self.item(index, number)
        await self.connect()
        await self.link(series_id=1, remote_id="4242")

    async def _read_items(self):
        rows = await (await self.db.execute(
            "SELECT item_id FROM reading_progress WHERE user_id = 1 AND status = 'completed'"
        )).fetchall()
        return sorted(row["item_id"] for row in rows)

    async def test_a_shelf_behind_the_site_is_caught_up(self):
        # The one that matters: the push refuses to send a number lower
        # than the service's, so a shelf left behind never speaks again.
        await self._shelf()
        with mock_client(self._service(volume=2)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 2)
        self.assertEqual(await self._read_items(), [1, 2])

    async def test_it_never_walks_the_shelf_backwards(self):
        # Read further here than the site knows: nothing is unmarked, and
        # nothing is marked either -- what happens next is a push.
        await self._shelf()
        for item_id in (1, 2, 3):
            await self.read(item_id)
        with mock_client(self._service(volume=1)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 0)
        self.assertEqual(await self._read_items(), [1, 2, 3])

    async def test_a_lower_number_cannot_fill_in_what_was_skipped(self):
        # The same rule stated where it can actually be seen. How far the
        # reader has got is a MAX, not a count: with only volume 3 read,
        # this shelf is AT 3, and a site that says 1 is behind it. Marking
        # volume 1 "because the site said 1" would invent reading that
        # never happened -- and the earlier test cannot see that mistake,
        # because there everything below the number was already read.
        await self._shelf()
        await self.read(3)
        with mock_client(self._service(volume=1)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 0)
        self.assertEqual(await self._read_items(), [3])

    async def test_an_entry_on_no_list_marks_nothing(self):
        await self._shelf()
        with mock_client(self._service()):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 0)
        self.assertEqual(await self._read_items(), [])

    async def test_a_number_in_the_unit_this_shelf_does_not_count_in_is_ignored(self):
        # A volume-numbered shelf says nothing about chapters, and a
        # service's chapter count is not a volume it could adopt.
        await self._shelf()
        with mock_client(self._service(chapter=40)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 0)
        self.assertEqual(await self._read_items(), [])

    async def test_a_volume_count_is_not_adopted_by_a_chapter_numbered_shelf(self):
        # The mirror of the test above, and the one that was wrong. A
        # chapter-numbered shelf must ignore the service's VOLUME the same
        # way a volume-numbered one ignores its chapter -- and with
        # nothing read yet it did not, because the unit was inferred from
        # which local field held a number and NEITHER does until something
        # is marked read. Reported from a real install: a newly added
        # series, linked, came back with its first chapter marked read and
        # that number pushed straight back to the service.
        await self._shelf(kind="chapter")
        with mock_client(self._service(volume=1)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 0)
        self.assertEqual(await self._read_items(), [])

    async def test_the_unit_is_the_shelfs_even_when_nothing_has_been_read(self):
        # Same mistake, seen from the size of the damage rather than its
        # existence: a shelf at volume 12 of a long-running series would
        # have had twelve chapters marked read, not one.
        await self._shelf(kind="chapter", numbers=(1.0, 2.0, 3.0))
        with mock_client(self._service(volume=3, chapter=None)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 0)
        self.assertEqual(await self._read_items(), [])

    async def test_a_service_saying_zero_does_not_mark_a_chapter_zero(self):
        # Zero is how both services say "nothing read", and a prologue
        # numbered 0 is ordinary in manga -- so the two meet, and without
        # the guard an entry nobody has touched would mark it read.
        await self._shelf(kind="chapter", numbers=(0.0, 1.0, 2.0))
        with mock_client(self._service(chapter=0, volume=0)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 0)
        self.assertEqual(await self._read_items(), [])

    async def test_a_chapter_numbered_shelf_still_adopts_a_chapter(self):
        # The other half: fixing the unit must not stop the feature
        # working. Nothing read here either, which is the state the bug
        # lived in.
        await self._shelf(kind="chapter")
        with mock_client(self._service(chapter=2)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 2)
        self.assertEqual(await self._read_items(), [1, 2])

    async def test_a_volume_numbered_shelf_still_adopts_a_volume(self):
        await self._shelf(kind="volume")
        with mock_client(self._service(volume=2)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result["marked"], 2)
        self.assertEqual(await self._read_items(), [1, 2])

    async def test_an_already_finished_volume_keeps_when_it_was_finished(self):
        # completed_at and last_read_at are read by the statistics and by
        # the home page's "Reading" order. Re-stamping them would say a
        # book finished in April was finished the moment a link was made.
        await self._shelf()
        await self.db.execute(
            "UPDATE reading_progress SET completed_at = '2020-01-01 00:00:00', "
            "last_read_at = '2020-01-01 00:00:00' WHERE item_id = 1")
        await self.read(1)
        await self.db.execute(
            "UPDATE reading_progress SET completed_at = '2020-01-01 00:00:00', "
            "last_read_at = '2020-01-01 00:00:00' WHERE item_id = 1")
        await self.db.commit()
        with mock_client(self._service(volume=3)):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        row = await (await self.db.execute(
            "SELECT completed_at, last_read_at FROM reading_progress WHERE item_id = 1")).fetchone()
        self.assertEqual(row["completed_at"], "2020-01-01 00:00:00")
        self.assertEqual(row["last_read_at"], "2020-01-01 00:00:00")
        # And the two it really did mark are counted, not all three.
        self.assertEqual(result["marked"], 2)

    async def test_a_web_novel_is_caught_up_in_both_units(self):
        # "vol 3 ch 41" is how one is cited, and the site can be ahead on
        # either number.
        await self.novel(chapters=(1.0, 2.0, 3.0))
        await self.connect()
        await self.link(novel_id=1, remote_id="4242")
        with mock_client(self._service(chapter=2)):
            result = await service.catch_up(1, MANGAUPDATES, novel_id=1)
        rows = await (await self.db.execute(
            "SELECT chapter_id FROM novel_read_chapters WHERE user_id = 1")).fetchall()
        self.assertEqual(result["marked"], 2)
        self.assertEqual(sorted(r["chapter_id"] for r in rows), [101, 102])

    async def test_a_finished_volume_on_the_site_finishes_its_chapters_here(self):
        # What "volume 1 read" means locally, and what store.novel_progress
        # reads back out to decide the volume it reports.
        await self.novel(chapters=(1.0, 2.0, 3.0))
        await self.connect()
        await self.link(novel_id=1, remote_id="4242")
        with mock_client(self._service(volume=1)):
            result = await service.catch_up(1, MANGAUPDATES, novel_id=1)
        self.assertEqual(result["marked"], 3)

    async def test_the_description_comes_over(self):
        await self._shelf()
        with mock_client(self._service(description="<b>A</b> tagged&nbsp;description")):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        row = await (await self.db.execute(
            "SELECT summary, source_summary FROM series WHERE id = 1")).fetchone()
        self.assertTrue(result["described"])
        # Stripped and unescaped on the way in, like every other text
        # these catalogues answer with.
        self.assertEqual(row["summary"], "A tagged description")
        self.assertEqual(row["source_summary"], "A tagged description")

    async def test_a_description_edited_here_is_not_overwritten(self):
        # Linking a second service, or re-linking after a mistake, must
        # not silently undo an edit -- which is how the app looks like it
        # forgot. What the tracker says is still recorded, so "use the
        # tracker's again" stays possible.
        await self._shelf()
        with mock_client(self._service(description="The tracker's words")):
            await service.catch_up(1, MANGAUPDATES, series_id=1)
        await self.db.execute("UPDATE series SET summary = 'Mine' WHERE id = 1")
        await self.db.commit()
        with mock_client(self._service(description="The tracker's newer words")):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        row = await (await self.db.execute(
            "SELECT summary, source_summary FROM series WHERE id = 1")).fetchone()
        self.assertFalse(result["described"])
        self.assertEqual(row["summary"], "Mine")
        self.assertEqual(row["source_summary"], "The tracker's newer words")

    async def test_a_service_that_will_not_answer_does_not_break_the_link(self):
        # This runs from the Link button. The link itself is already
        # saved, and the push that follows is what reports the service's
        # health -- an unreachable catalogue must not turn "linked" into
        # an error page.
        await self._shelf()
        with mock_client(lambda request: httpx.Response(500, json={})):
            result = await service.catch_up(1, MANGAUPDATES, series_id=1)
        self.assertEqual(result, {"marked": 0, "described": False, "remote": None})


class DescriptionsForLinksMadeEarlier(_Case):
    """A link made before descriptions existed is never revisited -- the
    same shape as the scanner skipping an unchanged file -- so those
    series stay blank unless something goes back for them. This is that,
    and like every other backfill here it has to do nothing at all the
    second time."""

    def _catalogue(self, description="From the catalogue", status=200):
        self.asked = []

        def handler(request: httpx.Request):
            if request.url.path.startswith("/v1/series/"):
                self.asked.append(request.url.path)
                if status != 200:
                    return httpx.Response(status, json={})
                return httpx.Response(200, json={"description": description})
            return httpx.Response(200, json={})
        return handler

    async def _linked(self, series_id):
        # Not self.series(): that one hardcodes a folder, and this needs
        # several shelves at once to see what a whole library costs.
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path, number_kind) "
            "VALUES (?, 1, 'S', 's', ?, 'volume')", (series_id, f"s{series_id}"))
        await self.db.commit()
        await self.link(series_id=series_id, remote_id=str(1000 + series_id))

    async def _summaries(self):
        rows = await (await self.db.execute(
            "SELECT id, summary, source_summary FROM series ORDER BY id")).fetchall()
        return {r["id"]: (r["summary"], r["source_summary"]) for r in rows}

    async def _run(self, handler):
        with mock_client(handler):
            return await service.backfill_summaries(pause=0)

    async def test_a_series_linked_before_this_existed_gets_its_description(self):
        await self.connect()
        await self._linked(1)
        self.assertEqual(await self._run(self._catalogue()), 1)
        self.assertEqual((await self._summaries())[1],
                         ("From the catalogue", "From the catalogue"))

    async def test_the_second_start_asks_nothing(self):
        # The rule every backfill in main.py follows: cheap in the steady
        # state. `source_summary` is what records that this one has run.
        await self.connect()
        await self._linked(1)
        await self._run(self._catalogue())
        self.assertEqual(await self._run(self._catalogue()), 0)
        self.assertEqual(self.asked, [])

    async def test_a_catalogue_with_nothing_to_say_is_asked_only_once(self):
        # "No description" is an ordinary answer and has to be recorded as
        # one, or every start would ask about the same series again.
        await self.connect()
        await self._linked(1)
        self.assertEqual(await self._run(self._catalogue(description="")), 0)
        self.assertEqual((await self._summaries())[1], ("", ""))
        await self._run(self._catalogue())
        self.assertEqual(self.asked, [])

    async def test_a_service_that_will_not_answer_is_tried_again_next_time(self):
        # A failure is NOT an answer: recording it the same way would
        # leave the series blank forever because a catalogue was down for
        # a minute.
        await self.connect()
        await self._linked(1)
        self.assertEqual(await self._run(self._catalogue(status=500)), 0)
        self.assertEqual((await self._summaries())[1], (None, None))
        self.assertEqual(await self._run(self._catalogue()), 1)

    async def test_one_refusal_stops_it_asking_that_account_again(self):
        # A signed-out or rate-limited account would otherwise produce one
        # doomed request per linked series -- three hundred of them on a
        # real library.
        await self.connect()
        for series_id in (1, 2, 3):
            await self._linked(series_id)
        await self._run(self._catalogue(status=500))
        self.assertEqual(len(self.asked), 1)

    async def test_an_edit_here_still_wins(self):
        # Same rule as a link's own fetch: what the tracker says is
        # recorded, what is on the page is left alone.
        await self.connect()
        await self._linked(1)
        await self.db.execute("UPDATE series SET summary = 'Mine' WHERE id = 1")
        await self.db.commit()
        self.assertEqual(await self._run(self._catalogue()), 0)
        self.assertEqual((await self._summaries())[1], ("Mine", "From the catalogue"))

    async def test_a_novel_is_not_its_business(self):
        # A novel's synopsis comes from the site it was scraped from,
        # which is a better source and has a backfill of its own.
        await self.connect()
        await self.novel(chapters=(1.0,))
        await self.link(novel_id=1, remote_id="77")
        self.assertEqual(await self._run(self._catalogue()), 0)
        self.assertEqual(self.asked, [])


class TheDescriptionAppearsWhereItLives(_Case):
    """A link that fetched a description says so in the panel. The
    paragraph it is talking about lives under the series title, outside
    what htmx swapped -- so unless the response carries it out of band,
    the panel announces a description the page is not showing, and only a
    reload makes the claim true."""

    @staticmethod
    def _request():
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/tracking/mangaupdates/link", "raw_path": b"/tracking/mangaupdates/link",
            "query_string": b"", "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"hx-request", b"true")],
        }
        return Request(scope)

    def _service(self, description="From the catalogue"):
        def handler(request: httpx.Request):
            if request.url.path.startswith("/v1/lists/series/"):
                return httpx.Response(404, json={})
            if request.url.path.startswith("/v1/series/"):
                return httpx.Response(200, json={"description": description})
            return httpx.Response(200, json={})
        return handler

    async def _link(self, *, series_id=None, novel_id=None, description="From the catalogue"):
        from app.tracking.router import link_series
        with mock_client(self._service(description)):
            response = await link_series(
                MANGAUPDATES, self._request(), remote_id="4242", remote_title="Remote",
                remote_url="https://mu/4242", series_id=series_id, novel_id=novel_id,
                user={"id": 1}, db=self.db)
        await service.drain()
        return response.template.render(response.context)

    async def test_the_paragraph_comes_back_with_the_panel(self):
        await self.series()
        await self.connect()
        html = await self._link(series_id=1)
        paragraph = html.split('id="series-summary"', 1)[1].split("</p>", 1)[0]
        self.assertIn('hx-swap-oob="true"', paragraph)
        self.assertIn("From the catalogue", paragraph)
        # And it is not still wearing the class that hides it while there
        # is nothing to show, or it would arrive invisible.
        self.assertNotIn("hidden", paragraph.split(">", 1)[0])

    async def test_it_says_so_and_shows_it_from_the_same_fact(self):
        # The line in the panel and the swapped paragraph both hang off
        # `described`, so the panel cannot claim something the page then
        # fails to show.
        await self.series()
        await self.connect()
        await self.db.execute("UPDATE series SET summary = 'Mine' WHERE id = 1")
        await self.db.commit()
        html = await self._link(series_id=1)
        self.assertNotIn("Description taken from", html)
        # The icon beside the title is swapped out of band by every link,
        # so what has to be absent is the PARAGRAPH, not out-of-band
        # swapping as such.
        self.assertNotIn('id="series-summary"', html)

    async def test_a_novel_link_swaps_nothing(self):
        # A novel page has no such paragraph: an out-of-band swap aimed at
        # an element that is not there is dropped silently, so sending one
        # is only a way to be wrong later.
        await self.novel(chapters=(1.0,))
        await self.connect()
        html = await self._link(novel_id=1)
        self.assertNotIn('id="series-summary"', html)


class TheIconsKeepUpWithTheLink(_Case):
    """Each icon reports whether this shelf is tied to its own service,
    and they live OUTSIDE the dialog -- so linking something on the very
    page whose icon says "not linked" left it saying that until the next
    load."""

    @staticmethod
    def _request():
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/tracking/mangaupdates/link", "raw_path": b"/tracking/mangaupdates/link",
            "query_string": b"", "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"hx-request", b"true")],
        }
        return Request(scope)

    def _service(self):
        def handler(request: httpx.Request):
            if request.url.path.startswith("/v1/lists/series/"):
                return httpx.Response(404, json={})
            return httpx.Response(200, json={})
        return handler

    def _icon(self, html, tracker=MANGAUPDATES):
        return html.split(f'id="tracking-toggle-{tracker}"', 1)[1].split("</button>", 1)[0]

    async def _link(self):
        from app.tracking.router import link_series
        with mock_client(self._service()):
            response = await link_series(
                MANGAUPDATES, self._request(), remote_id="4242", remote_title="Remote",
                remote_url="https://mu/4242", series_id=1, novel_id=None,
                user={"id": 1}, db=self.db)
        await service.drain()
        return response.template.render(response.context)

    async def _unlink(self):
        from app.tracking.router import unlink_series
        response = await unlink_series(
            MANGAUPDATES, self._request(), series_id=1, novel_id=None,
            user={"id": 1}, db=self.db)
        return response.template.render(response.context)

    async def test_a_link_sends_the_new_colour_back(self):
        await self.series()
        await self.connect()
        icon = self._icon(await self._link())
        self.assertIn('hx-swap-oob="true"', icon)
        self.assertIn("text-emerald-300", icon)
        self.assertNotIn("text-neutral-500", icon)

    async def test_the_other_services_icon_comes_back_unchanged(self):
        # Both are sent, not just the one that changed: it costs one
        # element and it means the swap cannot be the thing that gets out
        # of step.
        await self.series()
        await self.connect()
        html = await self._link()
        other = self._icon(html, MYANIMELIST)
        self.assertIn('hx-swap-oob="true"', other)
        self.assertIn("text-neutral-500", other)

    async def test_an_unlink_sends_it_back_grey(self):
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        icon = self._icon(await self._unlink())
        self.assertIn('hx-swap-oob="true"', icon)
        self.assertIn("text-neutral-500", icon)

    async def test_a_send_does_not_replace_them(self):
        # Nothing about sending numbers changes what an icon says, and
        # replacing an element for nothing throws away the state it
        # carries.
        from app.tracking.router import send_to_tracker
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        with mock_client(self._service()):
            response = await send_to_tracker(
                MANGAUPDATES, self._request(), series_id=1, novel_id=None,
                chapter="3", volume="", score="", status="", start_date="", finish_date="",
                user={"id": 1}, db=self.db)
        await service.drain()
        self.assertNotIn('id="tracking-toggle-', response.template.render(response.context))

    def test_all_three_places_render_the_one_macro(self):
        # The markup used to be copied into both pages; the dialog is now
        # a third renderer of it, and three copies of an icon whose colour
        # means something are three chances to disagree.
        for page in ("app/templates/library/series.html",
                     "app/templates/novels/detail.html",
                     "app/templates/_tracker_panel.html"):
            text = (ROOT / page).read_text()
            self.assertIn("tracker_icon.icon(", text, page)
            self.assertNotIn('<button type="button" id="tracking-toggle', text, page)

    def test_the_click_handler_survives_the_swap(self):
        # A listener bound to a button would go with it on the first swap,
        # and silently: the icon would still be there, still the right
        # colour, and no longer open anything.
        js = (ROOT / "app/static/js/tracker-toggle.js").read_text()
        self.assertIn('closest("[data-tracker-dialog]")', js)
        for page in ("app/templates/library/series.html",
                     "app/templates/novels/detail.html"):
            text = (ROOT / page).read_text()
            self.assertNotIn('getElementById("tracking-toggle', text, page)
            self.assertIn("js/tracker-toggle.js", text, page)


class TheGridHearsAboutALink(_Case):
    """The dot lives on the home grid, and a link is made on a different
    page -- so a home page open in another tab went on showing "not
    linked" until it was reloaded. The link and the unlink publish, and
    the browser refetches the card it already has."""

    def setUp(self):
        self.published = []
        original = library_events.publish
        library_events.publish = self.published.append
        self.addCleanup(lambda: setattr(library_events, "publish", original))

    @staticmethod
    def _request():
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/tracking/mangaupdates/link", "raw_path": b"/tracking/mangaupdates/link",
            "query_string": b"", "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"hx-request", b"true")],
        }
        return Request(scope)

    def _service(self):
        def handler(request: httpx.Request):
            if request.url.path.startswith("/v1/lists/series/"):
                return httpx.Response(404, json={})
            return httpx.Response(200, json={})
        return handler

    async def _link(self, *, series_id=None, novel_id=None):
        from app.tracking.router import link_series
        with mock_client(self._service()):
            await link_series(MANGAUPDATES, self._request(), remote_id="4242",
                              remote_title="Remote", remote_url="https://mu/4242",
                              series_id=series_id, novel_id=novel_id,
                              user={"id": 1}, db=self.db)
        await service.drain()

    async def _unlink(self, *, series_id=None, novel_id=None):
        from app.tracking.router import unlink_series
        await unlink_series(MANGAUPDATES, self._request(), series_id=series_id,
                            novel_id=novel_id, user={"id": 1}, db=self.db)

    def _events(self, kind):
        return [e for e in self.published if e["type"] == kind]

    async def test_linking_a_series_tells_the_grid(self):
        await self.series()
        await self.connect()
        await self._link(series_id=1)
        self.assertEqual(self._events("series_updated"), [{"type": "series_updated", "series_id": 1}])

    async def test_unlinking_a_series_tells_it_too(self):
        # Going back to grey is exactly as much of a change as going blue.
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        await self._unlink(series_id=1)
        self.assertEqual(self._events("series_updated"), [{"type": "series_updated", "series_id": 1}])

    async def test_a_novel_gets_its_own_event(self):
        # Not series_updated with a novel id: the two are looked up by
        # different attributes on the card, so one event for both would
        # send every browser hunting for a card that is not there.
        await self.novel(chapters=(1.0,))
        await self.connect()
        await self._link(novel_id=1)
        self.assertEqual(self._events("novel_updated"), [{"type": "novel_updated", "novel_id": 1}])
        self.assertEqual(self._events("series_updated"), [])

    async def test_unlinking_a_novel_tells_the_grid(self):
        await self.novel(chapters=(1.0,))
        await self.connect()
        await self.link(novel_id=1)
        await self._unlink(novel_id=1)
        self.assertEqual(self._events("novel_updated"), [{"type": "novel_updated", "novel_id": 1}])

    def test_the_browser_acts_on_the_novel_event(self):
        js = (ROOT / "app/static/js/library-events.js").read_text()
        # Named events now: the server writes `event: novel_updated` and
        # the browser has a handler under that key, where it used to test a
        # `type` field out of the payload.
        self.assertIn("novel_updated:", js)
        self.assertIn('refreshCard("novel", data.novel_id)', js)

    async def test_the_event_carries_no_count(self):
        # A link belongs to a READER. The event reaches every connected
        # browser, so a number in it would be one reader's tracking drawn
        # on another's screen; refetching the card answers each browser
        # with its own.
        await self.series()
        await self.connect()
        await self._link(series_id=1)
        for event in self._events("series_updated"):
            self.assertEqual(set(event), {"type", "series_id"})


class TheDotOnTheCover(_Case):
    """The home grid is the one screen that shows every cover at once, so
    it is where "what still needs linking" gets answered. A dot in the
    corner of each cover, in the same three colours as the icon on the
    series page."""

    @staticmethod
    def _request():
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
            "headers": [], "client": ("test", 1), "server": ("test", 80),
        }
        return Request(scope)

    async def series(self, series_id=1, kind="volume"):
        """On the shelf means holding something: the grid lists only series
        that still have an item (see library/series_scope.py), so a fixture
        without one produces no card to put a dot on."""
        await super().series(series_id, kind)
        await self.item(900 + series_id, 1, series_id=series_id)

    async def _cards(self):
        from app.library.router import home
        response = await home(self._request(), {"id": 1, "username": "u", "avatar_path": None,
                                                "page_time_cap_seconds": 180,
                                                "cover_blur_px": 12}, self.db)
        return response.context

    async def test_an_unlinked_series_still_gets_a_dot(self):
        # The question this answers is "what is NOT linked", so drawing
        # nothing there would answer the opposite one -- and an absent dot
        # reads as "nothing to say", not as "not linked".
        await self.series()
        context = await self._cards()
        card = next(c for c in context["cards"] if c["kind"] == "series")
        self.assertEqual(card["tracked_count"], 0)

    async def test_it_counts_services_and_not_rows(self):
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        context = await self._cards()
        card = next(c for c in context["cards"] if c["kind"] == "series")
        self.assertEqual(card["tracked_count"], 1)
        self.assertEqual(context["tracker_total"], 2)

    async def test_both_services_is_the_full_count(self):
        await self.series()
        await self.connect()
        for tracker in (MANGAUPDATES, MYANIMELIST):
            await store.save_link(self.db, 1, tracker, series_id=1, remote_id="9",
                                  remote_title="R", remote_url="u")
        await self.db.commit()
        context = await self._cards()
        card = next(c for c in context["cards"] if c["kind"] == "series")
        self.assertEqual(card["tracked_count"], context["tracker_total"])

    async def test_another_readers_link_is_not_mine(self):
        # A link belongs to a reader, like a status does -- a shared
        # install would otherwise show one account's tracking on another's
        # grid.
        await self.series()
        await store.save_account(self.db, 2, MANGAUPDATES, access_token="t", username="v")
        await store.save_link(self.db, 2, MANGAUPDATES, series_id=1, remote_id="9",
                              remote_title="R", remote_url="u")
        await self.db.commit()
        context = await self._cards()
        card = next(c for c in context["cards"] if c["kind"] == "series")
        self.assertEqual(card["tracked_count"], 0)

    async def test_a_novel_is_counted_too(self):
        await self.novel(chapters=(1.0,))
        await self.connect()
        await self.link(novel_id=1)
        context = await self._cards()
        card = next(c for c in context["cards"] if c["kind"] == "novel")
        self.assertEqual(card["tracked_count"], 1)

    async def test_the_card_it_renders_wears_the_colour(self):
        # The three states as they actually reach the screen. Rendered
        # rather than asserted on the number, because the number is only
        # useful if the template turns it into the same vocabulary the
        # series page's icon uses.
        await self.series()
        await self.connect()
        context = await self._cards()
        from app.library.router import templates
        grey = templates.get_template("library/index.html").render(context)
        self.assertIn('class="track-dot text-neutral-500"', grey)
        await self.link(series_id=1)
        one = templates.get_template("library/index.html").render(await self._cards())
        self.assertIn('class="track-dot text-sky-300"', one)
        await store.save_link(self.db, 1, MYANIMELIST, series_id=1, remote_id="9",
                              remote_title="R", remote_url="u")
        await self.db.commit()
        both = templates.get_template("library/index.html").render(await self._cards())
        self.assertIn('class="track-dot text-emerald-300"', both)

    def test_a_card_built_in_the_browser_draws_one_as_well(self):
        # Same builder, same grid: a card that arrives over SSE must not
        # be the one without a dot.
        js = (ROOT / "app/static/js/library-events.js").read_text()
        self.assertIn("track-dot", js)
        for colour in ("text-neutral-500", "text-sky-300", "text-emerald-300"):
            self.assertIn(colour, js)


class TheIconsThatOpenTheDialog(_Case):
    """Two icons, one per service, and each says the one thing its being
    permanently on screen is for: whether THIS shelf is linked to THAT
    service. The dialog behind them is shut until one is clicked."""

    @staticmethod
    def _request():
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/series/1", "raw_path": b"/series/1", "query_string": b"",
            "root_path": "", "headers": [], "client": ("test", 1), "server": ("test", 80),
        }
        return Request(scope)

    async def _page(self):
        from app.library.router import series_detail
        response = await series_detail(self._request(), 1, None, {"id": 1, "username": "u",
                                                                  "avatar_path": None,
                                                                  "page_time_cap_seconds": 180,
                                                                  "cover_blur_px": 12}, self.db)
        return response.template.render(response.context)

    async def _icon(self, tracker):
        html = await self._page()
        return html.split(f'id="tracking-toggle-{tracker}"', 1)[1].split("</button>", 1)[0]

    async def _mal_account(self):
        await store.save_account(self.db, 1, MYANIMELIST, access_token="mal",
                                 refresh_token=None, username="r", expires_at=None)
        await self.db.commit()

    async def test_there_is_one_icon_per_service(self):
        # The count is the point: one icon could only ever report a
        # number, and every click on it began by asking which you meant.
        await self.series()
        html = await self._page()
        for tracker in (MANGAUPDATES, MYANIMELIST):
            self.assertIn(f'id="tracking-toggle-{tracker}"', html)

    async def test_each_one_wears_its_own_services_name(self):
        await self.series()
        self.assertIn(">MU<", await self._icon(MANGAUPDATES))
        self.assertIn(">MAL<", await self._icon(MYANIMELIST))

    async def test_nothing_linked_is_grey_on_both(self):
        await self.series()
        for tracker in (MANGAUPDATES, MYANIMELIST):
            with self.subTest(tracker=tracker):
                self.assertIn("text-neutral-500", await self._icon(tracker))

    async def test_a_link_colours_only_the_service_it_was_made_with(self):
        # The whole reason there are two: "linked" is a fact about one
        # service, and one icon could only average them.
        await self.series()
        await self.connect()
        await self._mal_account()
        await self.link(series_id=1)
        self.assertIn("text-emerald-300", await self._icon(MANGAUPDATES))
        mal = await self._icon(MYANIMELIST)
        self.assertIn("text-neutral-500", mal)
        self.assertNotIn("text-emerald-300", mal)

    async def test_each_icon_fetches_its_own_services_panel(self):
        # Fetched on the click rather than rendered with the page: the
        # editor inside opens with the service's own numbers, and a copy
        # baked in here would send a stale one back.
        await self.series()
        self.assertIn('hx-get="/tracking/mangaupdates/panel?series_id=1"',
                      await self._icon(MANGAUPDATES))
        self.assertIn('hx-get="/tracking/myanimelist/panel?series_id=1"',
                      await self._icon(MYANIMELIST))

    async def test_the_dialog_starts_shut_and_empty(self):
        # A search box and two service rows are a wall on every visit that
        # is not about tracking -- and the contents cost two requests to a
        # catalogue, which nobody has asked for until they click.
        await self.series()
        html = await self._page()
        dialog = html.split('id="tracker-dialog"', 1)[1].split(">", 1)[0]
        self.assertNotIn("open", dialog)
        self.assertIn('<div id="tracker-panel"></div>', html)

    async def test_a_novel_page_renders_the_same_two_icons(self):
        # The same macro on both pages, so they cannot drift apart.
        from app.novels.router import novel_detail
        await self.novel()
        scope = dict(self._request().scope, path="/novels/1", raw_path=b"/novels/1")
        response = await novel_detail(
            1, Request(scope), None,
            {"id": 1, "username": "u", "avatar_path": None,
             "page_time_cap_seconds": 180, "cover_blur_px": 12}, self.db)
        html = response.template.render(response.context)
        for tracker in (MANGAUPDATES, MYANIMELIST):
            self.assertIn(f'id="tracking-toggle-{tracker}"', html)
        self.assertIn('hx-get="/tracking/mangaupdates/panel?novel_id=1"', html)


class TheDialogTheIconsOpen(_Case):
    """One service per dialog, opened by a request of its own -- so the
    editor inside can carry what that service holds RIGHT NOW."""

    @staticmethod
    def _request(method="GET", path="/tracking/mangaupdates/panel"):
        scope = {
            "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
            "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"hx-request", b"true")],
        }
        return Request(scope)

    async def _open(self, tracker=MANGAUPDATES, handler=None, **kwargs):
        from app.tracking.router import open_panel
        if handler is None:
            def handler(request):
                return httpx.Response(404, json={})
        module = mangaupdates if tracker == MANGAUPDATES else myanimelist
        with mock_client(handler, module):
            response = await open_panel(
                tracker, self._request(), user={"id": 1}, db=self.db,
                **{"series_id": 1, "novel_id": None, **kwargs})
        return response.template.render(response.context)

    async def _mal_link(self):
        await store.save_account(self.db, 1, MYANIMELIST, access_token="mal",
                                 refresh_token=None, username="r", expires_at=None)
        await store.save_link(self.db, 1, MYANIMELIST, series_id=1, remote_id="7",
                              remote_title="R", remote_url="https://mal/7")
        await self.db.commit()

    async def test_it_shows_the_one_service_that_was_asked_for(self):
        await self.series()
        await self.connect()
        await self._mal_link()
        html = await self._open(MANGAUPDATES)
        self.assertIn("MangaUpdates", html)
        # The other service's own row is NOT in here: its link, its boxes
        # and its failures belong to its own dialog.
        self.assertNotIn("Find this on MyAnimeList", html)
        self.assertNotIn("https://mal/7", html)

    async def test_an_unconnected_service_says_how_to_connect_it(self):
        await self.series()
        html = await self._open(MANGAUPDATES)
        self.assertIn("Connect MangaUpdates", html)
        self.assertIn("/settings#tracking", html)

    async def test_an_unlinked_service_opens_on_a_search_box(self):
        await self.series()
        await self.connect()
        html = await self._open(MANGAUPDATES)
        self.assertIn("Find this on MangaUpdates", html)

    async def test_the_editor_opens_with_what_the_service_holds(self):
        # Read at the moment the dialog was, because the whole point of an
        # editor is to change what is there: pre-filled from anything
        # older, pressing Send puts a stale number back.
        await self.series()
        await self.connect()
        await self.link(series_id=1)

        def handler(request: httpx.Request):
            if request.url.path.endswith("/rating"):
                return httpx.Response(200, json={"rating": 7.5})
            return httpx.Response(200, json={
                "list_id": mangaupdates.COMPLETE_LIST,
                "status": {"chapter": 41, "volume": 3}})

        html = await self._open(MANGAUPDATES, handler)
        self.assertIn('name="chapter" min="0" step="1" inputmode="numeric"\n'
                      '                       value="41"', html)
        self.assertIn('value="3"', html)
        self.assertIn('value="7.5"', html)
        # The list the entry is on IS its status on this service, so the
        # select arrives on it.
        self.assertEqual(selected_status(html), str(mangaupdates.COMPLETE_LIST))

    async def test_an_entry_on_the_reading_list_arrives_on_that_list(self):
        # The control for the line above: the selection has to come from
        # the list and not from one option simply always being selected.
        await self.series()
        await self.connect()
        await self.link(series_id=1)

        def handler(request: httpx.Request):
            if request.url.path.endswith("/rating"):
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"list_id": 0, "status": {"chapter": 4}})

        html = await self._open(MANGAUPDATES, handler)
        self.assertEqual(selected_status(html), str(mangaupdates.READING_LIST))

    async def test_an_entry_on_no_list_arrives_on_leave_it_alone(self):
        # Not on a list yet: nothing in the service's own list is where it
        # is, so the select opens on an EMPTY value -- which the send reads
        # as "leave it", instead of quietly filing it on the first list.
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        html = await self._open(MANGAUPDATES)
        self.assertEqual(selected_status(html), "")
        self.assertIn(">Not on a list<", html)

    async def test_myanimelist_offers_its_own_words_and_its_dates(self):
        await self.series()
        await self._mal_link()

        def handler(request: httpx.Request):
            return httpx.Response(200, json={"id": 7, "my_list_status": {
                "status": "on_hold", "num_chapters_read": 3, "score": 0,
                "start_date": "2026-01-03", "finish_date": "2026"}})

        html = await self._open(MYANIMELIST, handler)
        self.assertEqual(selected_status(html), "on_hold")
        self.assertIn(">Plan to read<", html)
        self.assertIn('name="start_date"\n                       value="2026-01-03"', html)
        # A partial date is what MAL keeps for a vague entry; it fits no
        # date box, and the box is left empty rather than guessed at.
        self.assertIn('name="finish_date"\n                       value=""', html)

    async def test_mangaupdates_offers_no_date_boxes(self):
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        html = await self._open(MANGAUPDATES, lambda request: httpx.Response(
            200, json={"list_id": 0, "status": {}}))
        self.assertNotIn('name="start_date"', html)
        self.assertIn("MangaUpdates keeps no reading dates", html)

    async def test_a_service_that_will_not_answer_leaves_the_boxes_out(self):
        # An empty box that sends is how a rating gets wiped, so an
        # unreadable service gets no form at all -- and says why.
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        html = await self._open(
            MANGAUPDATES, lambda request: httpx.Response(503, json={}))
        self.assertIn("Could not read MangaUpdates", html)
        self.assertNotIn('name="chapter"', html)

    async def test_the_dialog_is_open_when_it_is_rendered_with_a_page(self):
        # The no-htmx path: a search or a link that came back as a
        # redirect would otherwise land on a page showing no sign of it.
        from app.library.router import series_detail
        await self.series()
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/series/1", "raw_path": b"/series/1", "query_string": b"",
            "root_path": "", "headers": [], "client": ("test", 1), "server": ("test", 80),
        }
        response = await series_detail(
            Request(scope), 1, None,
            {"id": 1, "username": "u", "avatar_path": None,
             "page_time_cap_seconds": 180, "cover_blur_px": 12}, self.db)
        context = dict(response.context)
        self.assertIsNone(context["tracker_open"])


if __name__ == "__main__":
    unittest.main()


class WhenTheShelfIsFinished(_Case):
    """Every status of this app's six that a catalogue has a word for is
    reported to it, and the rules are the same three that govern a
    number: said once per change, and never at all to a service that got
    there first. "Finished" is the one most of these are written about."""

    async def _finish(self, *, series_id=None, novel_id=None, user_id=1, status="completed"):
        table, column = (
            ("series_status", "series_id") if series_id is not None
            else ("novel_status", "novel_id")
        )
        await self.db.execute(
            f"INSERT INTO {table} (user_id, {column}, status) VALUES (?, ?, ?)",
            (user_id, series_id if series_id is not None else novel_id, status))
        await self.db.commit()

    @staticmethod
    def _recording(sent, *, list_id=0, chapter=None):
        """MangaUpdates, answering with an entry on `list_id` and keeping
        every write it is handed."""
        def handler(request: httpx.Request):
            if request.url.path.startswith("/v1/lists/series/") and request.method == "GET":
                return httpx.Response(200, json={
                    "list_id": list_id,
                    "status": {"volume": chapter} if chapter is not None else {},
                })
            sent.append((request.method, request.url.path, request.content))
            return httpx.Response(200, json={})
        return handler

    async def _pushed_status(self, user_id=1):
        row = await (await self.db.execute(
            "SELECT last_pushed_status FROM tracker_links WHERE user_id = ?", (user_id,))).fetchone()
        return row["last_pushed_status"]

    async def test_it_moves_the_entry_to_the_completed_list(self):
        # MangaUpdates has no status field: finishing something IS moving
        # it to list 2. Asserted on the wire rather than on the argument,
        # because the list id is the entire claim.
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            outcome = await service.push(1, series_id=1)
        self.assertEqual(outcome, "sent")
        body = json.loads(sent[0][2])[0]
        self.assertEqual(body["list_id"], mangaupdates.COMPLETE_LIST)
        self.assertEqual(await self._pushed_status(), "completed")

    async def test_myanimelist_hears_it_as_a_status(self):
        # The same claim, in the other service's vocabulary -- and in the
        # same PUT as the numbers, since that call replaces the record.
        await self.series()
        await self.item(1, 3.0)
        await self.read(1)
        await store.save_account(self.db, 1, MYANIMELIST, access_token="t", username="r")
        await store.save_link(
            self.db, 1, MYANIMELIST, series_id=1, remote_id="7",
            remote_title="R", remote_url="https://mal/7")
        await self.db.commit()
        await self._finish(series_id=1)
        sent = []

        def handler(request: httpx.Request):
            if request.method == "GET":
                return httpx.Response(200, json={"id": 7, "my_list_status": {
                    "num_volumes_read": 1, "status": "reading", "score": 8}})
            sent.append(request.content.decode())
            return httpx.Response(200, json={})

        with mock_client(handler, myanimelist):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        form = urllib.parse.parse_qs(sent[0])
        self.assertEqual(form["status"], ["completed"])
        # The rating it was not asked to move is still re-sent as it
        # stands: this PUT replaces, so omitting it wipes it.
        self.assertEqual(form["score"], ["8"])
        self.assertEqual(form["num_volumes_read"], ["3"])

    async def test_it_is_said_once_and_not_on_every_page_turn(self):
        # A push runs whenever a page is turned, and a finished series
        # goes on being read. Without `last_pushed_status` the same claim
        # would cost a request every time.
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            await service.push(1, series_id=1)
            first = len(sent)
            self.assertEqual(await service.push(1, series_id=1), "up-to-date")
        self.assertEqual((first, len(sent)), (1, 1))

    async def test_finishing_something_with_nothing_marked_read_still_speaks(self):
        # The shelf most worth reporting -- read elsewhere, filed here --
        # is exactly the one with no local progress behind it.
        await self.series()
        await self.item(1, 1.0)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        self.assertEqual(json.loads(sent[0][2])[0]["list_id"], mangaupdates.COMPLETE_LIST)

    async def test_nothing_read_and_not_finished_is_still_nothing_to_say(self):
        # The control for the test above: the guard it relaxed still holds
        # everywhere else.
        await self.series()
        await self.item(1, 1.0)
        await self.connect()
        await self.link(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            self.assertEqual(await service.push(1, series_id=1), "nothing-read")
        self.assertEqual(sent, [])

    async def test_an_ordinary_push_leaves_the_list_where_it_found_it(self):
        # Not being marked finished is the ABSENCE of an opinion, not the
        # opinion that it is unfinished. A series the reader filed under
        # "on hold" on the site (list 4) must come back on list 4.
        await self.series()
        await self.item(1, 5.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        sent = []
        with mock_client(self._recording(sent, list_id=4)):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        self.assertEqual(json.loads(sent[0][2])[0]["list_id"], 4)
        self.assertIsNone(await self._pushed_status())

    async def test_every_status_with_a_list_moves_the_entry_to_it(self):
        # The user asked for every status, not only Finished: each of this
        # app's statuses that MangaUpdates has a list for goes to it.
        for status, list_id in (("reading", mangaupdates.READING_LIST),
                                ("plan_to_read", mangaupdates.WISH_LIST),
                                ("dropped", mangaupdates.UNFINISHED_LIST),
                                ("on_hold", mangaupdates.ON_HOLD_LIST)):
            with self.subTest(status=status):
                await self.db.execute("DELETE FROM series_status")
                await self.db.execute("DELETE FROM tracker_links")
                await self.db.execute("DELETE FROM series_status")
                await self.db.execute("DELETE FROM items WHERE id = 1")
                await self.db.execute("DELETE FROM series WHERE id = 1")
                await self.db.commit()
                await self.series()
                await self.item(1, 2.0)
                await self.read(1)
                await self.connect()
                await self.link(series_id=1)
                await self._finish(series_id=1, status=status)
                sent = []
                # Starting from a list it is NOT headed for, so a push that
                # merely kept the list would fail here -- including for
                # "reading", whose list is the default.
                with mock_client(self._recording(sent, list_id=mangaupdates.COMPLETE_LIST)):
                    await service.push(1, series_id=1)
                self.assertEqual(json.loads(sent[0][2])[0]["list_id"], list_id)
                self.assertEqual(await self._pushed_status(), status)

    async def test_a_status_the_service_has_no_word_for_is_not_sent(self):
        # "Stopped reading" is neither on hold nor dropped, and MangaUpdates
        # has no list of its own for it: the entry stays where it is.
        await self.series()
        await self.item(1, 2.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1, status="stopped_reading")
        sent = []
        with mock_client(self._recording(sent, list_id=mangaupdates.ON_HOLD_LIST)):
            await service.push(1, series_id=1)
        self.assertEqual(json.loads(sent[0][2])[0]["list_id"], mangaupdates.ON_HOLD_LIST)
        self.assertIsNone(await self._pushed_status())

    async def test_a_change_of_status_is_news_again(self):
        # Said once per CHANGE: finished, then put back to reading for a
        # re-read, is two things to say, and the second one is not
        # swallowed by the first having been recorded.
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            await service.push(1, series_id=1)
            await self.db.execute("UPDATE series_status SET status = 'reading'")
            await self.db.commit()
            self.assertEqual(await service.push(1, series_id=1), "sent")
        self.assertEqual([json.loads(body)[0]["list_id"] for _, _, body in sent],
                         [mangaupdates.COMPLETE_LIST, mangaupdates.READING_LIST])
        self.assertEqual(await self._pushed_status(), "reading")

    async def test_plan_to_read_with_nothing_read_goes_straight_onto_the_wish_list(self):
        # Nothing read is the ordinary state of a shelf filed under "plan
        # to read", and an entry on no list yet is ADDED to the list it
        # belongs on rather than to Reading and then moved.
        await self.series()
        await self.item(1, 1.0)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1, status="plan_to_read")
        sent = []

        def handler(request: httpx.Request):
            if request.method == "GET":
                return httpx.Response(404, json={})
            sent.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={})

        with mock_client(handler):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        self.assertEqual(sent[0], ("/v1/lists/series",
                                   [{"series": {"id": 99}, "list_id": mangaupdates.WISH_LIST}]))

    async def test_a_service_that_got_there_first_is_not_told_again(self):
        # Finished on the site, or by another device. There is nothing to
        # send, but it is still true -- so it is RECORDED rather than
        # asked about again on the next page turn.
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1)
        sent = []
        with mock_client(self._recording(sent, list_id=mangaupdates.COMPLETE_LIST, chapter=9)):
            await service.push(1, series_id=1)
        self.assertEqual(sent, [])
        self.assertEqual(await self._pushed_status(), "completed")

    async def test_myanimelist_is_not_re_written_when_it_is_already_completed(self):
        # The service got there first -- moved on the site, or told by
        # another device. On MangaUpdates this costs nothing either way
        # (the list it would be moved to is the one it is on), but MAL's
        # call REPLACES the record, so telling it again is a real write of
        # fields this app only re-sent because it had to.
        await self.series()
        await self.item(1, 3.0)
        await self.read(1)
        await store.save_account(self.db, 1, MYANIMELIST, access_token="t", username="r")
        await store.save_link(
            self.db, 1, MYANIMELIST, series_id=1, remote_id="7",
            remote_title="R", remote_url="https://mal/7")
        await self.db.commit()
        await self._finish(series_id=1)
        writes = []

        def handler(request: httpx.Request):
            if request.method == "GET":
                # Already completed there, and already further ahead.
                return httpx.Response(200, json={"id": 7, "my_list_status": {
                    "num_volumes_read": 9, "status": "completed", "score": 8}})
            writes.append(request.content.decode())
            return httpx.Response(200, json={})

        with mock_client(handler, myanimelist):
            await service.push(1, series_id=1)
        self.assertEqual(writes, [])
        # Still recorded, so the next page turn does not ask again.
        self.assertEqual(await self._pushed_status(), "completed")

    async def test_a_finished_novel_is_reported_the_same_way(self):
        await self.novel()
        await self.read_chapter(101)
        await self.connect()
        await self.link(novel_id=1)
        await self._finish(novel_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            self.assertEqual(await service.push(1, novel_id=1), "sent")
        self.assertEqual(json.loads(sent[0][2])[0]["list_id"], mangaupdates.COMPLETE_LIST)

    async def test_another_readers_finish_is_not_this_readers(self):
        # The status table is per USER, like the link itself.
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1, user_id=2)
        sent = []
        with mock_client(self._recording(sent)):
            await service.push(1, series_id=1)
        self.assertEqual(json.loads(sent[0][2])[0]["list_id"], mangaupdates.READING_LIST)


class ReadingDatesOnMyAnimeList(_Case):
    """MyAnimeList keeps when the reader started and finished; MangaUpdates
    keeps neither. The app knows both from its own reading history, and
    fills them in -- only where the site has none, and only riding on a
    write that is happening anyway."""

    async def _mal(self):
        await store.save_account(self.db, 1, MYANIMELIST, access_token="t", username="r")
        await store.save_link(
            self.db, 1, MYANIMELIST, series_id=1, remote_id="7",
            remote_title="R", remote_url="https://mal/7")
        await self.db.commit()

    async def _history(self):
        # Read on two days; the second volume completed on the later one.
        await self.db.execute(
            "INSERT INTO page_reads (user_id, item_id, kind, page_index, started_at, seconds, cap_seconds)"
            " VALUES (1, 1, 'cbz', 0, '2026-03-02 12:00:00', 30, 120)")
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent, last_read_at, completed_at)"
            " VALUES (1, 2, 'completed', 100, '2026-03-09 12:00:00', '2026-03-09 12:00:00')")
        await self.db.commit()

    @staticmethod
    def _holding(writes, **status):
        def handler(request: httpx.Request):
            if request.method == "GET":
                return httpx.Response(200, json={"id": 7, "my_list_status": {
                    "status": "reading", "score": 0, "num_volumes_read": 0, **status}})
            writes.append(urllib.parse.parse_qs(request.content.decode()))
            return httpx.Response(200, json={})
        return handler

    async def test_the_dates_are_asked_for_by_name(self):
        # They are not among my_list_status' default fields: a read that
        # does not name them sees none, and every push would then "fill"
        # a date that was there all along.
        seen = []

        def handler(request: httpx.Request):
            seen.append(request.url.params["fields"])
            return httpx.Response(200, json={"id": 7, "my_list_status": {
                "status": "reading", "start_date": "2026-01-03"}})

        with mock_client(handler, myanimelist):
            remote = await myanimelist.get_progress("t", "7")
        self.assertIn("my_list_status{start_date,finish_date}", seen[0])
        self.assertEqual((remote.status, remote.start_date), ("reading", "2026-01-03"))

    async def test_the_dates_this_app_saw_are_the_first_read_and_the_last_finish(self):
        await self.series()
        await self.item(1, 1.0)
        await self.item(2, 2.0)
        await self._history()
        dates = await store.local_dates(self.db, 1, series_id=1)
        # Not finished yet, so there is no finish date: a date there would
        # claim something the reader has not said.
        self.assertEqual(dates, store.LocalDates(start="2026-03-02", finish=None))
        await self.db.execute(
            "INSERT INTO series_status (user_id, series_id, status) VALUES (1, 1, 'completed')")
        await self.db.commit()
        dates = await store.local_dates(self.db, 1, series_id=1)
        self.assertEqual(dates, store.LocalDates(start="2026-03-02", finish="2026-03-09"))

    async def test_finishing_fills_both_blank_dates(self):
        await self.series()
        await self.item(1, 1.0)
        await self.item(2, 2.0)
        await self._history()
        await self._mal()
        await self.db.execute(
            "INSERT INTO series_status (user_id, series_id, status) VALUES (1, 1, 'completed')")
        await self.db.commit()
        writes = []
        with mock_client(self._holding(writes), myanimelist):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        self.assertEqual(writes[0]["status"], ["completed"])
        self.assertEqual(writes[0]["start_date"], ["2026-03-02"])
        self.assertEqual(writes[0]["finish_date"], ["2026-03-09"])

    async def test_a_date_already_on_the_site_is_kept(self):
        # Somebody's own record, typed or imported: not overwritten by this
        # app's guess, and re-sent as it stands (the PUT replaces).
        await self.series()
        await self.item(1, 1.0)
        await self.item(2, 2.0)
        await self._history()
        await self._mal()
        writes = []
        with mock_client(self._holding(writes, start_date="2019-05-01"), myanimelist):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        self.assertEqual(writes[0]["start_date"], ["2019-05-01"])
        # Not finished here, so nothing claims a finish.
        self.assertNotIn("finish_date", writes[0])

    async def test_mangaupdates_is_never_sent_a_date(self):
        await self.series()
        await self.item(1, 1.0)
        await self.item(2, 2.0)
        await self._history()
        await self.connect()
        await self.link(series_id=1)
        await self.db.execute(
            "INSERT INTO series_status (user_id, series_id, status) VALUES (1, 1, 'completed')")
        await self.db.commit()
        bodies = []

        def handler(request: httpx.Request):
            if request.method == "GET":
                return httpx.Response(200, json={"list_id": 0, "status": {}})
            bodies.append(request.content.decode())
            return httpx.Response(200, json={})

        with mock_client(handler):
            await service.push(1, series_id=1)
        self.assertTrue(bodies)
        self.assertFalse(any("date" in body for body in bodies))

    async def test_the_editor_sends_the_dates_it_was_given(self):
        from app.tracking.router import send_to_tracker
        await self.series()
        await self._mal()
        writes = []
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/tracking/myanimelist/send", "raw_path": b"/tracking/myanimelist/send",
            "query_string": b"", "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"hx-request", b"true")],
        }
        with mock_client(self._holding(writes, finish_date="2020-01-01"), myanimelist):
            await send_to_tracker(
                MYANIMELIST, Request(scope), series_id=1, novel_id=None,
                chapter="", volume="", score="", status="dropped",
                start_date="2024-02-29", finish_date="not a date",
                user={"id": 1}, db=self.db)
        self.assertEqual(writes[0]["status"], ["dropped"])
        self.assertEqual(writes[0]["start_date"], ["2024-02-29"])
        # A box that is not a date is "leave it alone", like an empty one:
        # the date already there goes back as it stands.
        self.assertEqual(writes[0]["finish_date"], ["2020-01-01"])

    async def test_a_status_from_another_service_is_not_sent(self):
        # The select offers THIS service's words. Anything else -- a stale
        # form, a MangaUpdates list id posted to MAL -- is "leave it".
        from app.tracking.router import send_to_tracker
        await self.series()
        await self._mal()
        writes = []
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/tracking/myanimelist/send", "raw_path": b"/tracking/myanimelist/send",
            "query_string": b"", "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"hx-request", b"true")],
        }
        with mock_client(self._holding(writes, status="on_hold"), myanimelist):
            await send_to_tracker(
                MYANIMELIST, Request(scope), series_id=1, novel_id=None,
                chapter="4", volume="", score="", status="2",
                start_date="", finish_date="", user={"id": 1}, db=self.db)
        self.assertEqual(writes[0]["status"], ["on_hold"])


class TheRatingIsItsOwnResource(unittest.IsolatedAsyncioTestCase):
    """MangaUpdates keeps a rating at its own endpoint and MyAnimeList
    keeps it on the list-status record, which is the whole reason
    `get_state` exists apart from the `get_progress` a push uses."""

    def mal(self, handler):
        return mock_client(handler, myanimelist)

    async def test_a_rating_is_put_with_one_decimal(self):
        sent = []

        def handler(request: httpx.Request):
            sent.append((request.method, request.url.path, request.content))
            return httpx.Response(200, json={})

        with mock_client(handler):
            await mangaupdates.set_rating("t", "42", 8.49)
        self.assertEqual(sent[0][0], "PUT")
        self.assertEqual(sent[0][1], "/v1/series/42/rating")
        self.assertEqual(json.loads(sent[0][2]), {"rating": 8.5})

    async def test_taking_a_rating_away_is_a_delete_and_not_a_zero(self):
        # 0 is not in the range this API accepts, and it has a verb for
        # removing one.
        sent = []

        def handler(request: httpx.Request):
            sent.append((request.method, request.content))
            return httpx.Response(200, json={})

        with mock_client(handler):
            await mangaupdates.set_rating("t", "42", None)
        self.assertEqual(sent[0][0], "DELETE")
        self.assertEqual(sent[0][1], b"")

    async def test_removing_a_rating_that_was_never_there_is_not_a_failure(self):
        with mock_client(lambda request: httpx.Response(404, json={})):
            await mangaupdates.set_rating("t", "42", None)

    async def test_an_unrated_series_reads_as_none_rather_than_failing(self):
        with mock_client(lambda request: httpx.Response(404, json={})):
            self.assertIsNone(await mangaupdates.get_rating("t", "42"))

    async def test_the_editor_pays_for_a_second_request_and_a_push_does_not(self):
        # The distinction the two functions exist for.
        paths = []

        def handler(request: httpx.Request):
            paths.append(request.url.path)
            if request.url.path.endswith("/rating"):
                return httpx.Response(200, json={"rating": 7.5})
            return httpx.Response(200, json={"list_id": 0, "status": {"chapter": 12}})

        with mock_client(handler):
            await mangaupdates.get_progress("t", "42")
            after_push = list(paths)
            state = await mangaupdates.get_state("t", "42")
        self.assertEqual(after_push, ["/v1/lists/series/42"])
        self.assertEqual(paths[1:], ["/v1/lists/series/42", "/v1/series/42/rating"])
        self.assertEqual((state.chapter, state.score), (12.0, 7.5))

    async def test_a_rating_survives_the_entry_being_on_no_list(self):
        # Two resources, and only one of them has to exist.
        def handler(request: httpx.Request):
            if request.url.path.endswith("/rating"):
                return httpx.Response(200, json={"rating": 9.0})
            return httpx.Response(404, json={})

        with mock_client(handler):
            state = await mangaupdates.get_state("t", "42")
        self.assertEqual(state.score, 9.0)
        self.assertIsNone(state.chapter)

    async def test_neither_resource_existing_is_still_none(self):
        with mock_client(lambda request: httpx.Response(404, json={})):
            self.assertIsNone(await mangaupdates.get_state("t", "42"))

    async def test_myanimelist_reads_its_rating_off_the_same_record(self):
        paths = []

        def handler(request: httpx.Request):
            paths.append(request.url.path)
            return httpx.Response(200, json={"id": 7, "my_list_status": {
                "num_chapters_read": 3, "status": "completed", "score": 9}})

        with self.mal(handler):
            state = await myanimelist.get_state("t", "7")
        self.assertEqual(len(paths), 1)
        self.assertEqual((state.score, state.status), (9.0, "completed"))

    async def test_an_unrated_entry_is_none_and_not_a_rating_of_zero(self):
        # 0 is how MAL says "unrated". Carried through as a number it would
        # read as a rating, and the editor would offer to re-send it.
        payload = {"id": 7, "my_list_status": {"num_chapters_read": 1, "score": 0}}
        with self.mal(lambda request: httpx.Response(200, json=payload)):
            self.assertIsNone((await myanimelist.get_state("t", "7")).score)

    async def test_a_half_score_rounds_up_and_not_to_even(self):
        # round() is half-to-even: it turns 8.5 into 8 and 9.5 into 10.
        # Two identical gestures landing on different sides is not
        # something anybody would guess from the box they typed into.
        for given, expected in ((8.5, "9"), (9.5, "10"), (7.4, "7"), (7.6, "8")):
            with self.subTest(given=given):
                sent = []

                def handler(request: httpx.Request):
                    if request.method == "GET":
                        return httpx.Response(200, json={})
                    sent.append(request.content.decode())
                    return httpx.Response(200, json={})

                with self.mal(handler):
                    await myanimelist.set_state("t", "7", score=given, remote=None)
                self.assertEqual(urllib.parse.parse_qs(sent[0])["score"], [expected])

    async def test_a_number_that_is_not_a_rating_is_none_rather_than_clamped(self):
        # A 50 typed into a box that takes ten is somebody meaning
        # something else, and recording 10 would report a rating nobody
        # gave.
        for value in (0, -1, 11, 50, "", "eight", None):
            with self.subTest(value=value):
                self.assertIsNone(models.score_in_range(value))
        self.assertEqual(models.score_in_range("8.5"), 8.5)
        self.assertEqual(models.score_in_range(10), 10.0)

    async def test_nothing_asked_for_is_no_request_at_all(self):
        # The guard that keeps a rewrite of the record it already holds
        # from happening for nothing.
        seen = []
        with self.mal(lambda request: (seen.append(1), httpx.Response(200, json={}))[1]):
            await myanimelist.set_state("t", "7", remote=None)
        with mock_client(lambda request: (seen.append(1), httpx.Response(200, json={}))[1]):
            await mangaupdates.set_state("t", "7", remote=None)
        self.assertEqual(seen, [])


class TypingTheNumbersInYourself(_Case):
    """`send_manually` is the one path in the app that can walk a tracker
    backwards, and the only one that asks first. Everything automatic
    refuses to, because it cannot tell a correction from a mistake."""

    @staticmethod
    def _recording(sent, *, chapter=None, volume=None, list_id=0):
        def handler(request: httpx.Request):
            if request.url.path.startswith("/v1/lists/series/") and request.method == "GET":
                status = {}
                if chapter is not None:
                    status["chapter"] = chapter
                if volume is not None:
                    status["volume"] = volume
                return httpx.Response(200, json={"list_id": list_id, "status": status})
            sent.append((request.method, request.url.path, request.content))
            return httpx.Response(200, json={})
        return handler

    async def _link_row(self):
        return await (await self.db.execute(
            "SELECT * FROM tracker_links WHERE user_id = 1")).fetchone()

    async def test_a_lower_number_than_the_service_holds_is_sent_anyway(self):
        # The whole reason the editor exists: the automatic path got it
        # wrong, and nothing else in the app can put it back.
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        sent = []
        with mock_client(self._recording(sent, volume=40)):
            outcome = await service.send_manually(
                1, MANGAUPDATES, series_id=1, volume=3)
        self.assertEqual(outcome, "sent")
        self.assertEqual(json.loads(sent[0][2])[0]["status"], {"volume": 3})
        self.assertEqual((await self._link_row())["last_pushed_volume"], 3)

    async def test_an_automatic_push_in_the_same_position_refuses(self):
        # The control: the never-regress rule is still there, and it is
        # only the typing that gets past it.
        await self.series()
        await self.item(1, 3.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        sent = []
        with mock_client(self._recording(sent, volume=40)):
            self.assertEqual(await service.push(1, series_id=1), "remote-ahead")
        self.assertEqual(sent, [])

    async def test_a_score_and_a_finish_go_out_together(self):
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            await service.send_manually(
                1, MANGAUPDATES, series_id=1, volume=12, score=8.5,
                status=str(mangaupdates.COMPLETE_LIST))
        methods = [(method, path) for method, path, _ in sent]
        self.assertEqual(methods, [
            ("POST", "/v1/lists/series/update"), ("PUT", "/v1/series/99/rating")])
        self.assertEqual(json.loads(sent[0][2])[0]["list_id"], mangaupdates.COMPLETE_LIST)
        self.assertEqual(json.loads(sent[1][2]), {"rating": 8.5})

    async def test_a_status_picked_by_hand_is_not_undone_by_the_next_push(self):
        # The shelf here is "reading"; somebody puts the entry on hold on
        # MangaUpdates from the editor. What is recorded is the SHELF's
        # status, so the next page turn has nothing new to say about it --
        # recording the pick instead would have that push put "reading"
        # straight back.
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self.db.execute(
            "INSERT INTO series_status (user_id, series_id, status) VALUES (1, 1, 'reading')")
        await self.db.commit()
        sent = []
        with mock_client(self._recording(sent, volume=1)):
            await service.send_manually(
                1, MANGAUPDATES, series_id=1, status=str(mangaupdates.ON_HOLD_LIST))
            self.assertEqual((await self._link_row())["last_pushed_status"], "reading")
            # (It still reads the service once, to agree on the volume it
            # has never recorded -- a read, not a write.)
            self.assertNotEqual(await service.push(1, series_id=1), "sent")
        self.assertEqual([json.loads(body)[0]["list_id"] for _, _, body in sent],
                         [mangaupdates.ON_HOLD_LIST])

    async def test_leaving_the_status_alone_moves_no_list(self):
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        sent = []
        with mock_client(self._recording(sent, list_id=mangaupdates.ON_HOLD_LIST)):
            await service.send_manually(1, MANGAUPDATES, series_id=1, volume=2)
        self.assertEqual(json.loads(sent[0][2])[0]["list_id"], mangaupdates.ON_HOLD_LIST)

    async def test_a_refusal_lands_on_the_link_rather_than_raising(self):
        await self.series()
        await self.connect()
        await self.link(series_id=1)

        def handler(request: httpx.Request):
            if request.method == "GET":
                return httpx.Response(404, json={})
            return httpx.Response(503, json={})

        with mock_client(handler):
            self.assertEqual(
                await service.send_manually(1, MANGAUPDATES, series_id=1, volume=1), "failed")
        self.assertIn("503", (await self._link_row())["last_error"])

    async def test_nothing_is_sent_for_something_that_is_not_linked(self):
        await self.series()
        await self.connect()
        seen = []
        with mock_client(lambda request: (seen.append(1), httpx.Response(200, json={}))[1]):
            self.assertEqual(
                await service.send_manually(1, MANGAUPDATES, series_id=1, volume=5), "unlinked")
        self.assertEqual(seen, [])


class ShelvesFiledBeforeThisExisted(_Case):
    """Nothing revisits a link, so a series finished -- or put on hold --
    last year would go on being reported as "reading" on both sites
    forever. Same two rules as every other startup backfill: cheap once it
    has run, no-op on the second start."""

    async def _finish(self, *, series_id=None, novel_id=None, user_id=1, status="completed"):
        table, column = (
            ("series_status", "series_id") if series_id is not None
            else ("novel_status", "novel_id")
        )
        await self.db.execute(
            f"INSERT INTO {table} (user_id, {column}, status) VALUES (?, ?, ?)",
            (user_id, series_id if series_id is not None else novel_id, status))
        await self.db.commit()

    @staticmethod
    def _recording(sent):
        def handler(request: httpx.Request):
            if request.url.path.startswith("/v1/lists/series/") and request.method == "GET":
                return httpx.Response(200, json={"list_id": 0, "status": {}})
            sent.append(request.content)
            return httpx.Response(200, json={})
        return handler

    async def test_it_reports_a_link_that_was_never_told(self):
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            self.assertEqual(await service.backfill_statuses(pause=0), 1)
        self.assertEqual(json.loads(sent[0])[0]["list_id"], mangaupdates.COMPLETE_LIST)

    async def test_the_second_start_does_nothing_and_asks_nothing(self):
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            await service.backfill_statuses(pause=0)
            after_first = len(sent)
            self.assertEqual(await service.backfill_statuses(pause=0), 0)
        self.assertEqual((after_first, len(sent)), (1, 1))

    async def test_a_shelf_with_no_status_is_not_in_the_answer(self):
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            self.assertEqual(await service.backfill_statuses(pause=0), 0)
        self.assertEqual(sent, [])

    async def test_a_shelf_on_hold_is_reported_too(self):
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1, status="on_hold")
        sent = []
        with mock_client(self._recording(sent)):
            self.assertEqual(await service.backfill_statuses(pause=0), 1)
        self.assertEqual(json.loads(sent[0])[0]["list_id"], mangaupdates.ON_HOLD_LIST)

    async def test_a_status_the_service_has_no_word_for_costs_nothing(self):
        # "Stopped reading" stays unreported for good, so it would be in
        # the query's answer on every start: it is skipped before a push,
        # rather than costing a push, a connection and a pause each boot.
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        await self._finish(series_id=1, status="stopped_reading")
        seen = []
        with mock_client(lambda request: (seen.append(1), httpx.Response(200, json={}))[1]), \
                patch.object(service, "push") as push:
            self.assertEqual(await service.backfill_statuses(pause=0), 0)
        push.assert_not_called()
        self.assertEqual(seen, [])

    async def test_a_finished_novel_is_in_the_answer_too(self):
        await self.novel()
        await self.read_chapter(101)
        await self.connect()
        await self.link(novel_id=1)
        await self._finish(novel_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            self.assertEqual(await service.backfill_statuses(pause=0), 1)

    async def test_one_signed_out_account_stops_it_asking_that_account_again(self):
        # Otherwise a signed-out reader with three hundred finished series
        # produces three hundred doomed requests on every start.
        await self.series(series_id=1)
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path, number_kind) "
            "VALUES (2, 1, 'S2', 's2', 's2', 'volume')")
        await self.db.commit()
        await self.link(series_id=1)
        await self.link(series_id=2)
        await self._finish(series_id=1)
        await self._finish(series_id=2)
        seen = []
        with mock_client(lambda request: (seen.append(1), httpx.Response(200, json={}))[1]):
            self.assertEqual(await service.backfill_statuses(pause=0), 0)
        self.assertEqual(seen, [])


class MarkingItFinishedTellsTheTrackers(_Case):
    """The status badge is where a reader files a shelf, and every status
    a catalogue has a word for is reported from it. Hooking only the
    backfill would leave every future change silent."""

    @staticmethod
    def _request(form):
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/series/1/status", "raw_path": b"/series/1/status",
            "query_string": b"", "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"hx-request", b"true"),
                        (b"content-type", b"application/x-www-form-urlencoded")],
        }
        body = urllib.parse.urlencode(form).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(scope, receive)

    @staticmethod
    def _recording(sent):
        def handler(request: httpx.Request):
            if request.url.path.startswith("/v1/lists/series/") and request.method == "GET":
                return httpx.Response(200, json={"list_id": 0, "status": {}})
            sent.append(request.content)
            return httpx.Response(200, json={})
        return handler

    async def _set_series(self, status):
        from app.library.router import set_series_status
        sent = []
        with mock_client(self._recording(sent)):
            await set_series_status(
                1, self._request({"status": status}), user={"id": 1}, db=self.db)
            await service.drain()
        return sent

    async def test_finishing_a_series_from_the_badge_reports_it(self):
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        sent = await self._set_series("completed")
        self.assertEqual(json.loads(sent[0])[0]["list_id"], mangaupdates.COMPLETE_LIST)

    async def test_any_other_status_from_the_same_badge_is_reported_too(self):
        await self.series()
        await self.item(1, 1.0)
        await self.read(1)
        await self.connect()
        await self.link(series_id=1)
        sent = await self._set_series("dropped")
        self.assertEqual(json.loads(sent[0])[0]["list_id"], mangaupdates.UNFINISHED_LIST)

    async def test_clearing_the_status_queues_nothing(self):
        # The control: no status is no opinion, and the entry is left
        # wherever it was filed on the site -- not even a push to find
        # that out.
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        with patch.object(service, "queue_push") as queued:
            self.assertEqual(await self._set_series(""), [])
        queued.assert_not_called()

    async def test_finishing_a_novel_from_its_badge_reports_it_too(self):
        from app.novels.router import set_novel_status
        await self.novel()
        await self.read_chapter(101)
        await self.connect()
        await self.link(novel_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            await set_novel_status(
                1, self._request({"status": "completed"}), user={"id": 1}, db=self.db)
            await service.drain()
        self.assertEqual(json.loads(sent[0])[0]["list_id"], mangaupdates.COMPLETE_LIST)


class WhatTheDialogsButtonsReachFor(_Case):
    """One endpoint behind the editor, and the two things it can get wrong
    are SCOPE: which fields it touches, and which service it touches."""

    @staticmethod
    def _request():
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/tracking/mangaupdates/send", "raw_path": b"/tracking/mangaupdates/send",
            "query_string": b"", "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"hx-request", b"true")],
        }
        return Request(scope)

    @staticmethod
    def _recording(sent, *, chapter=12, volume=3):
        def handler(request: httpx.Request):
            if request.method == "GET":
                if request.url.path.endswith("/rating"):
                    return httpx.Response(404, json={})
                return httpx.Response(200, json={
                    "list_id": 0, "status": {"chapter": chapter, "volume": volume}})
            sent.append((request.url.path, request.content))
            return httpx.Response(200, json={})
        return handler

    async def test_an_empty_box_leaves_that_field_alone(self):
        # The difference between "leave this alone" and "set it to
        # nothing". Reading an empty chapter box as 0 would tell the
        # service the reader had read none of them -- which is the exact
        # damage the never-regress rule exists to prevent, arriving by the
        # one door that is allowed past it.
        from app.tracking.router import send_to_tracker
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        sent = []
        with mock_client(self._recording(sent)):
            await send_to_tracker(
                MANGAUPDATES, self._request(), series_id=1, novel_id=None,
                chapter="", volume="5", score="", status="", start_date="", finish_date="",
                user={"id": 1}, db=self.db)
        status = json.loads(sent[0][1])[0]["status"]
        self.assertEqual(status, {"volume": 5})
        self.assertNotIn("chapter", status)
        # And an empty score box is not a rating of zero, which is how a
        # rating gets REMOVED on both services.
        self.assertEqual([path for path, _ in sent], ["/v1/lists/series/update"])

    async def test_a_filled_box_is_sent_even_when_it_is_lower(self):
        # The control for the line above: empty is what is ignored, not
        # small. Without this, "leave it alone" and the never-regress rule
        # look identical from outside.
        from app.tracking.router import send_to_tracker
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        sent = []
        with mock_client(self._recording(sent, chapter=99)):
            await send_to_tracker(
                MANGAUPDATES, self._request(), series_id=1, novel_id=None,
                chapter="2", volume="", score="", status="", start_date="", finish_date="",
                user={"id": 1}, db=self.db)
        self.assertEqual(json.loads(sent[0][1])[0]["status"], {"chapter": 2})

    async def _link_both(self):
        await self.series()
        await self.item(1, 4.0)
        await self.read(1)
        await self.connect()
        await store.save_account(self.db, 1, MYANIMELIST, access_token="mal", username="r")
        await store.save_link(self.db, 1, MYANIMELIST, series_id=1, remote_id="7",
                              remote_title="R", remote_url="https://mal/7")
        await self.db.commit()
        await self.link(series_id=1)

    async def test_a_narrowed_push_reaches_only_that_service(self):
        # `tracker=` is what keeps the push that follows a Link to the
        # thing just linked: the screen says "Link" under one service's
        # search results, and writing to the other one as well is a side
        # effect nothing named.
        await self._link_both()
        mu, mal = [], []

        def mu_handler(request: httpx.Request):
            if request.method == "GET":
                if request.url.path.endswith("/rating"):
                    return httpx.Response(404, json={})
                return httpx.Response(200, json={"list_id": 0, "status": {}})
            mu.append(request.content)
            return httpx.Response(200, json={})

        def mal_handler(request: httpx.Request):
            if request.method == "GET":
                return httpx.Response(200, json={"id": 7, "my_list_status": {}})
            mal.append(request.content)
            return httpx.Response(200, json={})

        with mock_client(mu_handler), mock_client(mal_handler, myanimelist):
            await service.push(1, series_id=1, novel_id=None, tracker=MANGAUPDATES)
        self.assertEqual(len(mu), 1)
        self.assertEqual(mal, [])

    async def test_linking_one_service_does_not_write_to_the_other(self):
        # The push that follows a Link is about the thing just linked. It
        # reaches for the same narrowing the button beside it does: the
        # screen says "Link" under one service's search results, and
        # writing to the other one as well is a side effect nothing named.
        from app.tracking.router import link_series
        await self.series()
        await self.item(1, 4.0)
        await self.read(1)
        await self.connect()
        await store.save_account(self.db, 1, MYANIMELIST, access_token="mal", username="r")
        await store.save_link(self.db, 1, MYANIMELIST, series_id=1, remote_id="7",
                              remote_title="R", remote_url="https://mal/7")
        await self.db.commit()
        mu, mal = [], []

        def mu_handler(request: httpx.Request):
            if request.method == "GET":
                if request.url.path.endswith("/rating"):
                    return httpx.Response(404, json={})
                if request.url.path.startswith("/v1/series/"):
                    return httpx.Response(200, json={"description": "A town."})
                return httpx.Response(404, json={})
            mu.append(request.content)
            return httpx.Response(200, json={})

        def mal_handler(request: httpx.Request):
            if request.method == "GET":
                return httpx.Response(200, json={"id": 7, "my_list_status": {}})
            mal.append(request.content)
            return httpx.Response(200, json={})

        with mock_client(mu_handler), mock_client(mal_handler, myanimelist):
            await link_series(
                MANGAUPDATES, self._request(), remote_id="99", remote_title="Q",
                remote_url="https://mu/99", series_id=1, novel_id=None,
                user={"id": 1}, db=self.db)
            await service.drain()
        self.assertTrue(mu)
        self.assertEqual(mal, [])

    async def test_the_automatic_push_still_reaches_both(self):
        # The control: narrowing is what the BUTTON does, not what the
        # push does. Everything automatic leaves it None and tells them
        # all, which is the whole reason `links_for` is a loop.
        await self._link_both()
        mu, mal = [], []

        def mu_handler(request: httpx.Request):
            if request.method == "GET":
                return httpx.Response(200, json={"list_id": 0, "status": {}})
            mu.append(request.content)
            return httpx.Response(200, json={})

        def mal_handler(request: httpx.Request):
            if request.method == "GET":
                return httpx.Response(200, json={"id": 7, "my_list_status": {}})
            mal.append(request.content)
            return httpx.Response(200, json={})

        with mock_client(mu_handler), mock_client(mal_handler, myanimelist):
            self.assertEqual(await service.push(1, series_id=1), "sent")
        self.assertEqual((len(mu), len(mal)), (1, 1))

    async def test_the_backfill_stops_asking_about_a_link_it_has_told(self):
        # The query is what makes the second start cost nothing. The push
        # behind it would no-op anyway, so without this the backfill would
        # simply be the same wasted walk over every finished shelf, one
        # database connection at a time, on every boot -- invisible from
        # the outside, which is why it is pinned here rather than through
        # the requests it does not make.
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        await self.db.execute(
            "INSERT INTO series_status (user_id, series_id, status) VALUES (1, 1, 'completed')")
        await self.db.commit()
        self.assertEqual(len(await store.links_status_unreported(self.db)), 1)
        await self.db.execute(
            "UPDATE tracker_links SET last_pushed_status = 'completed' WHERE user_id = 1")
        await self.db.commit()
        self.assertEqual(await store.links_status_unreported(self.db), [])
        # And it asks again the moment the shelf's status CHANGES: each
        # change is news, not only the first one.
        await self.db.execute("UPDATE series_status SET status = 'on_hold'")
        await self.db.commit()
        [row] = await store.links_status_unreported(self.db)
        self.assertEqual(row["local_status"], "on_hold")

    async def test_it_stops_asking_about_a_filed_novel_too(self):
        # The UNION's other half, which the series case cannot reach.
        await self.novel()
        await self.connect()
        await self.link(novel_id=1)
        await self.db.execute(
            "INSERT INTO novel_status (user_id, novel_id, status) VALUES (1, 1, 'completed')")
        await self.db.commit()
        self.assertEqual(len(await store.links_status_unreported(self.db)), 1)
        await self.db.execute(
            "UPDATE tracker_links SET last_pushed_status = 'completed' WHERE user_id = 1")
        await self.db.commit()
        self.assertEqual(await store.links_status_unreported(self.db), [])


class TheEditorArrivesFilledInWhicheverWayYouReachIt(_Case):
    """Three paths end on the editor -- opening the dialog, linking and
    sending -- and every one of them has to carry what the SERVICE holds.
    Linking did not, and the boxes came up empty
    over an entry already at chapter 12 (measured in both browsers); the
    first Send then moved the list and the rating while saying nothing
    about numbers the reader could see on the site."""

    @staticmethod
    def _request():
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/tracking/mangaupdates/link", "raw_path": b"/tracking/mangaupdates/link",
            "query_string": b"", "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"hx-request", b"true")],
        }
        return Request(scope)

    @staticmethod
    def _holding(chapter=12, volume=2, rating=None):
        def handler(request: httpx.Request):
            if request.url.path.endswith("/rating"):
                return httpx.Response(404 if rating is None else 200, json={"rating": rating})
            if request.url.path.startswith("/v1/lists/series/") and request.method == "GET":
                return httpx.Response(200, json={
                    "list_id": 0, "status": {"chapter": chapter, "volume": volume}})
            if request.url.path.startswith("/v1/series/"):
                return httpx.Response(200, json={"description": "A town."})
            return httpx.Response(200, json={})
        return handler

    def _value(self, html, field):
        chunk = html.split(f'name="{field}"', 1)[1].split(">", 1)[0]
        return chunk.split('value="', 1)[1].split('"', 1)[0] if 'value="' in chunk else None

    async def test_linking_lands_on_an_editor_that_is_already_filled(self):
        from app.tracking.router import link_series
        await self.series()
        await self.connect()
        with mock_client(self._holding(rating=7.5)):
            response = await link_series(
                MANGAUPDATES, self._request(), remote_id="99", remote_title="Q",
                remote_url="https://mu/99", series_id=1, novel_id=None,
                user={"id": 1}, db=self.db)
        await service.drain()
        html = response.template.render(response.context)
        self.assertEqual(self._value(html, "chapter"), "12")
        self.assertEqual(self._value(html, "volume"), "2")
        self.assertEqual(self._value(html, "score"), "7.5")

    async def test_opening_the_dialog_lands_on_one_that_is_filled_too(self):
        from app.tracking.router import open_panel
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        with mock_client(self._holding(chapter=41, volume=3)):
            response = await open_panel(
                MANGAUPDATES, self._request(), series_id=1, novel_id=None,
                user={"id": 1}, db=self.db)
        html = response.template.render(response.context)
        self.assertEqual(self._value(html, "chapter"), "41")

    async def test_and_so_does_the_editors_own_send(self):
        from app.tracking.router import send_to_tracker
        await self.series()
        await self.connect()
        await self.link(series_id=1)
        with mock_client(self._holding(chapter=5, volume=1)):
            response = await send_to_tracker(
                MANGAUPDATES, self._request(), series_id=1, novel_id=None,
                chapter="5", volume="", score="", status="", start_date="", finish_date="",
                user={"id": 1}, db=self.db)
        html = response.template.render(response.context)
        self.assertEqual(self._value(html, "chapter"), "5")


class TheEditorsTwoButtons(unittest.TestCase):
    """Update sends the boxes; Cancel sends nothing and shuts the dialog.

    What was here before was "Send these" and "Send what is read here" --
    two SENDS side by side, with no way out of a half-edited editor but
    Escape. The second one was the automatic push on demand, and the
    automatic push already runs on every mark-read and every shelf
    finished; the "Read here: volume 3, chapter 41" line above the boxes
    is what to copy into them when one of those did not land.
    """

    def setUp(self):
        self.markup = (ROOT / "app/templates/_tracker_panel.html").read_text()

    def test_the_send_button_says_update(self):
        self.assertIn(">Update</button>", self.markup)
        self.assertNotIn("Send these", self.markup)

    def test_cancel_cannot_submit_the_form_it_sits_in(self):
        # `type="button"` is the whole guarantee: htmx binds to the form's
        # submit event, so a default-type button inside it would POST the
        # boxes -- the exact opposite of what the word says.
        chunk = self.markup.split(">Cancel</button>", 1)[0].rsplit("<button", 1)[1]
        self.assertIn('type="button"', chunk)
        self.assertIn("data-tracker-close", chunk)

    def test_the_push_button_and_its_endpoint_are_both_gone(self):
        # Half of this removal would leave an endpoint nothing reaches, or
        # a button pointing at a 404.
        self.assertNotIn("/push", self.markup)
        self.assertNotIn("push_now", (ROOT / "app/tracking/router.py").read_text())

    def test_the_editor_still_has_something_that_sends(self):
        # Control for the two above: a panel that lost its submit button
        # would pass every NotIn in this class.
        self.assertIn('action="/tracking/{{ tracker.name }}/send"', self.markup)


class SwitchingServiceDoesNotShowTheOtherOnesNumbers(unittest.TestCase):
    """Both icons swap the SAME element, and the panel behind it takes
    two calls to a catalogue to build (a list read and a rating read --
    see service.remote_state). Opening MyAnimeList after MangaUpdates
    therefore left MangaUpdates' chapter, volume and score on screen for
    about a second, under a heading that had not arrived yet.

    Stale numbers in an editor are not a loading state: they are
    plausible, and Update sends whatever is in the boxes."""

    def setUp(self):
        self.js = code_of(ROOT / "app/static/js/tracker-toggle.js")

    def test_the_icon_empties_the_panel_before_the_answer_arrives(self):
        opener = self.js.split("data-tracker-dialog", 1)[1].split("data-tracker-close", 1)[0]
        self.assertIn("emptyPanel(", opener)

    def test_nothing_else_that_swaps_the_panel_does(self):
        # A search, a link, a send and an unlink all swap the panel of the
        # service already on screen -- blanking for those flashes the
        # dialog for nothing. Only the icon can change which service it is.
        self.assertEqual(self.js.count("emptyPanel("), 2)  # the call and the definition

    def test_the_placeholder_names_the_service_it_is_waiting_for(self):
        self.assertIn("dataset.trackerLabel", self.js)
        self.assertIn("data-tracker-label", (ROOT / "app/templates/_tracker_icon.html").read_text())

    def test_the_way_out_of_the_dialog_survives_the_blanking(self):
        # A modal dialog with its contents emptied has no ✕, and the
        # pointer then has nowhere to press at all. The button is moved
        # rather than rebuilt, so there is one copy of that markup.
        self.assertIn('panel.querySelector("[data-tracker-close]")', self.js)
        blanking = self.js.split("panel.textContent", 1)[1]
        self.assertIn("if (closer) row.append(closer);", blanking)


class TheEventThatSaysThePanelHasLanded(unittest.TestCase):
    """htmx 4 aims `htmx:after:swap` at the element that TRIGGERED the
    request -- the icon -- and not at what was swapped in, so a handler
    matching on the target's id finds nothing and fails silently.

    Measured in both browsers: the events arrive as after:request,
    before:swap, after:settle, after:process (target #tracker-panel),
    after:swap (target the icon). `after:process` is the one that names
    the panel, and with it the search box takes focus; without it
    document.activeElement stayed the dialog, which is what showModal()
    leaves focused when nothing inside asks for it."""

    def test_the_panel_is_found_through_the_event_that_names_it(self):
        js = code_of(ROOT / "app/static/js/tracker-toggle.js")
        self.assertIn('"htmx:after:process"', js)
        self.assertIn('event.target?.id !== "tracker-panel"', js)

    def test_it_does_not_go_back_to_the_one_that_does_not(self):
        # The control, and the reason this test exists rather than a
        # comment: the wrong event is the obvious choice, reads correctly,
        # and does nothing.
        js = code_of(ROOT / "app/static/js/tracker-toggle.js")
        self.assertNotIn("after:swap", js)
