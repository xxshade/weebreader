"""Where a work is in its PUBLICATION -- a different question from where
the reader is in it, and stored differently for that reason: the tracking
status is a row per user, this is a column on the series, because whether
a manga is still running is a fact about the manga.

The two vocabularies both contain "Completed", for different reasons, so
most of what is worth pinning here is that they cannot be used for each
other.
"""
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.datastructures import FormData
from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import home, series_card, set_series_publication_status
from app.novels.router import set_novel_publication_status
from app.status_labels import (
    _SOURCE_PUBLICATION_ALIASES, PUBLICATION_STATUS_LABELS, STATUS_LABELS,
    novel_publication_status, publication_status_from_source,
)


def _request(path="/", *, htmx=False, form=None):
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"hx-request", b"true")] if htmx else [],
        "client": ("test", 1), "server": ("test", 80),
    }
    request = Request(scope)
    if form is not None:
        # What Request.form() hands back once it has parsed a body.
        request._form = FormData(form)
    return request


class _SeriesCase(unittest.IsolatedAsyncioTestCase):
    """Setup only -- the cases below inherit it, and a base that also
    held tests would run every one of them twice."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        self.user = {"id": 1}
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'M', '/m', 'comic')")
        # Exactly how the scanner inserts one: no publication status named,
        # because the scanner has no idea and never will.
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'Serie', 'serie', '/f')")
        # A series is only on the shelf while it holds something (see
        # library/series_scope.py), so the card fixtures need one item.
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size) VALUES"
            " (1, 1, 1, 'V1', 'v1', '/f/v1.cbz', 'cbz', 'h1', 1)")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def _stored(self):
        row = await (
            await self.db.execute("SELECT publication_status FROM series WHERE id = 1")
        ).fetchone()
        return row["publication_status"]

    async def _cards(self):
        response = await home(_request(), self.user, self.db)
        return {c["name"]: c for c in response.context["cards"]}


class PublicationStatus(_SeriesCase):
    async def test_a_series_the_scanner_made_is_ongoing(self):
        # The default has to live in the COLUMN, not in a template or in
        # the endpoint: nothing on the scanner's path knows this field
        # exists, and it is the scanner that creates almost every series.
        self.assertEqual(await self._stored(), "ongoing")

    async def test_the_home_card_carries_it(self):
        self.assertEqual((await self._cards())["Serie"]["publication_status"], "ongoing")

    async def test_the_filter_offers_every_value_a_card_can_wear(self):
        # The filter is built from this dict and matched against the card's
        # own value, so the two coming apart means a card that no selection
        # can show.
        response = await home(_request(), self.user, self.db)
        self.assertEqual(set(response.context["publication_labels"]),
                         set(PUBLICATION_STATUS_LABELS))

    async def test_it_can_be_set(self):
        for value in PUBLICATION_STATUS_LABELS:
            await set_series_publication_status(
                1, _request(htmx=True, form={"publication_status": value}), self.user, self.db)
            self.assertEqual(await self._stored(), value)
            self.assertEqual((await self._cards())["Serie"]["publication_status"], value)

    async def test_a_tracking_status_is_not_a_publication_status(self):
        # Both lists say "completed"; nothing else overlaps, and the
        # endpoint must not accept the rest just because they are statuses.
        for value in STATUS_LABELS:
            if value in PUBLICATION_STATUS_LABELS:
                continue
            with self.assertRaises(HTTPException) as caught:
                await set_series_publication_status(
                    1, _request(htmx=True, form={"publication_status": value}), self.user, self.db)
            self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(await self._stored(), "ongoing")

    async def test_nonsense_is_refused_rather_than_stored(self):
        # This is the ONLY thing guarding the column on an upgraded
        # database: SQLite cannot add the CHECK by ALTER TABLE, so it
        # exists only where schema.sql created the table.
        for value in ("", "   ", "Ongoing!", "ongoing; DROP"):
            with self.assertRaises(HTTPException) as caught:
                await set_series_publication_status(
                    1, _request(htmx=True, form={"publication_status": value}), self.user, self.db)
            self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(await self._stored(), "ongoing")

    async def test_setting_it_on_a_series_that_is_not_there(self):
        with self.assertRaises(HTTPException) as caught:
            await set_series_publication_status(
                99, _request(htmx=True, form={"publication_status": "hiatus"}), self.user, self.db)
        self.assertEqual(caught.exception.status_code, 404)

    async def test_the_card_sse_inserts_carries_it_too(self):
        # A series that arrives live is built in JS from this JSON, not
        # from the Jinja grid -- so a badge added to one and not the other
        # would appear only after a reload.
        await set_series_publication_status(
            1, _request(htmx=True, form={"publication_status": "hiatus"}), self.user, self.db)
        card = await series_card(1, self.user, self.db)
        self.assertEqual(card["publication_status"], "hiatus")
        self.assertEqual(card["publication_label"], "Hiatus")

    async def test_it_is_not_the_readers_business(self):
        # Stored on the series, so it is the same for everyone -- unlike
        # the tracking status, which is keyed by user.
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (2, 'v', 'x')")
        await self.db.commit()
        await set_series_publication_status(
            1, _request(htmx=True, form={"publication_status": "canceled"}), self.user, self.db)
        response = await home(_request(), {"id": 2}, self.db)
        card = next(c for c in response.context["cards"] if c["name"] == "Serie")
        self.assertEqual(card["publication_status"], "canceled")


class NovelsReadItFromTheSource(_SeriesCase):
    """A web novel is never asked -- its source page already says so."""

    async def _novel_card(self, source_status):
        await self.db.execute("DELETE FROM novels")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url, status) VALUES (1, 'Novel', 'https://x/1', ?)",
            (source_status,))
        await self.db.commit()
        return (await self._cards())["Novel"]

    async def test_the_sources_wording_becomes_a_badge(self):
        for source, expected in [("Ongoing", "ongoing"), ("completed", "completed"),
                                 ("On hiatus", "hiatus"), ("Cancelled", "canceled")]:
            self.assertEqual((await self._novel_card(source))["publication_status"], expected)

    async def test_wording_this_app_has_no_word_for_gets_no_badge(self):
        # Not "ongoing". A guessed label is a confident statement with
        # nothing behind it, and the source is the only thing that knows.
        for source in ("Weekly", "?", "", None):
            self.assertIsNone((await self._novel_card(source))["publication_status"])


class SourceWording(unittest.TestCase):
    def test_it_is_case_and_space_insensitive(self):
        self.assertEqual(publication_status_from_source("  ON   HIATUS "), "hiatus")
        self.assertEqual(publication_status_from_source("COMPLETED"), "completed")

    def test_an_unknown_word_is_none_rather_than_a_default(self):
        self.assertIsNone(publication_status_from_source("Seasonal"))
        self.assertIsNone(publication_status_from_source(None))
        self.assertIsNone(publication_status_from_source(""))

    def test_every_alias_lands_on_a_real_value(self):
        # Not a sample -- the whole table. This is what makes the home
        # page's publication filter able to reach every card: its options
        # are PUBLICATION_STATUS_LABELS, so a novel mapped to anything
        # outside that would wear a badge no selection could ever match
        # and would simply vanish from a filtered grid.
        for word, value in _SOURCE_PUBLICATION_ALIASES.items():
            with self.subTest(word=word):
                self.assertIn(value, PUBLICATION_STATUS_LABELS)
                self.assertEqual(publication_status_from_source(word), value)



class AWebNovelsPublicationStatus(unittest.IsolatedAsyncioTestCase):
    """A novel is normally never asked -- its source page says so. What the
    override exists for is the wording that mapping cannot read, which
    otherwise gets no badge at all."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        self.user = {"id": 1}
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url, status)"
            " VALUES (1, 'N', 'https://x/1', 'Ongoing')")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url, status)"
            " VALUES (2, 'M', 'https://x/2', 'Hibernating')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def _card(self, novel_id):
        response = await home(_request(), self.user, self.db)
        return {c["id"]: c for c in response.context["cards"] if c["kind"] == "novel"}[novel_id]

    def test_the_source_is_followed_when_nothing_was_said(self):
        self.assertEqual(novel_publication_status(None, "Ongoing"), "ongoing")

    def test_a_wording_nothing_recognises_still_gets_no_badge(self):
        # Which is the case the override exists for, and it has to stay
        # true: a guessed "ongoing" is a confident label with nothing
        # behind it.
        self.assertIsNone(novel_publication_status(None, "Hibernating"))

    def test_the_readers_answer_wins(self):
        self.assertEqual(novel_publication_status("hiatus", "Ongoing"), "hiatus")

    def test_a_value_this_vocabulary_does_not_know_falls_back(self):
        # A stale override from an older vocabulary would otherwise render
        # as an unlabelled badge in the fallback grey.
        self.assertEqual(novel_publication_status("finished-ish", "Ongoing"), "ongoing")

    async def test_setting_and_clearing_it_reaches_the_home_card(self):
        await set_novel_publication_status(
            2, _request(htmx=True, form={"publication_status": "hiatus"}), self.user, self.db)
        self.assertEqual((await self._card(2))["publication_status"], "hiatus")
        # Cleared -- back to following the source, which for this novel
        # means no badge at all.
        await set_novel_publication_status(
            2, _request(htmx=True, form={"publication_status": ""}), self.user, self.db)
        self.assertIsNone((await self._card(2))["publication_status"])

    async def test_a_tracking_status_is_not_offered_here_either(self):
        for value in STATUS_LABELS:
            if value in PUBLICATION_STATUS_LABELS:
                continue
            with self.assertRaises(HTTPException):
                await set_novel_publication_status(
                    1, _request(htmx=True, form={"publication_status": value}), self.user, self.db)
