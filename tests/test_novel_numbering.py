"""Source numbers are independent of DOM order and stable chapter identities."""

import json
from dataclasses import replace
from unittest import TestCase

from app.database import get_connection
from app.novels import links
from app.novels.router import (
    _load_chapter_context,
    read_chapter_by_id,
    read_chapter,
    read_novel_progress,
)
from app.novels.scraper import _parse_chapter_list_html, ScrapedChapterRef
from tests.test_novel_sync import SyncCase
from tests.test_novel_page import _request

SOURCE = """<div id="episodeNime"><strong>Vol/Ch Title Release Date</strong><ul>
<li><a href="https://www.cardboardtranslation.com/2026/11/this-kind-of-play-comes-at-extra-cost-c63.html"><span>Ch. 63</span><span> Hold Hands, or Maybe a Hug?</span><span>08 November 2026</span></a></li>
<li><a href="https://www.cardboardtranslation.com/2026/09/this-kind-of-play-comes-at-extra-cost-c1.html"><span>Ch. 1</span><span> I Am Miss Lorentina’s Dog!</span><span>12 September 2026</span></a></li></ul></div>"""


class SourceNumbers(TestCase):
    def test_descending_real_labels_and_chapter_only_urls(self):
        rows = _parse_chapter_list_html(SOURCE)
        self.assertEqual(
            [(r.volume_number, r.chapter_number, r.title) for r in rows],
            [
                (1, 1, "I Am Miss Lorentina’s Dog!"),
                (1, 63, "Hold Hands, or Maybe a Hug?"),
            ],
        )
        self.assertEqual(rows[0].release_date, "12 September 2026")

    def test_mixed_formats_keep_label_priority_parts_and_literal_titles(self):
        source = """<div id="episodeNime"><ul>
        <li><a href="https://x/book-c45.html">[Vol. 3] Ch. 45.3 Interlude</a></li>
        <li><a href="https://x/book-v1c8.html">Ch. 7 Title about Ch. 5</a></li>
        <li><a href="https://x/book-c1.html">Chapter 1 First</a></li>
        <li><a href="https://x/share">Share</a></li></ul></div>"""
        rows = _parse_chapter_list_html(source)
        self.assertEqual(
            [(r.volume_number, r.chapter_number, r.title) for r in rows],
            [(1, 1, "First"), (1, 7, "Title about Ch. 5"), (3, 45.3, "Interlude")],
        )


