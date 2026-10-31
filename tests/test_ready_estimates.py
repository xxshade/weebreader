"""The ready projection must remain equivalent to the native format-specific path."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import sqlite3
from unittest.mock import patch

from app.database import get_connection, write_transaction
from app.library.router import _series_reading_uncached, _novel_reading_uncached, _series_reading
from app.stats import estimate_snapshots as snapshots
from app.stats.estimates import build_estimator
from app.stats.ready_estimates import cache, revisions, ReadyEstimateUpdates
from tests.test_estimates import EstimateCase
from tests import test_word_estimates as word_tests


class ReadyEstimates(EstimateCase):
    book = word_tests.WordEstimates.book
    evidence = word_tests.WordEstimates.evidence

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await cache.stop()
        self.addAsyncCleanup(cache.stop)
        for identity in (1, 2):
            await self.db.execute('INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(?,1,?,?,?)',
                                  (identity, f'S{identity}', f's{identity}', f'/s{identity}'))
        for identity in (1, 2):
            await self.book(identity)
            await self.db.execute('UPDATE items SET series_id=? WHERE id=?', (identity, identity))
        await self.db.commit()
        await self.read_comic(9, series_id=3, pages=100, seconds=1000)
        await self.item(10, fmt='cbz', series_id=1, pages=10)  # Mixed EPUB/comic series.
        await self.item(11, fmt='cbz', series_id=4, pages=20)  # Uses conservative fallback.
        for identity in (1, 2, 3):
            await self.novel_chapter(identity)
        await self.db.execute("UPDATE chapters SET content_html='<p>one two three four</p>',content_size=25")
        await self.db.execute('INSERT INTO volumes(id,novel_id,number) VALUES(2,1,2)')
        await self.db.execute('UPDATE chapters SET volume_id=2 WHERE id=3')
        await self.db.commit()
        await self.evidence()
        # Persist the native word counts, without touching the projection.
        native = await build_estimator(self.db, 1)
        await _series_reading_uncached(self.db, 1, native)
        await _novel_reading_uncached(self.db, 1, native)
        await native.prepare_novel(1, 1)
        await native.prepare_novel(1, 2)
        cache.enabled = True
        cache._current_path()

    async def state(self, uid=1, **kwargs):
        estimator = await build_estimator(self.db, uid, cached_only=True)
        return await cache.get(self.db, uid, estimator, **kwargs)

    async def equivalent(self):
        state = await self.state()
        self.assertIsNotNone(state)
        native = await build_estimator(self.db, 1, cached_only=True)
        self.assertEqual(state.series, await _series_reading_uncached(self.db, 1, native))
        self.assertEqual(state.novels, await _novel_reading_uncached(self.db, 1, native))
        for identity, volume in state.novel_states:
            await native.prepare_novel(identity, volume)
            self.assertEqual(state.novel_estimates.get((identity, volume)), native.for_novel(identity, volume))
        return state

    async def test_all_formats_mixed_series_and_volume_scope_hit_without_decode_or_source_queries(self):
        state = await self.equivalent()
        self.assertGreater(state.series[1].remaining, state.item_estimates[1].remaining)
        self.assertGreater(state.novel_estimates[(1, None)].remaining, state.novel_estimates[(1, 1)].remaining)
        with patch('app.stats.estimate_snapshots.load', side_effect=AssertionError('decoded stable snapshot')):
            again = await self.state()
        self.assertEqual(again.series, state.series)
        self.assertEqual(again.novels, state.novels)
        parsed = {'item:1:': ('item', 1, None), 'series:1:': ('series', 1, None),
                  'novel:1:': ('novel', 1, None), 'novel:1:1': ('novel', 1, 1)}
        result = await cache.api(self.db, 1, await build_estimator(self.db, 1), parsed)
        self.assertEqual([row['key'] for row in result['estimates']], list(parsed))
        self.assertEqual(result['estimates'][2]['remaining'], state.novels[1].remaining)
        self.assertEqual(result['estimates'][3]['remaining'], state.novel_estimates[(1, 1)].remaining)

    async def test_authoritative_fallback_can_prepare_an_item_missing_from_the_projection(self):
        await self.book(5)
        await self.db.execute('UPDATE items SET series_id=2 WHERE id=5')
        await self.db.commit()
        await self.state()  # This source has no persisted word-count snapshot yet.
        estimator = await build_estimator(self.db, 1)
        result = await cache.api(self.db, 1, estimator, {'item:5:': ('item', 5, None)})
        self.assertIsNone(result)
        self.assertNotIn(5, estimator.item_estimates, 'A fallback must not inherit unprepared None entries')
        item = await self.row('SELECT * FROM items WHERE id=5')
        await estimator.prepare_items([item])
        self.assertIsNotNone(estimator.item_estimates[5])

    async def test_newly_sufficient_pace_does_not_parse_every_cold_book_in_a_commit(self):
        await self.book(5)
        await self.db.execute('UPDATE items SET series_id=2 WHERE id=5')
        await self.db.execute('DELETE FROM account_pace_totals')
        await self.db.commit()
        await self.state()
        await self.evidence()
        with ExitStack() as stack:
            for name in ('inventory_epub', 'inventory_epub_total', 'inventory_html', 'inventory_html_total'):
                stack.enter_context(patch('app.stats.estimates.' + name, side_effect=AssertionError('cold source parsed')))
            await cache._refresh(1, authoritative=True)
        self.assertIsNone(cache.states[1].item_estimates[5])
        self.assertEqual(cache.states[1].item_states[5], 'calculating')

    async def test_epub_progress_rebuilds_its_series_and_eagerly_persists_the_new_anchor(self):
        before = await self.state()
        await self.progress(1, 'reading', 1)
        await self.db.execute("UPDATE reading_progress SET current_location='elidx:0:4' WHERE item_id=1")
        await self.db.commit()
        with patch('app.library.router._series_reading_uncached', wraps=_series_reading_uncached) as work:
            await cache._refresh(1, authoritative=True)
        self.assertTrue(work.call_args_list)
        self.assertTrue(all(call.args[3] == [1] for call in work.call_args_list), work.call_args_list)
        after = await self.equivalent()
        self.assertLess(after.item_estimates[1].remaining, before.item_estimates[1].remaining)
        self.assertEqual(after.series[2], before.series[2])

    async def test_text_pace_updates_both_epubs_and_novels_without_source_traversal(self):
        before = await self.state()
        await self.db.execute('UPDATE account_pace_totals SET seconds=seconds*2 WHERE user_id=1')
        await self.db.commit()
        with ExitStack() as stack:
            for name in ('inventory_epub', 'inventory_epub_total', 'inventory_html', 'inventory_html_total'):
                stack.enter_context(patch('app.stats.estimates.' + name, side_effect=AssertionError('source traversed')))
            after = await self.equivalent()
        self.assertAlmostEqual(after.item_estimates[2].remaining, before.item_estimates[2].remaining * 2)
        self.assertAlmostEqual(after.novels[1].remaining, before.novels[1].remaining * 2)

    async def test_comic_progress_retimes_other_series_fallback_and_mixed_totals(self):
        before = await self.state()
        await self.progress(9, 'reading', 50)
        after = await self.equivalent()
        self.assertNotEqual(after.series[4].remaining, before.series[4].remaining)
        self.assertNotEqual(after.series[1].remaining, before.series[1].remaining)

    async def test_novel_anchor_delta_uses_only_current_chapter_and_preserves_other_volume(self):
        before = await self.state()
        await self.db.execute("""INSERT INTO novel_reading_progress
            (user_id,novel_id,chapter_id,paragraph_index,character_offset,chapter_percent)
            VALUES(1,1,1,0,4,1)""")
        await self.db.commit()
        with patch('app.stats.estimates.inventory_html_total', side_effect=AssertionError('summed all chapters')):
            await cache._refresh(1, authoritative=True)
        after = await self.equivalent()
        self.assertAlmostEqual(after.novels[1].remaining, before.novels[1].remaining - .3)
        self.assertEqual(after.novel_estimates[(1, 2)], before.novel_estimates[(1, 2)])
        await self.db.execute('UPDATE novel_reading_progress SET character_offset=0 WHERE user_id=1')
        await self.db.commit()
        with patch('app.stats.estimates.inventory_html_total', side_effect=AssertionError('summed all chapters')):
            await cache._refresh(1, authoritative=True)
        restored = await self.equivalent()
        self.assertAlmostEqual(restored.novels[1].remaining, before.novels[1].remaining)
        for chapter, offset in ((1, 8), (1, 4), (3, 4), (1, 0)):
            await self.db.execute('UPDATE novel_reading_progress SET chapter_id=?,character_offset=? WHERE user_id=1',
                                  (chapter, offset))
            await self.db.commit()
            await cache._refresh(1, authoritative=True)
            actual = dict(cache.states[1].novel_estimates)
            native = await build_estimator(self.db, 1)
            with patch('app.stats.estimate_snapshots.load', return_value=None):
                for novel, volume in actual:
                    await native.prepare_novel(novel, volume)
            self.assertEqual(actual, native.novel_estimates)

    async def test_changed_chapter_membership_and_rules_do_not_use_an_old_delta_basis(self):
        await self.state()
        await self.db.execute('INSERT INTO novel_read_chapters(user_id,chapter_id) VALUES(1,1)')
        await self.db.commit()
        from app.stats.estimates import inventory_html_total
        with patch('app.stats.estimates.inventory_html_total', wraps=inventory_html_total) as source:
            await cache._refresh(1, authoritative=True)
        self.assertGreater(source.call_count, 0)
        await self.equivalent()
        await self.db.execute("INSERT INTO text_replacements(user_id,novel_id,find_text,replace_text) VALUES(1,1,'one','six ten')")
        await self.db.commit()
        with patch('app.stats.estimates.inventory_html_total', wraps=inventory_html_total) as source:
            await cache._refresh(1, authoritative=True)
        self.assertGreater(source.call_count, 0)
        await self.equivalent()

    async def test_series_api_upgrades_legacy_duration_instead_of_returning_it_forever(self):
        state = await self.state()
        scope = snapshots.signature((state.account, 'item', 1))
        with snapshots._connection() as db:
            db.execute("UPDATE snapshots SET payload='[999,1000,0]' WHERE scope=?", (scope,))
        estimator = await build_estimator(self.db, 1)
        self.assertIsNone(await cache.api(self.db, 1, estimator, {'series:1:': ('series', 1, None)}))
        self.assertNotIn(1, estimator.item_estimates)
        result = await _series_reading_uncached(self.db, 1, estimator, [1])
        self.assertLess(result[1].remaining, 999)

    async def test_chapter_order_and_newest_metadata_invalidate_without_body_edits(self):
        before = await self.state()
        await self.db.execute('UPDATE chapters SET order_index=order_index+1 WHERE id=1')
        await self.db.commit()
        after = await self.equivalent()
        self.assertIsNotNone(before.novels[1].remaining)
        self.assertIsNone(after.novels[1].remaining)
        await self.db.execute("UPDATE chapters SET discovered_at='2099-01-01' WHERE id=1")
        await self.db.commit()
        self.assertEqual((await self.equivalent()).novels[1].newest, '2099-01-01')

    async def test_acknowledgement_does_not_wait_for_future_notifications_forever(self):
        from app.stats.ready_estimates import ReadyEstimates as ReadyCache
        candidate = ReadyCache()
        candidate.enabled = True
        candidate.states[1] = None
        rounds = []
        async def continuously_dirtied(uid, **kwargs):
            rounds.append(id(asyncio.current_task()))
            candidate.notify()
            await asyncio.sleep(0)
        with patch.object(candidate, '_refresh', side_effect=continuously_dirtied):
            candidate.notify()
            first = candidate.task
            try:
                await asyncio.wait_for(candidate.flush(), .5)
                self.assertLessEqual(rounds.count(id(first)), 2)
            finally:
                await candidate.stop()

    async def test_novel_progress_marking_content_and_volume_move_invalidate(self):
        await self.state()
        await self.db.execute('INSERT INTO novel_read_chapters(user_id,chapter_id) VALUES(1,1)')
        await self.db.commit()
        state = await self.equivalent()
        self.assertEqual(state.novels[1].unread, 2)
        await self.db.execute("UPDATE chapters SET content_html='<p>six ten end</p>' WHERE id=2")
        await self.db.execute('UPDATE chapters SET volume_id=2 WHERE id=2')
        await self.db.commit()
        await self.equivalent()
        await cache._refresh(1, authoritative=True)
        await self.equivalent()

    async def test_snapshot_commit_cannot_lose_invalidation_and_rollback_does_not_change_revision(self):
        before = await self.state()
        scope = (before.account, 'item', 1)
        fingerprint = snapshots.read()[snapshots.signature(scope)][0]
        version = snapshots.version()
        with snapshots._connection() as db:
            db.execute("UPDATE snapshots SET payload='broken'")
            db.rollback()
        self.assertEqual(snapshots.version(), version)
        snapshots.save(scope, fingerprint, snapshots.WordCounts(1, 50))  # No main-DB notification.
        self.assertNotEqual(snapshots.version(), version)
        after = await self.equivalent()
        self.assertNotEqual(after.item_estimates[1], before.item_estimates[1])

    async def test_missing_snapshot_store_and_unavailable_revision_never_serve_unvalidated_projection(self):
        before = await self.state()
        path = Path(self._tmp.name) / 'cache' / 'reading-estimates.sqlite3'
        path.unlink()
        after = await self.equivalent()
        self.assertNotEqual(before.snapshot_version[0], after.snapshot_version[0])
        with patch('app.stats.estimate_snapshots.version', side_effect=sqlite3.OperationalError('unavailable')):
            self.assertIsNone(await self.state())
        self.assertNotIn(1, cache.states)

    async def test_file_change_without_watcher_and_rule_edit_are_revalidated(self):
        await self.state()
        file = Path((await self.row('SELECT file_path FROM items WHERE id=1'))[0])
        file.write_bytes(file.read_bytes() + b'\nZIP trailer')
        await self.equivalent()
        await self.db.execute("INSERT INTO text_replacements(user_id,series_id,find_text,replace_text) VALUES(1,1,'one','six ten')")
        await self.db.commit()
        await self.equivalent()

    async def test_concurrent_pace_commit_retries_with_fresh_estimator(self):
        await self.state()
        calls = 0
        async def race(db, uid):
            nonlocal calls
            calls += 1
            if calls == 2:
                other = await get_connection()
                try:
                    await other.execute('UPDATE account_pace_totals SET seconds=seconds+300 WHERE user_id=1')
                    await other.commit()
                finally:
                    await other.close()
            return await revisions(db, uid)
        with patch('app.stats.ready_estimates.revisions', side_effect=race):
            await self.state()
        self.assertGreater(calls, 2)
        await self.equivalent()

    async def test_account_namespace_reuse_and_lru_limits(self):
        await self.state()
        await self.db.execute("INSERT INTO users(id,username,password_hash) VALUES(2,'other','y')")
        await self.db.commit()
        with patch('app.stats.ready_estimates.MAX_ACCOUNTS', 1):
            other = await self.state(2)
            self.assertIsNone(other.item_estimates.get(1))
            self.assertEqual(list(cache.states), [2])
        await self.db.execute("UPDATE users SET password_hash='changed' WHERE id=1")
        await self.db.commit()
        await self.equivalent()
        cache.states.clear()
        with patch('app.stats.ready_estimates.MAX_ENTRIES', 1):
            self.assertIsNone(await self.state())
            self.assertNotIn(1, cache.states)
            self.assertIsNone(await self.state())

    async def test_warmup_allows_native_reads_and_cancels_on_shutdown(self):
        started = asyncio.Event()
        never = asyncio.Event()
        async def blocked(uid):
            started.set()
            await never.wait()
        with patch.object(cache, '_refresh', side_effect=blocked):
            await cache.start()
            await asyncio.wait_for(started.wait(), 2)
            estimator = await build_estimator(self.db, 1, cached_only=True)
            self.assertIsNone(await cache.get(self.db, 1, estimator))
            result = await asyncio.wait_for(_series_reading(self.db, 1, estimator), 2)
            self.assertEqual(result, await _series_reading_uncached(self.db, 1, estimator))
            task = cache.warmup
            await cache.stop()
            self.assertTrue(task.cancelled())

    async def test_stop_during_connection_acquisition_closes_the_connection_after_open(self):
        from unittest.mock import AsyncMock
        started, release = asyncio.Event(), asyncio.Event()
        connection = AsyncMock()
        async def opening():
            started.set()
            await release.wait()
            return connection
        with patch('app.database.get_connection', side_effect=opening):
            await cache.start()
            await asyncio.wait_for(started.wait(), 1)
            stopping = asyncio.create_task(cache.stop())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(stopping, 1)
        connection.close.assert_awaited_once()

    async def test_successful_http_mutation_refreshes_after_writer_lock_before_headers(self):
        before = await self.state()
        async def app(scope, receive, send):
            async with write_transaction() as db:
                await db.execute('UPDATE account_pace_totals SET seconds=seconds*2 WHERE user_id=1')
                await db.commit()
            await send({'type': 'http.response.start', 'status': 204, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})
        async def send(message):
            if message['type'] == 'http.response.start':
                self.assertGreater(cache.states[1].item_estimates[1].remaining, before.item_estimates[1].remaining)
        await asyncio.wait_for(ReadyEstimateUpdates(app)({'type': 'http', 'method': 'POST'}, None, send), 4)
        await self.equivalent()
