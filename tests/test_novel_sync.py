"""A web novel's sync, against a scripted site.

What each test pins was measured wrong before:
- New chapters sat at position 0 until every body had downloaded (half an
  hour for 900 chapters), and the reader opens a chapter by its position:
  every link opened the wrong chapter meanwhile.
- A URL listed twice broke UNIQUE(novel_id, url): every sync of that novel
  failed with nothing written.
- The cover's URL was recorded before its image downloaded, so one failed
  download kept the old picture for good.
- "Sync now" on a paused novel returned at once and stayed "queued".
- A manual reorder reused the volume's min..min+n range and collided with
  the next volume after a sync had appended a chapter.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.database import close_pool, get_connection, init_db
from app.library import events
from app.novels import router as novels_router
from app.novels import sync as sync_mod
from app.novels.scraper import ScrapedChapterRef, ScrapedNovelMeta, _parse_chapter_list_html


def ref(vol, chap, url=None, title=None):
    return ScrapedChapterRef(title=title or f"c{chap}", url=url or f"https://x/v{vol}c{chap}.html",
                             volume_number=vol, chapter_number=float(chap))


class SyncCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        (settings.data_dir / "covers").mkdir()
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        self.published = []
        original_publish = events.publish
        events.publish = self.published.append
        self.addCleanup(lambda: setattr(events, "publish", original_publish))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        await init_db()
        await self.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.execute("INSERT INTO novels (id, title, source_url, active) VALUES (1, 'N', 'https://x/n', 1)")
        self.site = []
        self.cover_url = None
        self.positions_while_downloading = None

    async def execute(self, sql, params=()):
        db = await get_connection()
        try:
            await db.execute(sql, params)
            await db.commit()
        finally:
            await db.close()

    async def rows(self, sql, params=()):
        db = await get_connection()
        try:
            return [dict(r) for r in await (await db.execute(sql, params)).fetchall()]
        finally:
            await db.close()

    async def sync(self, *, image=b"\x89PNG", manual=False):
        async def index(url):
            return ScrapedNovelMeta(title="N", cover_url=self.cover_url, synopsis=None, status=None,
                                    chapters=list(self.site))

        async def contents(urls, on_chapter_done=None, delay_seconds=None):
            # What a reader opening a link meanwhile would get.
            self.positions_while_downloading = await self.order()
            for i, url in enumerate(urls, 1):
                await on_chapter_done(i, len(urls), url, True, "<p>x</p>")

        async def fetch_image(url):
            return (image, "image/png") if image else None

        with patch.object(sync_mod, "fetch_novel_index", index), \
                patch.object(sync_mod, "fetch_all_chapter_contents", contents), \
                patch.object(sync_mod, "fetch_image", fetch_image), \
                patch.object(sync_mod, "encode_cover_thumbnail", lambda b: _identity(b)):
            return await sync_mod.sync_novel(1, manual=manual)

    async def order(self):
        return [r["url"].rsplit("/", 1)[1].removesuffix(".html")
                for r in await self.rows("SELECT url FROM chapters ORDER BY order_index, id")]


async def _identity(data):
    return data


class Positions(SyncCase):
    async def test_new_chapters_are_in_place_before_any_body_downloads(self):
        self.site = [ref(1, 1), ref(1, 2), ref(1, 3)]
        await self.sync()
        self.assertEqual(self.positions_while_downloading, ["v1c1", "v1c2", "v1c3"])
        self.site = [ref(1, 1), ref(1, 2), ref(1, 3), ref(1, 4), ref(2, 5)]
        await self.sync()
        self.assertEqual(self.positions_while_downloading, ["v1c1", "v1c2", "v1c3", "v1c4", "v2c5"])
        positions = [r["order_index"] for r in await self.rows("SELECT order_index FROM chapters ORDER BY order_index")]
        self.assertEqual(positions, list(range(5)))

    async def test_manual_order_appends_new_chapters_at_the_end_at_once(self):
        self.site = [ref(1, 1), ref(1, 2)]
        await self.sync()
        await self.execute("UPDATE novels SET manual_sort = 1")
        await self.execute("UPDATE chapters SET order_index = 1 - order_index")  # reversed by hand
        self.site = [ref(1, 1), ref(1, 2), ref(1, 3)]
        await self.sync()
        self.assertEqual(self.positions_while_downloading, ["v1c2", "v1c1", "v1c3"])

    async def test_a_single_chapter_novel_appends_after_it(self):
        # MAX(order_index) = 0 read as "none" put the next one at 0 too.
        self.site = [ref(1, 1)]
        await self.sync()
        await self.execute("UPDATE novels SET manual_sort = 1")
        self.site = [ref(1, 1), ref(1, 2)]
        await self.sync()
        positions = [r["order_index"] for r in await self.rows("SELECT order_index FROM chapters ORDER BY id")]
        self.assertEqual(positions, [0, 1])

    async def test_a_new_chapter_is_announced(self):
        self.site = [ref(1, 1)]
        await self.sync()
        self.assertIn({"type": "novel_updated", "novel_id": 1}, self.published)

    async def test_a_volume_emptied_upstream_goes(self):
        self.site = [ref(1, 1), ref(2, 2, url="https://x/moved.html")]
        await self.sync()
        self.site = [ref(1, 1), ref(1, 2, url="https://x/moved.html")]
        await self.sync()
        self.assertEqual([r["number"] for r in await self.rows("SELECT number FROM volumes")], [1])


class Robustness(SyncCase):
    async def test_a_url_listed_twice_is_one_chapter(self):
        self.site = [ref(1, 1), ref(1, 1), ref(1, 2)]
        await self.sync()
        self.assertEqual(await self.order(), ["v1c1", "v1c2"])
        self.assertIsNotNone((await self.rows("SELECT last_checked_at FROM novels"))[0]["last_checked_at"])

    async def test_the_parser_drops_the_repeat_itself(self):
        html = ('<div><a href="https://x/n-v1c1-a.html">Chapter 1</a>'
                '<a href="https://x/n-v1c1-a.html">Latest: Chapter 1</a>'
                '<a href="https://x/n-v1c2-b.html">Chapter 2</a></div>')
        self.assertEqual([c.url for c in _parse_chapter_list_html(html)],
                         ["https://x/n-v1c1-a.html", "https://x/n-v1c2-b.html"])

    async def test_a_failed_cover_download_is_tried_again(self):
        self.site = [ref(1, 1)]
        self.cover_url = "https://x/old.png"
        await self.sync()
        self.cover_url = "https://x/new.png"
        await self.sync(image=None)  # the download fails
        self.assertEqual((await self.rows("SELECT cover_url FROM novels"))[0]["cover_url"], "https://x/old.png")
        await self.sync(image=b"new")
        row = (await self.rows("SELECT cover_url, cover_image FROM novels"))[0]
        self.assertEqual((row["cover_url"], row["cover_image"]), ("https://x/new.png", b"new"))

    async def test_sync_now_runs_on_a_paused_novel(self):
        await self.execute("UPDATE novels SET active = 0")
        self.site = [ref(1, 1)]
        self.assertEqual(await self.sync(), 0)            # the periodic one skips it
        self.assertEqual(await self.order(), [])
        self.assertEqual(await self.sync(manual=True), 1)  # a person asked
        self.assertEqual(await self.order(), ["v1c1"])


class ManualReorder(SyncCase):
    async def test_dragging_an_appended_chapter_into_its_volume_keeps_positions_unique(self):
        self.site = [ref(1, 1), ref(1, 2), ref(2, 3), ref(2, 4)]
        await self.sync()
        await self.execute("UPDATE novels SET manual_sort = 1, manual_sort_editing = 1")
        self.site.append(ref(1, 5))          # appended at the END of the novel
        await self.sync()
        ids = {r["url"].rsplit("/", 1)[1].removesuffix(".html"): r["id"]
               for r in await self.rows("SELECT id, url FROM chapters")}
        volume_1 = (await self.rows("SELECT id FROM volumes WHERE number = 1"))[0]["id"]

        class Body:
            async def json(self):
                return {"volume_id": volume_1, "chapter_ids": [ids["v1c1"], ids["v1c5"], ids["v1c2"]]}

        db = await get_connection()
        try:
            await novels_router.reorder_novel_chapters(1, Body(), {"id": 1}, db)
        finally:
            await db.close()
        self.assertEqual(await self.order(), ["v1c1", "v1c5", "v1c2", "v2c3", "v2c4"])
        positions = [r["order_index"] for r in await self.rows("SELECT order_index FROM chapters")]
        self.assertEqual(sorted(positions), list(range(5)))


class Trackers(SyncCase):
    async def test_an_entry_not_started_marks_no_prologue_read(self):
        # Listed but not started is chapter 0, volume 0 on the service.
        from app.tracking import service
        from app.tracking.models import RemoteProgress
        from app.tracking.store import LocalProgress
        await self.execute("INSERT INTO volumes (id, novel_id, number) VALUES (10, 1, 0)")
        await self.execute("INSERT INTO chapters (id, novel_id, volume_id, number, title, url, order_index)"
                           " VALUES (100, 1, 10, 0, 'Prologue', 'https://x/p', 0)")
        db = await get_connection()
        try:
            marked = await service._adopt(db, 1, LocalProgress(), RemoteProgress(chapter=0, volume=0),
                                          series_id=None, novel_id=1)
            await db.commit()
        finally:
            await db.close()
        self.assertEqual(marked, 0)
        self.assertEqual(await self.rows("SELECT * FROM novel_read_chapters"), [])

    async def test_a_successful_push_clears_the_accounts_error(self):
        from app.tracking import store
        await self.execute("INSERT INTO tracker_accounts (user_id, tracker, access_token, last_error)"
                           " VALUES (1, 'mangaupdates', 't', 'expired -- sign in again')")
        await self.execute("INSERT INTO tracker_links (id, user_id, tracker, novel_id, remote_id, remote_title,"
                           " remote_url) VALUES (5, 1, 'mangaupdates', 1, 'r', 'T', 'https://x')")
        db = await get_connection()
        try:
            await store.record_push(db, 5, chapter=3, volume=None)
            await db.commit()
        finally:
            await db.close()
        self.assertEqual(await self.rows("SELECT last_error FROM tracker_accounts"), [{"last_error": None}])


class NovelPageEvents(SyncCase):
    async def test_marking_chapters_read_refreshes_the_home_card(self):
        self.site = [ref(1, 1), ref(1, 2)]
        await self.sync()
        self.published.clear()
        chapter = (await self.rows("SELECT id FROM chapters ORDER BY id"))[0]["id"]
        volume = (await self.rows("SELECT id FROM volumes"))[0]["id"]

        class Body:
            def __init__(self, payload):
                self.payload = payload

            async def json(self):
                return self.payload

        await novels_router.set_read_status(chapter, Body({"read": True}), {"id": 1})
        db = await get_connection()
        try:
            await novels_router.set_volume_read_status(volume, Body({"read": True}), {"id": 1}, db)
        finally:
            await db.close()
        from app.tracking import service
        await service.drain()
        self.assertEqual([e for e in self.published if e["type"] == "novel_updated"],
                         [{"type": "novel_updated", "novel_id": 1}] * 2)
        self.assertEqual(len(await self.rows("SELECT * FROM novel_read_chapters")), 2)
