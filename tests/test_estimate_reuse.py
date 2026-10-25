"""Unchanged authoritative requests reuse source counts with current account pace."""
from contextlib import ExitStack, closing
import json
import sqlite3
import zipfile
from pathlib import Path

from app.config import settings
from app.stats import estimate_snapshots as snapshots
from unittest.mock import patch

from app.library.router import reading_estimates
from app.stats.estimates import build_estimator
from tests.test_estimates import EstimateCase
from tests.test_word_estimates import WordEstimates as _Fixtures
from tests.test_text_eta_cards import JsonRequest


class EstimateReuse(EstimateCase):
    book = _Fixtures.book
    evidence = _Fixtures.evidence

    async def scopes(self):
        await self.book()
        await self.progress(1, 'reading', 10)
        await self.db.execute("UPDATE reading_progress SET current_location='elidx:0:4' WHERE item_id=1")
        for identity in range(1, 66):
            await self.novel_chapter(identity)
        await self.db.execute("UPDATE chapters SET content_html='<p>one two three four</p>'")
        await self.evidence(words=6000, seconds=1800)
        return [{'kind':'item','id':1}, {'kind':'novel','id':1}, {'kind':'novel','id':1,'volume_id':1}]

    async def estimates(self, targets):
        return (await reading_estimates(JsonRequest(targets), {'id':1}, self.db))['estimates']

    def no_source_work(self):
        stack = ExitStack()
        for name in ('inventory_epub', 'inventory_epub_total', 'inventory_html', 'inventory_html_total'):
            stack.enter_context(patch('app.stats.estimates.'+name,
                                      side_effect=AssertionError('Unchanged estimates must not traverse source inventories')))
        stack.enter_context(patch('app.stats.estimates.snapshots.save',
                                  side_effect=AssertionError('Unchanged estimates must not rewrite snapshots')))
        return stack

    async def test_repeated_authoritative_scopes_do_no_source_work_or_snapshot_writes(self):
        targets = await self.scopes()
        expected = await self.estimates(targets)
        statements = []
        await self.db.set_trace_callback(statements.append)
        try:
            with self.no_source_work(), patch.object(snapshots, "read", wraps=snapshots.read) as reads:
                for _ in range(3):
                    self.assertEqual(await self.estimates(targets), expected)
                self.assertEqual(reads.call_count, 3, "Each estimator must read the bounded snapshot index only once")
        finally:
            await self.db.set_trace_callback(None)
        self.assertFalse(any('content_html' in query.lower() for query in statements),
                         'Warm novel estimates must not fetch chapter source bodies')

    async def test_new_account_and_work_pace_updates_cached_counts_without_source_work(self):
        targets = await self.scopes()
        before = await self.estimates(targets)
        await self.evidence(novel_id=1, words=1200, seconds=720)
        model = await build_estimator(self.db, 1)
        with self.no_source_work():
            after = await self.estimates(targets)
            rendered = await build_estimator(self.db, 1, cached_only=True)
            await rendered.prepare_items([{'id':1,'file_format':'epub'}])
            await rendered.prepare_novel(1)
        # Five words after the saved EPUB anchor, 65 complete four-word chapters.
        self.assertAlmostEqual(after[0]['remaining'], 5*model.text_rate('epub',1), msg='Cached words must use current account pace')
        self.assertAlmostEqual(after[1]['remaining'], 260*model.text_rate('novel',1))
        self.assertNotEqual(before[0]['remaining'], after[0]['remaining'])
        self.assertNotEqual(before[1]['remaining'], after[1]['remaining'])
        self.assertEqual(rendered.for_novel(1).remaining, after[1]['remaining'])
        self.assertEqual(rendered.item_estimates[1].remaining, after[0]['remaining'])

    async def test_epub_position_rules_and_changed_file_invalidate_authoritative_counts(self):
        await self.book()
        await self.evidence()
        targets = [{'kind':'item','id':1}]
        self.assertAlmostEqual((await self.estimates(targets))[0]['remaining'], 1.8)
        await self.progress(1, 'reading', 99)
        await self.db.execute("UPDATE reading_progress SET current_location='elidx:0:8' WHERE item_id=1")
        self.assertAlmostEqual((await self.estimates(targets))[0]['remaining'], 1.2,
                               msg='A changed anchor must invalidate cached words')
        with patch('app.stats.estimates.text_replacements.rules_for', return_value=[{'find':'three','replace':'three extra'}]):
            self.assertAlmostEqual((await self.estimates(targets))[0]['remaining'], 1.5,
                                   msg='Replacement changes must invalidate cached words')
        path = Path(self._tmp.name) / '1.epub'
        with zipfile.ZipFile(path) as source:
            entries = {name:source.read(name) for name in source.namelist()}
        entries['a.xhtml'] = b'<p>one two three four five six</p>'
        with zipfile.ZipFile(path, 'w') as changed:
            for name, content in entries.items():
                changed.writestr(name, content)
        self.assertAlmostEqual((await self.estimates(targets))[0]['remaining'], 1.8,
                               msg='A replaced EPUB must invalidate cached words')
        await self.db.execute("UPDATE reading_progress SET status='completed' WHERE item_id=1")
        completed, = await self.estimates(targets)
        self.assertEqual(completed['state'], 'complete')
        self.assertIsNone(completed['remaining'])

    async def test_novel_position_read_status_volume_and_same_size_content_invalidate_counts(self):
        targets = await self.scopes()
        await self.estimates(targets)
        targets = targets[1:]
        await self.db.execute("""INSERT INTO novel_reading_progress
            (user_id,novel_id,chapter_id,paragraph_index,character_offset,chapter_percent)
            VALUES(1,1,1,0,8,1)""")
        self.assertAlmostEqual((await self.estimates(targets))[0]['remaining'], 258*.3,
                               msg='A new novel anchor must invalidate cached words')
        await self.db.execute('INSERT INTO novel_read_chapters(user_id,chapter_id) VALUES(1,2)')
        self.assertAlmostEqual((await self.estimates(targets))[0]['remaining'], 254*.3,
                               msg='Read chapters must invalidate cached words')
        await self.db.execute('INSERT INTO volumes(id,novel_id,number) VALUES(2,1,2)')
        await self.db.execute('UPDATE chapters SET volume_id=2 WHERE id=65')
        whole, volume = await self.estimates(targets)
        self.assertAlmostEqual(whole['remaining'], 254*.3)
        self.assertAlmostEqual(volume['remaining'], 250*.3, msg='Volume membership must invalidate cached words')
        # Same number of UTF-8 bytes, but one word instead of four.
        await self.db.execute("UPDATE chapters SET content_html='<p>onetwothreefour   </p>' WHERE id=3")
        whole, volume = await self.estimates(targets)
        self.assertAlmostEqual(whole['remaining'], 251*.3, msg='Same-size chapter edits must invalidate cached words')
        await self.db.execute('UPDATE chapters SET content_html=NULL WHERE id=3')
        unavailable, _ = await self.estimates(targets)
        self.assertIsNone(unavailable['remaining'], 'Missing content must not reuse a complete old estimate')
        self.assertEqual(unavailable['state'], 'content_unavailable')

    async def test_legacy_and_corrupt_snapshot_upgrade_without_erasing_history(self):
        await self.book()
        await self.evidence()
        target = [{'kind':'item','id':1}]
        expected = await self.estimates(target)
        cache_path = settings.data_dir / 'cache' / 'reading-estimates.sqlite3'
        with closing(sqlite3.connect(cache_path)) as cache, cache:
            cache.execute('UPDATE snapshots SET payload=?', (json.dumps([99,99,0]),))
        rendered = await build_estimator(self.db, 1, cached_only=True)
        await rendered.prepare_items([{'id':1,'file_format':'epub'}])
        self.assertIsNotNone(rendered.item_estimates[1], 'Legacy duration remains visible until upgraded')
        self.assertEqual(rendered.item_estimates[1].remaining, 99, 'Legacy duration remains visible until upgraded')
        before = tuple(await self.row('SELECT words,seconds,samples FROM account_pace_totals'))
        self.assertEqual(await self.estimates(target), expected, 'The endpoint must upgrade a legacy duration to exact words')
        with self.no_source_work():
            self.assertEqual(await self.estimates(target), expected)
        from app.stats.text_inventory import _CACHE_VERSION
        corrupt = ['{broken'] + [json.dumps({'format':2,'inventory_version':_CACHE_VERSION,'words':words})
                                 for words in ([True,6], [7,6], [0,10**400])]
        for payload in corrupt:
            with closing(sqlite3.connect(cache_path)) as cache, cache:
                cache.execute('UPDATE snapshots SET payload=?', (payload,))
            self.assertEqual(await self.estimates(target), expected, 'Corrupt derived cache must be rebuildable')
        with patch('app.stats.text_inventory._CACHE_VERSION', _CACHE_VERSION+1):
            invalidated = await build_estimator(self.db, 1, cached_only=True)
            await invalidated.prepare_items([{'id':1,'file_format':'epub'}])
            self.assertIsNone(invalidated.item_estimates[1], 'Inventory semantics must invalidate cached word counts')
            self.assertEqual(await self.estimates(target), expected)
            with self.no_source_work():
                self.assertEqual(await self.estimates(target), expected)
        self.assertEqual(tuple(await self.row('SELECT words,seconds,samples FROM account_pace_totals')), before)


del _Fixtures
