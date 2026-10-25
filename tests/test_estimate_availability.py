"""Saved estimates and honest partial totals survive large listing renders."""
from unittest.mock import patch
from pathlib import Path
from tests.test_word_estimates import WordEstimates as _Fixtures
from tests.test_estimates import EstimateCase
from app.stats.estimates import build_estimator


class EstimateAvailability(EstimateCase):
    book = _Fixtures.book
    evidence = _Fixtures.evidence
    async def test_warm_large_listing_keeps_last_valid_result_without_parsing(self):
        item = await self.book()
        await self.evidence()
        cold = await self.estimator()
        await cold.prepare_items([item])
        expected = cold.for_item(item, None)
        # Different instances, as on a full reload. Force the large-scope path.
        items = [dict(item, id=identity) for identity in range(1, 66)]
        warm = await build_estimator(self.db, 1, defer_large_scopes=True)
        with patch('app.stats.estimates.inventory_epub_total', side_effect=AssertionError('Render parsed content')):
            await warm.prepare_items(items)
        self.assertEqual(warm.for_item(item, None), expected,
                         'Reload must preserve the last valid estimate')

    async def test_one_unstarted_unavailable_book_keeps_known_series_total(self):
        good = await self.book()
        bad = await self.book(2)
        Path(bad['file_path']).unlink()
        await self.evidence()
        estimator = await self.estimator()
        await estimator.prepare_items([good, bad])
        self.assertAlmostEqual(estimator.total_remaining([dict(good, status=None), dict(bad, status=None)]), 1.8,
                               msg='One unknown book must not discard known time')
        self.assertEqual(estimator.item_states[2], 'content_unavailable')
        self.assertEqual(estimator.missing_items([good, bad]), 1)
        await self.db.execute("INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(1,1,'Series','series','/series')")
        await self.db.execute('UPDATE items SET series_id=1')
        from app.library.router import reading_estimates
        from tests.test_text_eta_cards import JsonRequest
        response = await reading_estimates(JsonRequest([{'kind':'series','id':1},{'kind':'item','id':2}]), {'id':1}, self.db)
        series, item = response['estimates']
        self.assertAlmostEqual(series['remaining'], 1.8)
        self.assertEqual(series['missing'], 1)
        self.assertEqual(item['state'], 'content_unavailable')
        self.assertFalse(item['collecting'], 'An unreadable book must not ask for more reading samples')

    async def test_changed_file_position_rules_and_account_reject_saved_result(self):
        item = await self.book()
        await self.evidence()
        original = await self.estimator()
        await original.prepare_items([item])
        self.assertIsNotNone(original.for_item(item, None))
        # Deferred rendering may read snapshots but must not parse the EPUB.
        async def rendered():
            estimator = await build_estimator(self.db, 1, defer_large_scopes=True)
            await estimator.prepare_items([dict(item, id=i) for i in range(1,66)])
            return estimator
        await self.db.execute("UPDATE items SET file_hash='changed' WHERE id=1")
        self.assertIsNone((await rendered()).for_item(item, None), 'Changed content must invalidate the result')
        await self.db.execute("UPDATE items SET file_hash='h1' WHERE id=1")
        await self.progress(1, 'reading', 10)
        await self.db.execute("UPDATE reading_progress SET current_location='elidx:0:4' WHERE item_id=1")
        self.assertIsNone((await rendered()).for_item(item, None), 'Changed source position must invalidate the result')
        await self.db.execute('DELETE FROM reading_progress')
        with patch('app.stats.estimates.text_replacements.rules_for', return_value=[{'find':'one','replace':'two words'}]):
            self.assertIsNone((await rendered()).for_item(item, None), 'Changed replacement rules must invalidate the result')
        await self.db.execute("INSERT INTO users(id,username,password_hash) VALUES(2,'other','x')")
        await self.db.execute("""INSERT INTO page_reads(user_id,kind,started_at,seconds,cap_seconds,
            visit_id,pace_words,pace_seconds,pace_samples)
            VALUES(2,'epub',datetime('now'),300,300,'other-evidence',1000,300,3)""")
        other = await build_estimator(self.db, 2, defer_large_scopes=True)
        await other.prepare_items([dict(item, id=i) for i in range(1,66)])
        self.assertIsNone(other.for_item(item, None), 'Another account must never receive the snapshot')

    async def test_large_novel_snapshot_invalidates_same_size_content_change_and_completion(self):
        for i in range(1,66):
            await self.novel_chapter(i)
        await self.db.execute("UPDATE chapters SET content_html='<p>one two</p>',content_size=14")
        await self.evidence()
        cold = await self.estimator()
        await cold.prepare_novel(1)
        expected = cold.for_novel(1)
        warm = await build_estimator(self.db, 1, defer_large_scopes=True)
        with patch('app.stats.estimates.inventory_html_total', side_effect=AssertionError('Render parsed chapter')):
            await warm.prepare_novel(1)
        self.assertEqual(warm.for_novel(1), expected, 'Novel reload must keep its last result')
        await self.db.execute("UPDATE chapters SET content_html='<p>one-two</p>' WHERE id=1")
        changed = await build_estimator(self.db, 1, defer_large_scopes=True)
        await changed.prepare_novel(1)
        self.assertIsNone(changed.for_novel(1), 'Same-size chapter edits must invalidate the result')
        await self.db.execute('INSERT INTO novel_read_chapters(user_id,chapter_id) SELECT 1,id FROM chapters')
        done = await build_estimator(self.db, 1, defer_large_scopes=True)
        await done.prepare_novel(1)
        self.assertIsNone(done.for_novel(1), 'Completed novel must not resurrect a snapshot')
        self.assertEqual(done.novel_states[(1,None)], 'complete')

    async def test_explicit_states_and_repaired_content_retry_without_progress_writes(self):
        item = await self.book()
        collecting = await self.estimator()
        await collecting.prepare_items([item])
        self.assertEqual(collecting.item_states[1], 'collecting')
        await self.evidence()
        await self.progress(1, 'reading', 40)
        await self.db.execute("UPDATE reading_progress SET current_location='old-anchor' WHERE item_id=1")
        invalid = await self.estimator()
        await invalid.prepare_items([item])
        self.assertEqual(invalid.item_states[1], 'position_unavailable')
        await self.db.execute("UPDATE reading_progress SET current_location='elidx:0:0' WHERE item_id=1")
        before = tuple(await self.row('SELECT * FROM reading_progress'))
        path = Path(item['file_path'])
        original = path.read_bytes()
        path.write_bytes(b'broken')
        broken = await self.estimator()
        await broken.prepare_items([item])
        self.assertEqual(broken.item_states[1], 'content_unavailable')
        path.write_bytes(original)
        repaired = await self.estimator()
        await repaired.prepare_items([item])
        self.assertEqual(repaired.item_states[1], 'available')
        after = tuple(await self.row('SELECT * FROM reading_progress'))
        self.assertEqual(before, after, 'Estimation must not change reading status or progress')


del _Fixtures
