"""A span about something that went away does not take the batch with it.

The reader flushes up to 200 spans at once. One naming an item or chapter
removed while the reader was open failed the foreign key, and the whole
batch -- every valid span in it -- was dropped. And a chapter's novel was
whatever the client said.
"""
from pathlib import Path

from tests.test_stats import StatsCase

ROOT = Path(__file__).resolve().parent.parent


class Survivors(StatsCase):
    async def stored(self):
        return [dict(r) for r in await (await self.db.execute(
            "SELECT item_id, novel_id, chapter_id, kind FROM page_reads ORDER BY id")).fetchall()]

    async def test_the_spans_that_still_have_something_are_kept(self):
        await self.item(1)
        await self.novel_chapter(5, novel_id=2)
        stored = await self.record([
            {"item_id": 1, "page_index": 0, "seconds": 10, "ago": 0},
            {"item_id": 99, "page_index": 0, "seconds": 10, "ago": 0},
            {"chapter_id": 5, "page_index": 0, "seconds": 10, "ago": 0},
            {"chapter_id": 98, "page_index": 0, "seconds": 10, "ago": 0},
        ])
        self.assertEqual(stored, 2)
        self.assertEqual(await self.stored(), [
            {"item_id": 1, "novel_id": None, "chapter_id": None, "kind": "cbz"},
            {"item_id": None, "novel_id": 2, "chapter_id": 5, "kind": "novel"},
        ])

    async def test_a_chapters_novel_is_its_own_not_the_clients(self):
        await self.novel_chapter(5, novel_id=2)
        await self.novel_chapter(6, novel_id=3)
        await self.record([{"novel_id": 3, "chapter_id": 5, "page_index": 0, "seconds": 10, "ago": 0}])
        self.assertEqual((await self.stored())[0]["novel_id"], 2)

    async def test_nothing_left_stores_nothing(self):
        self.assertEqual(
            await self.record([{"item_id": 99, "page_index": 0, "seconds": 10, "ago": 0}]), 0)


class TheClientNoLongerSaysWhichNovel(StatsCase):
    def test_no_reader_sends_it(self):
        for name in ("reading-time.js", "reader-text.js"):
            self.assertNotIn("novel_id:", (ROOT / "app/static/js" / name).read_text(), name)
