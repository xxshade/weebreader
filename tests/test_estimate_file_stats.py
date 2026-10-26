"""Every eligible EPUB is checked off-loop, in bounded request-local batches."""
import asyncio
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import threading
from unittest.mock import patch
import zipfile

from app.library.router import reading_estimates
from app.stats.estimates import build_estimator
from tests.test_estimates import EstimateCase
from tests.test_text_eta_cards import JsonRequest
from tests.test_word_estimates import WordEstimates as _Fixtures


class EstimateFileStats(EstimateCase):
    book = _Fixtures.book
    evidence = _Fixtures.evidence

    @contextmanager
    def source_checks(self, items, *, hold=None):
        paths = {row['file_path']: row['id'] for row in items}
        loop_thread = threading.get_ident()
        original_stat, original_offload = Path.stat, asyncio.to_thread
        local = threading.local()
        trace = {'checks': [], 'groups': [], 'done': threading.Event()}

        def stat(path, *args, **kwargs):
            identity = paths.get(str(path))
            if identity is not None:
                self.assertNotEqual(threading.get_ident(), loop_thread,
                                    'Source file checks must run outside the event loop')
                self.assertIsNotNone(getattr(local, 'group', None))
                local.group.append(identity)
                trace['checks'].append(identity)
                if hold is not None:
                    entered, release = hold
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('Test must release the file-check worker')
            return original_stat(path, *args, **kwargs)

        async def offload(fn, *args, **kwargs):
            def run():
                local.group = []
                try:
                    return fn(*args, **kwargs)
                finally:
                    if local.group:
                        trace['groups'].append(tuple(local.group))
                        trace['done'].set()
                    local.group = None
            return await original_offload(run)

        with patch.object(Path, 'stat', stat), patch('app.stats.estimates.asyncio.to_thread', offload):
            yield trace

    async def shelf(self, count):
        items = [await self.book(identity) for identity in range(1, count+1)]
        await self.db.execute("INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(1,1,'S','s','/s')")
        await self.db.execute('UPDATE items SET series_id=1')
        await self.evidence()
        return items

    async def request(self):
        return (await reading_estimates(JsonRequest([{'kind':'series','id':1}]), {'id':1}, self.db))['estimates'][0]

    async def test_warm_endpoint_and_html_check_every_eligible_file_in_bounded_batches(self):
        items = await self.shelf(65)
        skipped = [await self.book(identity) for identity in (66, 67, 68)]
        await self.db.execute('UPDATE items SET series_id=1')
        await self.progress(66, 'completed', 100)
        await self.progress(67, 'reading', 10)
        await self.progress(68, 'reading', 10)
        await self.db.execute("UPDATE reading_progress SET current_location='legacy' WHERE item_id=68")
        for item in skipped:
            Path(item['file_path']).unlink()
        expected = await self.request()
        self.assertAlmostEqual(expected['remaining'], 65*1.8, msg='Every eligible file must retain its estimate')
        self.assertEqual(expected['missing'], 2)
        for mode in ('endpoint', 'html'):
            with self.subTest(mode=mode), self.source_checks(items+skipped) as trace:
                if mode == 'endpoint':
                    self.assertEqual(await self.request(), expected)
                else:
                    model = await build_estimator(self.db, 1, cached_only=True)
                    with patch('app.stats.estimates.inventory_epub_total', side_effect=AssertionError('HTML parsed source')):
                        await model.prepare_items(items+skipped+[dict(items[0], id=999)])
                    self.assertEqual(model.item_states[66], 'complete')
                    self.assertEqual(model.item_states[67], 'position_unavailable')
                    self.assertEqual(model.item_states[68], 'position_unavailable')
                    self.assertAlmostEqual(model.total_remaining([dict(item, status=None) for item in items]), 65*1.8)
                self.assertEqual(Counter(trace['checks']), Counter(range(1,66)),
                                 'Every eligible file must be checked exactly once')
                self.assertEqual(sorted(map(len, trace['groups'])), [1,32,32],
                                 'Source checks must use bounded batches of at most 32 files')

    async def test_changed_missing_and_invalid_paths_remain_independent_and_retryable(self):
        items = await self.shelf(3)
        self.assertAlmostEqual((await self.request())['remaining'], 5.4)
        history = tuple(await self.row('SELECT COUNT(*),SUM(pace_words),SUM(pace_seconds) FROM page_reads'))
        changed, missing, invalid = [Path(item['file_path']) for item in items]
        with zipfile.ZipFile(changed) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries['a.xhtml'] = b'<p>one two three four five six seven eight</p>'
        with zipfile.ZipFile(changed, 'w') as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        missing_bytes = missing.read_bytes()
        missing.unlink()
        await self.db.execute('UPDATE items SET file_path=? WHERE id=3', (str(invalid)+'\0',))
        await self.db.commit()
        html = await build_estimator(self.db, 1, cached_only=True)
        try:
            await html.prepare_items(items)
        except (OSError, ValueError):
            self.fail('One bad path must not abort a batch')
        self.assertEqual(html.item_states[1], 'calculating', 'Changed files must invalidate saved counts')
        self.assertEqual(html.item_states[2], 'content_unavailable')
        self.assertEqual(html.item_states[3], 'content_unavailable')
        partial = await self.request()
        self.assertAlmostEqual(partial['remaining'], 3.0, msg='One failed stat must not discard healthy estimates')
        self.assertEqual(partial['missing'], 2)
        missing.write_bytes(missing_bytes)
        await self.db.execute('UPDATE items SET file_path=? WHERE id=3', (str(invalid),))
        await self.db.commit()
        repaired = await self.request()
        self.assertAlmostEqual(repaired['remaining'], 6.6, msg='Later requests must recheck repaired files')
        self.assertEqual(repaired['missing'], 0)
        self.assertEqual(tuple(await self.row('SELECT COUNT(*),SUM(pace_words),SUM(pace_seconds) FROM page_reads')), history)

    async def test_insufficient_evidence_and_completed_files_need_no_filesystem_checks(self):
        items = [await self.book(identity) for identity in (1, 2)]
        await self.progress(2, 'completed', 100)
        for item in items:
            Path(item['file_path']).unlink()
        with self.source_checks(items) as trace:
            model = await build_estimator(self.db, 1, cached_only=True)
            await model.prepare_items(items)
        self.assertEqual(model.item_states, {1:'collecting', 2:'complete'})
        self.assertEqual(trace['checks'], [], 'Ineligible items must not access the filesystem')

    async def test_cancellation_stops_before_scheduling_another_batch(self):
        items = await self.shelf(65)
        model = await build_estimator(self.db, 1, cached_only=True)
        entered, release = threading.Event(), threading.Event()
        with self.source_checks(items, hold=(entered, release)) as trace:
            task = asyncio.create_task(model.prepare_items(items))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(await asyncio.to_thread(trace['done'].wait, 5))
        self.assertEqual(len(trace['groups']), 1, 'Cancellation must not schedule another file-check batch')
        self.assertLessEqual(len(trace['checks']), 32, 'A cancelled request may finish at most its bounded active batch')


del _Fixtures
