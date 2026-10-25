"""The home page's "Reading" strip, and the order its cards are in.

That order is not decoration. `library-events.js` moves a card to the
FRONT of the strip when a progress event names it, with no refetch at all
-- which is only correct because this list is sorted by the very column
the progress writers set to `now`. So the cases below go through the real
handlers rather than writing the rows themselves: what they pin is the
COUPLING between the two halves, and a test that issued its own UPDATE
would keep passing while the pair drifted apart.
"""
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import _reading_items
from app.novels.router import save_novel_progress
from app.reader.router import save_progress

APP = Path(__file__).resolve().parent.parent / "app"


class _JsonRequest:
    """Enough of a Request for a handler that only reads its body."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class ReadingStripCase(unittest.IsolatedAsyncioTestCase):
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
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'S', 's', '/f')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def item(self, item_id, title, *, hours_ago):
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size, page_count) VALUES (?, 1, 1, ?, ?, ?, 'cbz', ?, 1, 100)",
            (item_id, title, title.lower(), f"/{title}", f"h{item_id}"))
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, current_page, progress_percent,"
            " last_read_at) VALUES (1, ?, 'reading', 10, 10, datetime('now', ?))",
            (item_id, f"-{hours_ago} hours"))
        await self.db.commit()

    async def novel(self, novel_id, title, chapter_id, *, hours_ago):
        await self.db.execute("INSERT INTO novels (id, title, source_url) VALUES (?, ?, ?)",
                              (novel_id, title, f"https://x/{novel_id}"))
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (?, ?, 1)",
                              (novel_id * 100, novel_id))
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url, content_html,"
            " content_size) VALUES (?, ?, ?, 1.0, 'C', ?, '<p>x</p>', LENGTH('<p>x</p>'))",
            (chapter_id, novel_id, novel_id * 100, f"https://x/c{chapter_id}"))
        await self.db.execute(
            "INSERT INTO novel_reading_progress (user_id, novel_id, chapter_id, scroll_percent,"
            " chapter_percent, updated_at) VALUES (1, ?, ?, 10, 10, datetime('now', ?))",
            (novel_id, chapter_id, f"-{hours_ago} hours"))
        await self.db.commit()

    async def titles(self):
        return [r["title"] for r in await _reading_items(self.db, 1)]


class Ordering(ReadingStripCase):
    async def test_it_is_most_recently_read_first(self):
        # Deliberately against the id order: the oldest read has the LOWEST
        # id here, so a list that came out right by accident of insertion
        # order fails this.
        await self.item(1, "D", hours_ago=3)
        await self.item(2, "C", hours_ago=2)
        await self.item(3, "B", hours_ago=1)
        self.assertEqual(await self.titles(), ["B", "C", "D"])

    async def test_saving_progress_moves_an_item_to_the_front(self):
        # The property library-events.js relies on when it promotes a card
        # with no refetch: the writer behind a progress event sets exactly
        # the column this list is sorted by, so the event's arrival IS the
        # new position.
        await self.item(1, "B", hours_ago=1)
        await self.item(2, "C", hours_ago=2)
        await self.item(3, "D", hours_ago=3)
        await save_progress(3, _JsonRequest(
            {"current_page": 77, "progress_percent": 77, "status": "reading"}), self.user, self.db)
        self.assertEqual(await self.titles(), ["D", "B", "C"])

    async def test_two_saves_in_one_second_both_pass_what_was_not_touched(self):
        # What is actually guaranteed, and no more. `last_read_at` is
        # `datetime('now')`, which is second-granular, so two saves inside
        # the same second TIE and nothing orders them against each other --
        # measured, not assumed. Asserting a winner between them would be a
        # test that passes on the stable sort's input order rather than on
        # anything the code promises.
        await self.item(1, "B", hours_ago=1)
        await self.item(2, "C", hours_ago=2)
        await self.item(3, "D", hours_ago=3)
        body = {"current_page": 20, "progress_percent": 20, "status": "reading"}
        await save_progress(3, _JsonRequest(body), self.user, self.db)
        await save_progress(2, _JsonRequest(body), self.user, self.db)
        self.assertEqual((await self.titles())[-1], "B")
        self.assertEqual(sorted((await self.titles())[:2]), ["C", "D"])

    async def test_a_novel_is_ordered_on_the_same_clock_as_an_item(self):
        # The two content types come from different tables and different
        # timestamp columns; the strip interleaves them, so both columns
        # have to mean the same thing.
        await self.item(1, "B", hours_ago=2)
        await self.novel(1, "N", 501, hours_ago=1)
        self.assertEqual(await self.titles(), ["N", "B"])

    async def test_saving_a_novel_position_moves_it_to_the_front(self):
        await self.item(1, "B", hours_ago=1)
        await self.novel(1, "N", 501, hours_ago=3)
        await save_novel_progress(1, _JsonRequest(
            {"chapter_id": 501, "scroll_percent": 40, "chapter_percent": 40}), self.user, self.db)
        self.assertEqual(await self.titles(), ["N", "B"])


class TheCardCarriesItsEstimate(ReadingStripCase):
    """A comic on this strip is timed by the page at its own series' rate
    (see stats/estimates.py), and the estimator asks the ROW for the
    series it belongs to. The strip builds that row from a hand-written
    column list, so a column the estimator needs and the SELECT does not
    name is a crash on the home page -- for comics only, which is the half
    of the library the ebook tests never touch."""

    async def _time_a_sibling_volume(self):
        # Volume 9 of the same series, read through: 1800s over 100 pages.
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size, page_count)"
            " VALUES (9, 1, 1, 'v9', 'v9', '/v9', 'cbz', 'h9', 1, 100)")
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent,"
            " completed_at, completed_from_percent)"
            " VALUES (1, 9, 'completed', 100, datetime('now'), 100)")
        for _ in range(10):
            await self.db.execute(
                "INSERT INTO page_reads (user_id, item_id, kind, page_index, started_at,"
                " seconds, cap_seconds) VALUES (1, 9, 'cbz', 0, datetime('now'), 180, 180)")
        await self.db.commit()

    async def test_a_comic_shows_one_once_its_series_has_a_rate(self):
        await self.item(1, "A", hours_ago=1)
        await self._time_a_sibling_volume()
        entry, = [e for e in await _reading_items(self.db, 1) if e["kind"] == "item"]
        # 18 s/page over a 100-page volume is 1800s, of which 90% is left.
        self.assertAlmostEqual(entry["eta"].total, 1800, places=4)
        self.assertAlmostEqual(entry["eta"].remaining, 1620, places=4)

    async def test_and_none_while_nothing_in_that_series_has_been_timed(self):
        await self.item(1, "A", hours_ago=1)
        entry, = [e for e in await _reading_items(self.db, 1) if e["kind"] == "item"]
        self.assertIsNone(entry["eta"])


class ANovelCardsTimeIsItsVolumes(ReadingStripCase):
    async def volume(self):
        await self.novel(1,'N',501,hours_ago=1)
        await self.db.execute("INSERT INTO volumes (id,novel_id,number) VALUES (200,1,2)")
        for chapter,volume,words in ((501,100,100),(502,100,200),(503,200,1000)):
            source='<p>' + 'x '*words + '</p>'
            if chapter == 501:
                await self.db.execute('UPDATE chapters SET content_html=?,content_size=? WHERE id=501',(source,len(source)))
            else:
                await self.db.execute("""INSERT INTO chapters
                    (id,novel_id,volume_id,number,title,url,content_html,content_size)
                    VALUES (?,1,?,?,'C',?,?,?)""", (chapter,volume,chapter,f'https://x/{chapter}',source,len(source)))
        await self.db.execute('UPDATE novel_reading_progress SET paragraph_index=0,character_offset=40,chapter_percent=99')
        await self.db.commit()

    async def learn(self):
        for index in range(3):
            await self.db.execute("""INSERT INTO page_reads
                (user_id,kind,started_at,seconds,cap_seconds,visit_id,words,pace_words,pace_seconds,pace_samples)
                VALUES (1,'epub',datetime('now'),100,100,?,1000,1000,100,1)""", (f'sample-{index}',))
        await self.db.commit()

    async def card(self):
        # The browser fills a cache miss through the authoritative endpoint;
        # HTML rendering itself only reads that cached result.
        from app.library.router import reading_estimates
        from tests.test_text_eta_cards import JsonRequest
        await reading_estimates(JsonRequest([{'kind':'novel','id':1,'volume_id':100}]), {'id':1}, self.db)
        return next(entry for entry in await _reading_items(self.db,1) if entry['kind']=='novel')

    async def test_reading_scope_stops_at_volume_and_uses_character_anchor(self):
        await self.volume()
        await self.learn()
        card=await self.card()
        # 20 words traversed; 80 + 200 remain. The other volume is excluded.
        self.assertAlmostEqual(card['eta'].remaining,28)
        self.assertEqual(card['eta_volume_id'],100)
        self.assertEqual(card['eta_scope'],'volume 1')
        self.assertEqual(card['chapter_label'],'Vol. 1 · Ch. 1')

    async def test_marked_chapters_are_excluded_from_volume_scope(self):
        await self.volume()
        await self.learn()
        await self.db.execute('INSERT INTO novel_read_chapters(user_id,chapter_id) VALUES (1,501)')
        await self.db.commit()
        self.assertAlmostEqual((await self.card())['eta'].remaining,20)

    async def test_missing_chapter_body_means_collecting_not_a_byte_guess(self):
        await self.volume()
        await self.learn()
        await self.db.execute('UPDATE chapters SET content_html=NULL,content_size=NULL WHERE id=502')
        await self.db.commit()
        card=await self.card()
        self.assertIsNone(card['eta'])
        self.assertTrue(card['text_eta'])

    async def test_completed_volume_has_no_collecting_label(self):
        await self.volume()
        await self.learn()
        await self.db.executemany('INSERT INTO novel_read_chapters(user_id,chapter_id) VALUES (1,?)',[(501,),(502,)])
        await self.db.commit()
        card=await self.card()
        self.assertIsNone(card['eta'])
        self.assertFalse(card['text_eta'])

    async def test_no_word_evidence_means_collecting_even_with_legacy_time(self):
        await self.volume()
        await self.db.execute("INSERT INTO page_reads(user_id,novel_id,chapter_id,kind,started_at,seconds,cap_seconds) VALUES (1,1,501,'novel',datetime('now'),1000,1000)")
        await self.db.commit()
        card=await self.card()
        self.assertIsNone(card['eta'])
        self.assertTrue(card['text_eta'])

    async def test_item_card_claims_no_volume_scope(self):
        await self.item(1,'A',hours_ago=1)
        entry=next(entry for entry in await _reading_items(self.db,1) if entry['kind']=='item')
        self.assertEqual(entry['eta_scope'],'')



if __name__ == "__main__":
    unittest.main()
