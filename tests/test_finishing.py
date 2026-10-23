"""A book is finished once, however many times the reader says so.

The readers post `completed` on every save from the end on: the comic
reader on each turn on the last page and again when the page is left, the
text reader for anything past 98%. Each of those used to restamp
`completed_at` -- the finish date the trackers are sent -- and queue another
push. Now the save that FINISHES the book does both, and the ones after it
do neither.

Pushes for one shelf also no longer run side by side. Two at once (the
last page's save and the beacon on leaving) both read "not sent yet" and
both sent; a second request now waits for the first and runs once after it.
"""
import asyncio
import unittest

from app.reader import router as reader_router
from app.tracking import service
from tests.test_progress_validation import ProgressCase


class FinishedOnce(ProgressCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.db.execute("INSERT INTO series (id, library_id, name, sort_name, folder_path)"
                              " VALUES (1, 1, 'S', 's', 'S')")
        await self.db.execute("UPDATE items SET series_id = 1 WHERE id = 1")
        await self.db.commit()
        self.pushed = []
        original = reader_router.tracking.queue_push
        reader_router.tracking.queue_push = lambda user_id, **kw: self.pushed.append(kw)
        self.addCleanup(lambda: setattr(reader_router.tracking, "queue_push", original))

    async def completed_at(self):
        row = await (await self.db.execute("SELECT completed_at FROM reading_progress")).fetchone()
        return row["completed_at"]

    async def backdate(self):
        await self.db.execute("UPDATE reading_progress SET completed_at = '2020-01-01 00:00:00'")
        await self.db.commit()

    async def test_reaching_the_end_stamps_it_and_pushes(self):
        await self.post({"current_page": 9, "progress_percent": 100, "status": "completed"})
        self.assertIsNotNone(await self.completed_at())
        self.assertEqual(self.pushed, [{"series_id": 1}])

    async def test_saving_again_at_the_end_changes_neither(self):
        await self.post({"current_page": 9, "progress_percent": 100, "status": "completed"})
        await self.backdate()
        await self.post({"current_page": 9, "progress_percent": 100, "status": "completed"})
        self.assertEqual(await self.completed_at(), "2020-01-01 00:00:00")
        self.assertEqual(len(self.pushed), 1)

    async def test_reading_it_again_to_the_end_is_a_new_finish(self):
        await self.post({"current_page": 9, "progress_percent": 100, "status": "completed"})
        await self.backdate()
        await self.post({"current_page": 2, "progress_percent": 30, "status": "reading"})
        await self.post({"current_page": 9, "progress_percent": 100, "status": "completed"})
        self.assertNotEqual(await self.completed_at(), "2020-01-01 00:00:00")
        self.assertEqual(len(self.pushed), 2)

    async def test_a_book_first_opened_at_its_end_is_finished_by_that_save(self):
        # No row at all before it: the first save is the transition.
        await self.post({"current_page": 9, "progress_percent": 100, "status": "completed"})
        self.assertEqual(len(self.pushed), 1)


class MarkingReadAgainKeepsTheDate(FinishedOnce):
    """"Mark as read" on something already read is not a new finish either
    -- and on a whole series it moved every volume's date to today."""

    async def test_marking_the_series_keeps_a_finished_volume_s_date(self):
        from app.reading_progress import mark_series_read

        await self.post({"current_page": 9, "progress_percent": 100, "status": "completed"})
        await self.backdate()
        await mark_series_read(self.db, 1, 1)
        await self.db.commit()
        self.assertEqual(await self.completed_at(), "2020-01-01 00:00:00")

    async def test_marking_the_item_keeps_it_too(self):
        from app.reading_progress import mark_item_read

        await self.post({"current_page": 9, "progress_percent": 100, "status": "completed"})
        await self.backdate()
        await mark_item_read(self.db, 1, 1, 9)
        await self.db.commit()
        self.assertEqual(await self.completed_at(), "2020-01-01 00:00:00")

    async def test_an_unread_one_is_stamped_now(self):
        from app.reading_progress import mark_series_read

        await mark_series_read(self.db, 1, 1)
        await self.db.commit()
        self.assertIsNotNone(await self.completed_at())


class OnePushPerShelfAtATime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.running = 0
        self.most = 0
        self.calls = 0
        original = service.push

        async def slow_push(user_id, **kw):
            self.calls += 1
            self.running += 1
            self.most = max(self.most, self.running)
            await asyncio.sleep(0.05)
            self.running -= 1
            return "sent"

        service.push = slow_push
        self.addCleanup(lambda: setattr(service, "push", original))

    async def test_requests_meanwhile_become_one_more_run_after(self):
        for _ in range(4):
            service.queue_push(1, series_id=7)
        await service.drain()
        self.assertEqual(self.most, 1)
        self.assertEqual(self.calls, 2)
        self.assertEqual(service._pushing, {})

    async def test_different_shelves_still_go_together(self):
        service.queue_push(1, series_id=7)
        service.queue_push(1, series_id=8)
        await service.drain()
        self.assertEqual(self.most, 2)
        self.assertEqual(self.calls, 2)

    async def test_a_push_that_raises_does_not_block_the_next(self):
        async def broken(user_id, **kw):
            raise RuntimeError("boom")

        service.push = broken
        with self.assertLogs('background', level='ERROR') as logs:
            service.queue_push(1, series_id=7)
            await service.drain()
        self.assertEqual(len(logs.records), 1)
        self.assertIn('RuntimeError: boom', logs.output[0])
        self.assertEqual(service._pushing, {})


if __name__ == "__main__":
    unittest.main()
