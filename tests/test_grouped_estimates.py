"""Requested series share preparation without widening account or work scopes."""
from pathlib import Path
from unittest.mock import patch

from app import text_replacements
from app.library.router import _series_reading, reading_estimates
from app.stats.estimates import build_estimator
from tests.test_estimates import EstimateCase
from tests.test_estimate_reuse import EstimateReuse as _Fixtures
from tests.test_text_eta_cards import JsonRequest


class GroupedEstimates(EstimateCase):
    book = _Fixtures.book
    evidence = _Fixtures.evidence
    replacement_scopes = _Fixtures.replacement_scopes

    async def test_mixed_targets_keep_order_account_scope_and_unread_boundaries(self):
        await self.replacement_scopes()
        await self.book(5)  # Orphan: only the explicit item target may include it.
        private = await self.book(6)
        await self.db.execute("INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(3,1,'Private','private','/private')")
        await self.db.execute('UPDATE items SET series_id=3 WHERE id=6')
        await self.progress(2, 'completed', 100)
        await self.progress(3, 'reading', 10)  # Legacy percent, no source anchor.
        await self.db.execute('INSERT INTO novel_read_chapters(user_id,chapter_id) VALUES(1,1)')
        await self.db.execute("INSERT INTO users(id,username,password_hash) VALUES(2,'other','x')")
        await self.db.execute("""INSERT INTO page_reads(user_id,kind,started_at,seconds,cap_seconds,
            visit_id,pace_words,pace_seconds,pace_samples)
            VALUES(2,'epub',datetime('now'),0,0,'other',1200,360,3)""")
        await self.db.execute("""INSERT INTO text_replacements(user_id,series_id,find_text,replace_text)
            VALUES(2,1,'one','one extra extra')""")
        await self.progress(1, 'completed', 100, user_id=2)
        await self.db.commit()
        targets = [
            {'kind':'item','id':2}, {'kind':'series','id':2},
            {'kind':'novel','id':1,'volume_id':3}, {'kind':'item','id':5},
            {'kind':'series','id':1}, {'kind':'series','id':999},
            {'kind':'novel','id':2}, {'kind':'novel','id':1,'volume_id':1},
            {'kind':'novel','id':1}, {'kind':'series','id':2},
            {'kind':'item','id':3}, {'kind':'item','id':4},
            {'kind':'novel','id':1,'volume_id':2},  # Volume belongs to novel 2.
        ]
        original_stat = Path.stat
        statements = []

        def stat(path, *args, **kwargs):
            if str(path) == private['file_path']:
                self.fail('Grouped requests must not inspect unrequested series')
            return original_stat(path, *args, **kwargs)

        await self.db.set_trace_callback(statements.append)
        try:
            with patch.object(Path, 'stat', stat):
                first = (await reading_estimates(JsonRequest(targets), {'id':1}, self.db))['estimates']
        finally:
            await self.db.set_trace_callback(None)
        self.assertEqual(sum('WHERE items.series_id IS NOT NULL' in sql for sql in statements), 1,
                         'All requested series must use one unread-items query')
        expected_keys = list(dict.fromkeys(f"{t['kind']}:{t['id']}:{t.get('volume_id') or ''}" for t in targets))
        self.assertEqual([r['key'] for r in first], expected_keys)
        self.assertEqual([None if r['remaining'] is None else round(r['remaining'], 5) for r in first],
                         [None, 1.8, 2.1, 1.8, 2.1, None, 1.2, None, 2.1, None, 1.8, None])
        self.assertEqual(first[1]['missing'], 1)
        self.assertEqual(first[1]['state'], 'position_unavailable')
        self.assertEqual(first[0]['state'], 'complete')
        self.assertEqual(first[5]['state'], 'complete')
        with patch.object(Path, 'stat', stat):
            second = (await reading_estimates(JsonRequest(targets), {'id':2}, self.db))['estimates']
        for index in (1,4,8):
            self.assertIsNotNone(second[index]['remaining'],
                                 'Grouped progress must belong to the requesting account')
        self.assertAlmostEqual(second[1]['remaining'], 3.6, msg='Grouped progress must belong to the requesting account')
        self.assertEqual(second[1]['missing'], 0)
        self.assertAlmostEqual(second[4]['remaining'], 2.4, msg='Grouped rules must belong to the requesting account')
        self.assertAlmostEqual(second[8]['remaining'], 2.4, msg='Read chapters must belong to the requesting account')

    async def test_empty_single_and_multiple_series_have_explicit_query_scopes(self):
        await self.replacement_scopes()
        model = await build_estimator(self.db, 1)
        statements = []
        await self.db.set_trace_callback(statements.append)
        try:
            self.assertEqual(await _series_reading(self.db, 1, model, series_ids=[]), {},
                             'Empty targets must not read the whole shelf')
            self.assertEqual(statements, [], 'An empty target set must not read the whole shelf')
            single = await _series_reading(self.db, 1, model, series_ids=[1])
            self.assertEqual(set(single), {1})
            self.assertTrue(any('AND items.series_id = 1' in sql for sql in statements),
                            'One series must retain the simple query path')
            self.assertFalse(any('series_id IN' in sql for sql in statements),
                             'One series must retain its lazy rule query')
            statements.clear()
            multiple = await _series_reading(self.db, 1, await build_estimator(self.db, 1), series_ids=[2,1,2])
            self.assertEqual(set(multiple), {1,2})
            self.assertEqual(sum('WHERE items.series_id IS NOT NULL' in sql for sql in statements), 1)
            all_series = await _series_reading(self.db, 1, await build_estimator(self.db, 1))
            self.assertEqual(multiple, all_series, 'Home and requested scopes must use the same aggregation')
        finally:
            await self.db.set_trace_callback(None)

    async def test_bulk_rules_are_bounded_ordered_and_account_and_work_scoped(self):
        await self.db.executemany(
            'INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(?,1,?,?,?)',
            [(i, f'S{i}', f's{i}', f'/s{i}') for i in range(1,502)])
        await self.db.execute("INSERT INTO users(id,username,password_hash) VALUES(2,'other','x')")
        await self.novel_chapter(1)
        await self.db.executemany('INSERT INTO text_replacements(user_id,series_id,find_text,replace_text) VALUES(?,?,?,?)',
                                 [(1,1,'a','b'),(1,1,'b','c'),(1,501,'x','y'),(2,1,'a','private')])
        await self.db.execute("INSERT INTO text_replacements(user_id,novel_id,find_text,replace_text) VALUES(1,1,'a','novel')")
        await self.db.commit()
        statements = []
        await self.db.set_trace_callback(statements.append)
        try:
            empty = await text_replacements.rules_for_series(self.db, 1, [])
            self.assertEqual(empty, {})
            self.assertEqual(statements, [])
            rules = await text_replacements.rules_for_series(self.db, 1, [*range(501,0,-1),1,None])
        finally:
            await self.db.set_trace_callback(None)
        self.assertEqual(set(rules), set(range(1,502)), 'Bulk rules must retain empty requested series')
        self.assertEqual(rules[1], [{'find':'a','replace':'b'}, {'find':'b','replace':'c'}],
                         'Bulk rules must preserve replacement order')
        self.assertEqual(rules[250], [], 'Empty rules must be retained for reuse')
        self.assertEqual(rules[501], [{'find':'x','replace':'y'}])
        queries = [sql for sql in statements if 'FROM text_replacements' in sql]
        self.assertEqual(len(queries), 2, 'Bulk rules must use bounded queries of at most 500 targets')
        self.assertTrue(all('user_id = 1' in sql for sql in queries))
        other = await text_replacements.rules_for_series(self.db, 2, [1])
        self.assertEqual(other, {1:[{'find':'a','replace':'private'}]}, 'Bulk rules must not leak another account')


del _Fixtures
