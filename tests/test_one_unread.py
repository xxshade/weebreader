"""There is one Unread, not two (see GLOSSARY.md): an item with no
reading_progress row. Marking an item or a whole series unread deletes the
row, the progress save cannot write an 'unread' one, and migration 7 deletes
the ones "Mark all unread" used to leave behind -- which is what put the
"clear" button on every volume of a series, never-opened ones included.

The card has one way back to Unread (the ↺), on a book being read and on a
finished one, and it asks first in both, naming what is lost.
"""
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import templates
from app.reading_progress import mark_item_read, mark_item_unread, mark_series_unread
from tests.test_progress_validation import ProgressCase

ROOT = Path(__file__).resolve().parents[1]


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x'), (2, 'v', 'x')")
        await self.db.execute("INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'L', '/l', 'comic')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path) VALUES (1, 1, 'S', 's', 'S'), "
            "(2, 1, 'T', 't', 'T')")
        for item_id, series_id in ((1, 1), (2, 1), (3, 1), (4, 2)):
            await self.db.execute(
                "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path, file_format,"
                " file_hash, file_size, page_count) VALUES (?, 1, ?, ?, ?, ?, 'cbz', ?, 1, 10)",
                (item_id, series_id, f"I{item_id}", f"i{item_id}", f"/l/{item_id}.cbz", f"h{item_id}"))
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def reading(self, user_id, item_id):
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, current_page, progress_percent, last_read_at)"
            " VALUES (?, ?, 'reading', 3, 30, datetime('now'))", (user_id, item_id))

    async def rows(self):
        return {(r["user_id"], r["item_id"]): r["status"] for r in await (await self.db.execute(
            "SELECT user_id, item_id, status FROM reading_progress")).fetchall()}


class MarkingUnreadDeletesTheRow(DbCase):
    async def test_an_item(self):
        await self.reading(1, 1)
        await mark_item_read(self.db, 1, 2, 9)
        await mark_item_unread(self.db, 1, 1)
        await mark_item_unread(self.db, 1, 2)
        self.assertEqual(await self.rows(), {})

    async def test_a_series_leaves_other_series_and_other_users_alone(self):
        for user_id, item_id in ((1, 1), (1, 3), (1, 4), (2, 1)):
            await self.reading(user_id, item_id)
        await mark_item_read(self.db, 1, 2, 9)
        await mark_series_unread(self.db, 1, 1)
        self.assertEqual(await self.rows(), {(1, 4): "reading", (2, 1): "reading"})

    async def test_a_series_never_opened_gets_no_rows(self):
        # THE bug: 'unread' rows for every volume, each one a card with
        # something to "clear".
        await mark_series_unread(self.db, 1, 1)
        self.assertEqual(await self.rows(), {})


class TheReaderCannotSaveUnread(ProgressCase):
    async def test_unread_is_refused_and_not_stored(self):
        with self.assertRaises(HTTPException) as caught:
            await self.post({"status": "unread", "progress_percent": 0})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIsNone(await self.stored())


class Migration7(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))

    def seed(self, version):
        db = sqlite3.connect(settings.db_path)
        db.executescript((ROOT / "app/schema.sql").read_text())
        db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x')")
        db.execute("INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'L', '/l', 'comic')")
        for item_id in (1, 2, 3):
            db.execute(
                "INSERT INTO items (id, library_id, title, sort_title, file_path, file_format, file_hash,"
                " file_size) VALUES (?, 1, 'I', 'i', ?, 'cbz', 'h', 1)", (item_id, f"/l/{item_id}"))
        db.executemany("INSERT INTO reading_progress (user_id, item_id, status) VALUES (1, ?, ?)",
                       ((1, "unread"), (2, "reading"), (3, "completed")))
        db.execute(f"PRAGMA user_version = {version}")
        db.commit()
        db.close()

    async def statuses(self):
        db = await get_connection()
        try:
            return {r["item_id"]: r["status"] for r in await (await db.execute(
                "SELECT item_id, status FROM reading_progress")).fetchall()}
        finally:
            await db.close()

    async def test_unread_rows_go_and_the_rest_stay(self):
        self.seed(6)
        await init_db()
        self.assertEqual(await self.statuses(), {2: "reading", 3: "completed"})

    async def test_it_runs_once(self):
        self.seed(7)
        await init_db()
        self.assertEqual(await self.statuses(), {1: "unread", 2: "reading", 3: "completed"})


class TheCardsButtons(unittest.TestCase):
    def render(self, status):
        item = {"id": 7, "title": "Vol 1", "metadata": None, "cover_path": None,
                "page_count": 10, "file_format": "cbz", "manual_order": None}
        progress = {7: {"status": status, "progress_percent": 30, "current_page": 3}} if status else {}
        series = {"id": 1, "manual_sort": 0, "manual_sort_editing": 0}
        return templates.env.get_template("library/_item_card.html").render(
            series=series, item=item, progress_map=progress, eta_map={}, original_rank={})

    def actions(self, html):
        return re.findall(r'hx-post="/reader/7/([a-z-]+)"', html)

    def test_unread_can_only_be_marked_read(self):
        self.assertEqual(self.actions(self.render(None)), ["mark-read"])

    def test_reading_can_go_either_way_and_asks_about_the_place(self):
        html = self.render("reading")
        self.assertEqual(self.actions(html), ["mark-read", "mark-unread"])
        self.assertIn("Where you left off will be forgotten.", html)

    def test_read_can_only_go_back_and_asks_about_the_stats(self):
        html = self.render("completed")
        self.assertEqual(self.actions(html), ["mark-unread"])
        self.assertIn("It will no longer count as finished in your stats.", html)

    def test_there_is_no_second_way_back(self):
        for status in (None, "reading", "completed"):
            self.assertNotIn("clear-status", self.render(status))


class MarkAllUnreadAsks(unittest.TestCase):
    def test_the_form_confirms(self):
        source = (ROOT / "app/templates/library/series.html").read_text()
        form = re.search(r'<form[^>]*mark-all-unread[^>]*>', source, re.S).group(0)
        self.assertIn("hx-confirm=", form)
