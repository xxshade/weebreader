"""What a reader is allowed to post as its position.

The readers save progress as JSON, and for a long time those values went
from the wire into `reading_progress` untouched. SQLite is dynamically
typed, so a REAL column does not refuse a string -- it STORES it. Every
screen that renders a percentage then raised on the row, and the home page
and the "Reading" strip answered 500 from that moment on, while /stats and
/settings stayed up. One POST, and the app's main screen was down until
somebody edited the database by hand.

So these are not tests about tidy input. They are about the blast radius of
one bad write, and they assert both halves: the request is refused, AND
nothing was written -- because it is the row, not the request, that does
the damage.
"""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.novels.router import save_novel_progress
from app.reader.router import save_progress


class _JsonRequest(Request):
    """A request whose body is whatever was handed in -- a dict to be
    encoded, or raw bytes for the malformed cases."""

    def __init__(self, body):
        super().__init__({
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
            "headers": [], "client": ("test", 1), "server": ("test", 80),
        })
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    async def body(self):
        return self._raw


class ProgressCase(unittest.IsolatedAsyncioTestCase):
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
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'L', '/l', 'comic')")
        await self.db.execute(
            "INSERT INTO items (id, library_id, title, sort_title, file_path, file_format,"
            " file_hash, file_size, page_count) VALUES (1, 1, 'T', 't', '/l/a.cbz', 'cbz', 'h', 1, 10)")
        await self.db.execute("INSERT INTO novels (id, title, source_url) VALUES (1, 'N', 'https://x/1')")
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (1, 1, 1)")
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url)"
            " VALUES (1, 1, 1, 1.0, 'C', 'https://x/c1')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def stored(self):
        return await (await self.db.execute(
            "SELECT status, progress_percent, current_page FROM reading_progress")).fetchone()

    async def post(self, body, item_id=1):
        return await save_progress(item_id, _JsonRequest(body), self.user, self.db)


class WhatTheItemReaderMaySave(ProgressCase):
    async def test_a_percentage_that_is_not_a_number_is_refused_and_not_stored(self):
        # THE bug. `progress_percent: "abc"` was accepted by the REAL
        # column as text, and `{{ progress.progress_percent|round }}` then
        # raised on every render of the home page and the reading strip.
        with self.assertRaises(HTTPException) as caught:
            await self.post({"status": "reading", "progress_percent": "abc"})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("progress_percent", caught.exception.detail)
        self.assertIsNone(await self.stored())

    async def test_a_status_outside_the_vocabulary_is_a_400_not_a_500(self):
        # It used to reach the CHECK constraint, which is the right
        # outcome (the row is refused) arriving in the wrong shape: an
        # IntegrityError, i.e. a 500.
        with self.assertRaises(HTTPException) as caught:
            await self.post({"status": "bogus", "progress_percent": 50})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIsNone(await self.stored())

    async def test_an_item_that_does_not_exist_is_a_404(self):
        # The foreign key would refuse it anyway -- as a 500.
        with self.assertRaises(HTTPException) as caught:
            await self.post({"status": "reading", "progress_percent": 10}, item_id=99999)
        self.assertEqual(caught.exception.status_code, 404)

    async def test_a_body_that_is_not_json_is_a_400(self):
        with self.assertRaises(HTTPException) as caught:
            await self.post(b"not json at all")
        self.assertEqual(caught.exception.status_code, 400)

    async def test_a_json_body_that_is_not_an_object_is_a_400(self):
        # Valid JSON, and `.get` on it is an AttributeError.
        with self.assertRaises(HTTPException) as caught:
            await self.post([1, 2, 3])
        self.assertEqual(caught.exception.status_code, 400)

    async def test_a_page_index_that_is_not_an_integer_is_a_400(self):
        with self.assertRaises(HTTPException) as caught:
            await self.post({"status": "reading", "progress_percent": 5, "current_page": "x"})
        self.assertEqual(caught.exception.status_code, 400)

    async def test_an_out_of_range_percentage_is_clamped_rather_than_refused(self):
        # Clamped and not rejected, unlike `ago` in stats/recording.py: a
        # percentage out of range is a miscount of something that did
        # happen, so the nearest true answer exists. 99999 used to be
        # stored as-is and rendered as `style="width: 99999%"`.
        await self.post({"status": "reading", "progress_percent": 99999})
        self.assertEqual((await self.stored())["progress_percent"], 100)
        await self.post({"status": "reading", "progress_percent": -40})
        self.assertEqual((await self.stored())["progress_percent"], 0)

    async def test_an_ordinary_save_still_lands(self):
        await self.post({"status": "reading", "progress_percent": 42.5, "current_page": 3})
        row = await self.stored()
        self.assertEqual((row["status"], row["progress_percent"], row["current_page"]),
                         ("reading", 42.5, 3))


class WhatTheNovelReaderMaySave(ProgressCase):
    async def novel_post(self, body):
        return await save_novel_progress(1, _JsonRequest(body), self.user, self.db)

    async def novel_stored(self):
        return await (await self.db.execute(
            "SELECT chapter_id, scroll_percent, chapter_percent FROM novel_reading_progress"
        )).fetchone()

    async def test_a_percentage_that_is_not_a_number_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            await self.novel_post({"chapter_id": 1, "scroll_percent": "abc", "chapter_percent": 5})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIsNone(await self.novel_stored())

    async def test_a_chapter_from_another_novel_is_a_404(self):
        # The foreign key only says the chapter exists; the progress row is
        # keyed by (user, novel) with the chapter hanging off it, so a
        # chapter belonging elsewhere would record a position this novel
        # can never resume from.
        await self.db.execute("INSERT INTO novels (id, title, source_url) VALUES (2, 'O', 'https://x/2')")
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (2, 2, 1)")
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url)"
            " VALUES (99, 2, 2, 1.0, 'C', 'https://x/other')")
        await self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            await self.novel_post({"chapter_id": 99, "scroll_percent": 5, "chapter_percent": 5})
        self.assertEqual(caught.exception.status_code, 404)

    async def test_fragment_position_survives_save_and_legacy_client_resets_offset(self):
        from app.novels.router import read_novel_progress
        import json
        for offset in (37, 94, None):
            body = {"chapter_id": 1, "paragraph_index": 7, "scroll_percent": 12.5}
            if offset is not None:
                body["character_offset"] = offset
            await self.novel_post(body)
            row = await (await self.db.execute("SELECT paragraph_index,character_offset FROM novel_reading_progress")).fetchone()
            self.assertEqual(tuple(row), (7, offset or 0))
            result = json.loads((await read_novel_progress(1, self.user, self.db)).body)
            self.assertEqual(result["position"][1:], [7, offset or 0])

    async def test_invalid_fragment_offset_does_not_replace_progress(self):
        await self.novel_post({"chapter_id": 1, "paragraph_index": 7, "character_offset": 37})
        for value in (1.5, True, "oops", float("inf")):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                await self.novel_post({"chapter_id": 1, "paragraph_index": 7, "character_offset": value})
        row = await (await self.db.execute("SELECT character_offset FROM novel_reading_progress")).fetchone()
        self.assertEqual(row[0], 37)

    async def test_an_ordinary_save_still_lands(self):
        await self.novel_post({"chapter_id": 1, "scroll_percent": 12.5,
                               "chapter_percent": 80, "paragraph_index": 7})
        row = await self.novel_stored()
        self.assertEqual((row["chapter_id"], row["scroll_percent"], row["chapter_percent"]),
                         (1, 12.5, 80))


if __name__ == "__main__":
    unittest.main()
