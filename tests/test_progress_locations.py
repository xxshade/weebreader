"""Completing an EPUB never combines a new chapter with an old paragraph."""
from app.reading_progress import mark_series_read, mark_series_read_up_to
from tests.test_progress_validation import ProgressCase


class CompletedLocations(ProgressCase):
    async def test_collective_and_tracker_completion_clear_the_paragraph_with_the_chapter(self):
        await self.db.execute("INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(1,1,'S','s','S')")
        await self.db.execute("UPDATE items SET series_id=1,file_format='epub',page_count=NULL,number_in_series=1 WHERE id=1")
        for complete in (mark_series_read, mark_series_read_up_to):
            with self.subTest(operation=complete.__name__):
                await self.db.execute('DELETE FROM reading_progress')
                await self.db.execute("INSERT INTO reading_progress(user_id,item_id,status,current_page,current_location,progress_percent) VALUES(1,1,'reading',1,'elidx:3',58)")
                if complete is mark_series_read:
                    await complete(self.db, 1, 1)
                else:
                    await complete(self.db, 1, 1, 1)
                row = await (await self.db.execute('SELECT status,current_page,current_location,progress_percent,completed_from_percent FROM reading_progress')).fetchone()
                self.assertEqual(dict(row), {'status': 'completed', 'current_page': None,
                    'current_location': None, 'progress_percent': 100.0, 'completed_from_percent': 58.0})
        await self.db.commit()

    async def test_late_exit_cannot_undo_completion_but_a_new_page_turn_can(self):
        from unittest.mock import patch
        from app.reading_progress import mark_item_read

        await self.post({'current_page': 1, 'current_location': 'elidx:3:375',
                         'progress_percent': 60})
        await mark_item_read(self.db, 1, 1, None)
        await self.db.commit()
        with patch('app.reader.router._publish_progress') as publish:
            await self.post({'current_page': 1, 'current_location': 'elidx:3:375',
                             'progress_percent': 60, 'on_exit': True})
            self.assertEqual(dict(await self.stored()), {
                'status': 'completed', 'progress_percent': 100.0, 'current_page': None})
            publish.assert_not_called()
        await self.post({'current_page': 0, 'current_location': 'elidx:1',
                         'progress_percent': 5, 'on_exit': False})
        self.assertEqual(dict(await self.stored()), {
            'status': 'reading', 'progress_percent': 5.0, 'current_page': 0})

    async def test_exit_still_saves_an_unfinished_book(self):
        await self.post({'current_page': 1, 'current_location': 'elidx:3:375',
                         'progress_percent': 60, 'on_exit': True})
        self.assertEqual(dict(await self.stored()), {
            'status': 'reading', 'progress_percent': 60.0, 'current_page': 1})
