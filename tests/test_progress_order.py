"""A late save from one EPUB opening must not undo a newer reading action."""
from unittest.mock import patch
from fastapi import HTTPException
from tests.test_progress_validation import ProgressCase

SESSION = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'


class OrderedProgress(ProgressCase):
    async def save(self, sequence, status='reading', session=SESSION):
        return await self.post({'save_session': session, 'save_sequence': sequence,
                               'status': status, 'current_page': sequence,
                               'current_location': f'elidx:{sequence}',
                               'progress_percent': 100 if status == 'completed' else 25})

    async def test_late_and_duplicate_saves_do_not_replace_completion_or_emit_progress(self):
        await self.save(2, 'completed')
        with patch('app.reader.router._publish_progress') as publish:
            for sequence in (1, 2):
                await self.save(sequence)
            publish.assert_not_called()
        self.assertEqual(dict(await self.stored()), {
            'status': 'completed', 'progress_percent': 100.0, 'current_page': 2})

    async def test_a_newer_page_turn_can_reopen_and_move_backwards(self):
        await self.save(2, 'completed')
        await self.save(3)
        self.assertEqual(dict(await self.stored()), {
            'status': 'reading', 'progress_percent': 25.0, 'current_page': 3})
        await self.save(1, 'completed')
        self.assertEqual((await self.stored())['status'], 'reading')

    async def test_other_openings_have_independent_counters(self):
        await self.save(20, 'completed')
        await self.save(1, session=OTHER)
        await self.save(19, 'completed')
        self.assertEqual((await self.stored())['status'], 'reading')

    async def test_ignored_exit_still_blocks_older_page_saves(self):
        await self.save(2, 'completed')
        await self.post({'save_session': SESSION, 'save_sequence': 4,
                         'status': 'reading', 'on_exit': True, 'progress_percent': 25})
        await self.save(3)
        self.assertEqual((await self.stored())['status'], 'completed')

    async def test_order_metadata_is_validated_before_writing(self):
        for fields in ({'save_session': SESSION}, {'save_sequence': 1},
                       {'save_session': 'bad', 'save_sequence': 1},
                       *({'save_session': SESSION, 'save_sequence': value}
                         for value in (None, True, 0, -1, 1.5, '1', 9007199254740992))):
            with self.subTest(fields=fields):
                with self.assertRaises(HTTPException) as caught:
                    await self.post(fields)
                self.assertEqual(caught.exception.status_code, 400)
                self.assertIsNone(await self.stored())

    async def test_watermarks_survive_reinitialization_and_follow_item_deletion(self):
        from app.database import init_db
        await self.save(2, 'completed')
        await init_db()
        await self.save(1)
        self.assertEqual((await self.stored())['status'], 'completed')
        await self.db.execute('DELETE FROM items WHERE id=1')
        await self.db.commit()
        count = await (await self.db.execute('SELECT COUNT(*) FROM item_progress_saves')).fetchone()
        self.assertEqual(count[0], 0)
