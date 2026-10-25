"""Text predictions pair validated words with time, independently of progress."""
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.stats.pace_totals import rebuild_pace_totals
from app.database import init_db
from tests.test_estimates import EstimateCase


class WordEstimates(EstimateCase):
    async def book(self, identity=1, text='one two three four', other='five six'):
        await self.item(identity)
        path = Path(self._tmp.name) / f'{identity}.epub'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
            archive.writestr('content.opf', '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
            archive.writestr('a.xhtml', '<p>' + text + '</p>')
            archive.writestr('b.xhtml', '<p>' + other + '</p>')
        await self.db.execute('UPDATE items SET file_path=? WHERE id=?', (str(path), identity))
        await self.db.commit()
        return await self.row('SELECT * FROM items WHERE id=?', (identity,))

    async def evidence(self, *, item_id=None, novel_id=None, words=1200, seconds=360, samples=3):
        kind = 'novel' if novel_id is not None else 'epub'
        await self.db.execute('''INSERT INTO page_reads(user_id,item_id,novel_id,kind,started_at,
            seconds,cap_seconds,visit_id,pace_words,pace_seconds,pace_samples)
            VALUES(1,?,?,?,'2026-10-25',0,0,'valid',?,?,?)''',
            (item_id, novel_id, kind, words, seconds, samples))
        await self.db.commit()

    async def test_legacy_time_and_absolute_progress_never_manufacture_a_text_rate(self):
        item = await self.book()
        await self.progress(1, 'reading', 80)
        await self.spend(3600, item_id=1)
        estimator = await self.estimator()
        await estimator.prepare_items([item])
        self.assertIsNone(estimator.text_rate('epub', 1))
        self.assertIsNone(estimator.for_item(item, None))
        self.assertTrue(estimator.is_collecting(item))
        self.assertEqual(estimator.spent_by_item[1], 3600)

    async def test_remaining_words_follow_saved_source_anchor_not_percentage(self):
        item = await self.book()
        await self.evidence()  # Prior history, including removed content.
        progress = await self.progress(1, 'reading', 99)
        await self.db.execute("UPDATE reading_progress SET current_page=0,current_location='elidx:0:8' WHERE item_id=1")
        await self.db.commit()
        estimator = await self.estimator()
        await estimator.prepare_items([item])
        # Three/four in the first section plus two words in the second.
        self.assertAlmostEqual(estimator.for_item(item, progress).remaining, 4 * .3)
        self.assertAlmostEqual(estimator.for_item(item, progress).total, 6 * .3)

    async def test_old_unresolvable_position_collects_instead_of_inventing_words(self):
        item = await self.book()
        await self.evidence()
        await self.progress(1, 'reading', 40)
        await self.db.execute("UPDATE reading_progress SET current_location='epubcfi(/6/4)' WHERE item_id=1")
        estimator = await self.estimator()
        await estimator.prepare_items([item])
        self.assertIsNone(estimator.for_item(item, None))

    async def test_valid_own_book_evidence_gradually_replaces_the_account_prior(self):
        await self.book()
        await self.evidence(words=6000, seconds=1800)
        before = (await self.estimator()).text_rate('epub', 1)
        await self.evidence(item_id=1, words=300, seconds=180, samples=1)
        little = (await self.estimator()).text_rate('epub', 1)
        await self.evidence(item_id=1, words=6000, seconds=3600)
        much = (await self.estimator()).text_rate('epub', 1)
        self.assertLess(before, little)
        self.assertLess(little, much)
        self.assertLess(much, .6)
        self.assertLess(little, .4)  # One short sample cannot abruptly replace history.

    async def test_all_evidence_thresholds_and_reader_isolation_apply(self):
        await self.evidence(words=1000, seconds=300, samples=2)
        self.assertIsNone((await self.estimator()).text_rate('epub', 1))
        await self.evidence(words=1, seconds=1, samples=1)
        self.assertIsNotNone((await self.estimator()).text_rate('epub', 1))
        self.assertIsNone((await self.estimator(user_id=2)).text_rate('epub', 1))

    async def test_removed_book_evidence_survives_without_contaminating_reused_id(self):
        item = await self.book()
        await self.evidence(item_id=item['id'])
        await self.db.execute('DELETE FROM items WHERE id=1')
        await self.db.commit()
        await self.book()
        estimator = await self.estimator()
        self.assertIsNotNone(estimator.text_rate('epub', 1))
        self.assertAlmostEqual(estimator.text_rate('epub', 1), .3)
        self.assertNotIn(('epub', 1), estimator.samples)
        self.assertEqual(estimator.account_sample, (1200, 360, 3))

    async def test_only_requested_books_are_parsed_and_warm_build_uses_summary_tables(self):
        selected = await self.book()
        await self.book(2)
        await self.evidence()
        from app.stats.estimates import inventory_epub_total
        with patch('app.stats.estimates.inventory_epub_total', wraps=inventory_epub_total) as read:
            estimator = await self.estimator()
            self.assertEqual(read.call_count, 0)
            await estimator.prepare_items([selected])
            self.assertEqual(read.call_count, 1)
            await estimator.prepare_items([selected])
            self.assertEqual(read.call_count, 1)

    async def test_summary_backfill_is_once_and_rebuild_matches_live_triggers(self):
        await self.book()
        await self.evidence(item_id=1)
        expected = tuple(await self.row('SELECT words,seconds,samples FROM account_pace_totals'))
        await self.db.execute('DELETE FROM derived_stats_state')
        await self.db.commit()
        await init_db()
        self.assertEqual(tuple(await self.row('SELECT words,seconds,samples FROM account_pace_totals')), expected)
        with patch('app.stats.pace_totals.rebuild_pace_totals', side_effect=AssertionError('Repeated history scan')):
            await init_db()
        await rebuild_pace_totals(self.db)
        self.assertEqual(tuple(await self.row('SELECT words,seconds,samples FROM item_pace_totals')), expected)

    async def test_novel_scope_uses_unread_words_and_current_block_offset(self):
        await self.novel_chapter(1)
        await self.novel_chapter(2)
        await self.novel_chapter(3)
        await self.db.execute("UPDATE chapters SET content_html='<p>one two three four</p>'")
        await self.db.execute('INSERT INTO volumes(id,novel_id,number) VALUES(2,1,2)')
        await self.db.execute('UPDATE chapters SET volume_id=2 WHERE id=3')
        await self.db.execute('''INSERT INTO novel_reading_progress(user_id,novel_id,chapter_id,paragraph_index,character_offset,chapter_percent)
            VALUES(1,1,1,0,8,99)''')
        await self.evidence()
        estimator = await self.estimator()
        await estimator.prepare_novel(1, 1)
        await estimator.prepare_novel(1)
        self.assertAlmostEqual(estimator.for_novel(1,1).remaining, 6*.3)
        self.assertAlmostEqual(estimator.for_novel(1).remaining, 10*.3)
        await self.db.execute('INSERT INTO novel_read_chapters(user_id,chapter_id) VALUES(1,2)')
        await self.db.commit()
        estimator = await self.estimator()
        await estimator.prepare_novel(1,1)
        self.assertAlmostEqual(estimator.for_novel(1,1).remaining, 2*.3)

    async def test_unknown_chapter_never_silently_understates_a_novel_scope(self):
        await self.novel_chapter(1, size=None)
        await self.evidence()
        estimator = await self.estimator()
        await estimator.prepare_novel(1)
        self.assertIsNone(estimator.for_novel(1))
        self.assertTrue(estimator.novel_collecting(1))

    async def test_collecting_novel_does_not_load_source_html(self):
        await self.novel_chapter(1)
        statements = []
        await self.db.set_trace_callback(statements.append)
        estimator = await self.estimator()
        await estimator.prepare_novel(1)
        await self.db.set_trace_callback(None)
        self.assertTrue(estimator.novel_collecting(1))
        self.assertFalse(any('content_html' in sql.lower() for sql in statements),
                         'Collecting data must not fetch chapter source bodies')