class RepairNumbers(SyncCase):
    async def test_ambiguous_numeric_link_keeps_its_current_target(self):
        self.correct = _parse_chapter_list_html(SOURCE)
        self.site = [replace(r, chapter_number=5) for r in self.correct]
        await self.sync()
        db = await get_connection()
        try:
            before = await links.resolve_number(db, 1, 1, 5)
        finally:
            await db.close()
        self.site = self.correct
        await self.sync()
        db = await get_connection()
        try:
            self.assertEqual(
                await links.resolve_number(db, 1, 1, 5),
                before,
                "An ambiguous legacy key must retain its existing resolution",
            )
        finally:
            await db.close()

    async def test_order_marker_does_not_become_a_replacement_rule(self):
        from app import text_replacements

        await self.seed_wrong()
        await self.sync()
        await self.execute(
            "INSERT INTO text_replacements(user_id,novel_id,find_text,replace_text) VALUES(1,1,'fixture','text')"
        )
        db = await get_connection()
        try:
            actual = await text_replacements.rules_for(db, 1, novel_id=1)
            transported = json.loads(
                await text_replacements.rules_json(db, 1, novel_id=1)
            )
            self.assertEqual(len(actual), 1)
            self.assertEqual([r for r in transported if r.get("find")], actual)
            self.assertEqual(
                transported[-1],
                {"reader_order": await links.revision(db, 1)},
                "Old restored readers need the order marker",
            )
        finally:
            await db.close()

    async def seed_wrong(self, manual=False):
        correct = _parse_chapter_list_html(SOURCE)
        self.correct = correct
        # The old parser numbered the descending source positions 1,2.
        self.site = [
            replace(correct[1], chapter_number=1),
            replace(correct[0], chapter_number=2),
        ]
        await self.sync()
        before = await self.rows(
            "SELECT id,url,order_index FROM chapters ORDER BY order_index"
        )
        self.ids = {r["url"]: r["id"] for r in before}
        self.saved = self.ids[correct[0].url]
        await self.execute(
            "UPDATE chapters SET content_html='<p>fixture passage retained</p>',content_size=31"
        )
        await self.execute(
            "INSERT INTO novel_reading_progress(user_id,novel_id,chapter_id,paragraph_index,character_offset,scroll_percent,chapter_percent) VALUES(1,1,?,0,7,80,20)",
            (self.saved,),
        )
        await self.execute(
            "INSERT INTO novel_read_chapters(user_id,chapter_id) VALUES(1,?)",
            (self.saved,),
        )
        if manual:
            await self.execute("UPDATE novels SET manual_sort=1")
        self.positions_while_downloading = None
        self.published.clear()
        self.site = correct
        return before

    async def test_existing_repair_keeps_passage_marks_bodies_and_aliases(self):
        await self.seed_wrong()
        await self.sync()
        after = await self.rows(
            "SELECT id,url,number,content_html FROM chapters ORDER BY order_index"
        )
        self.assertEqual([r["number"] for r in after], [1, 63])
        self.assertEqual({r["url"]: r["id"] for r in after}, self.ids)
        self.assertTrue(
            all(r["content_html"] == "<p>fixture passage retained</p>" for r in after)
        )
        saved = (
            await self.rows(
                "SELECT chapter_id,paragraph_index,character_offset FROM novel_reading_progress"
            )
        )[0]
        self.assertEqual(
            saved, dict(chapter_id=self.saved, paragraph_index=0, character_offset=7)
        )
        self.assertEqual(
            (await self.rows("SELECT chapter_id FROM novel_read_chapters"))[0][
                "chapter_id"
            ],
            self.saved,
        )
        self.assertIsNone(self.positions_while_downloading)
        self.assertIn(
            {"type": "novel_updated", "novel_id": 1},
            self.published,
            "Metadata-only repairs must notify views",
        )
        db = await get_connection()
        try:
            self.assertEqual(
                await links.resolve_number(db, 1, 1, 1),
                self.ids[self.correct[1].url],
                "Old numeric bookmarks must keep their target",
            )
            self.assertEqual(await links.resolve_number(db, 1, 1, 2), self.saved)
            body = json.loads((await read_novel_progress(1, {"id": 1}, db)).body)
            self.assertEqual(body["resume_url"], f"/novels/1/read/{self.saved}")
            self.assertEqual(body["position"], [0, 0, 7])
            ctx = await _load_chapter_context(
                1, 0, 0, {"id": 1}, db, chapter_id=self.saved
            )
            self.assertEqual(ctx["saved_offset"], 7)
        finally:
            await db.close()

    async def test_manual_order_is_retained_and_manifest_tracks_number_only_repair(
        self,
    ):
        before = await self.seed_wrong(manual=True)
        db = await get_connection()
        try:
            old = await links.revision(db, 1)
        finally:
            await db.close()
        await self.sync()
        self.assertEqual(
            await self.rows(
                "SELECT id,url,order_index FROM chapters ORDER BY order_index"
            ),
            before,
        )
        db = await get_connection()
        try:
            new = await links.revision(db, 1)
        finally:
            await db.close()
        self.assertNotEqual(
            old, new, "A number-only repair must invalidate the reader manifest"
        )
        await self.sync()
        db = await get_connection()
        try:
            self.assertEqual(
                await links.revision(db, 1),
                new,
                "An unchanged sync must retain the reader revision",
            )
        finally:
            await db.close()

    async def test_numeric_and_id_routes_render_the_same_retained_chapter(self):
        await self.seed_wrong()
        await self.sync()
        db = await get_connection()
        try:
            legacy = await read_chapter(
                1, 1, 1, _request("/novels/1/read/1/1"), {"id": 1, "username": "u"}, db
            )
            identity = self.ids[self.correct[1].url]
            current = await read_chapter_by_id(
                1,
                identity,
                _request(f"/novels/1/read/{identity}"),
                {"id": 1, "username": "u"},
                db,
            )
            for response in (legacy, current):
                self.assertIn(
                    f'data-start-chapter-id="{identity}"', response.body.decode()
                )
            self.assertIsNone(
                await _load_chapter_context(
                    99, 0, 0, {"id": 1}, db, chapter_id=identity
                ),
                "Chapter IDs cannot cross novel ownership",
            )
        finally:
            await db.close()

    async def test_aliases_survive_further_repairs_and_are_removed_with_chapters(self):
        await self.seed_wrong()
        await self.sync()
        self.site = [
            replace(r, chapter_number=r.chapter_number + 10, volume_number=2)
            for r in self.correct
        ]
        await self.sync()
        db = await get_connection()
        try:
            self.assertEqual(
                await links.resolve_number(db, 1, 1, 1),
                self.ids[self.correct[1].url],
                "Later repairs must not overwrite an existing alias",
            )
        finally:
            await db.close()
        await self.execute("DELETE FROM chapters WHERE id=?", (self.saved,))
        self.assertFalse(
            await self.rows(
                "SELECT 1 FROM novel_chapter_aliases WHERE chapter_id=?", (self.saved,)
            )
        )
