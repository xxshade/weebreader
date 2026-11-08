"""Actual novel synchronization across durable scheduling and fresh workers."""

import asyncio
from contextlib import suppress
from unittest.mock import patch

from app.config import settings
from app.novels import schedule, sync as sync_mod
from tests.test_novel_sync import SyncCase, ref
from app.novels.scraper import ScrapedNovelMeta


class PersistentSchedule(SyncCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clock = 1_000_000.0
        self.actual_sync = sync_mod.sync_novel

        async def index(url):
            return ScrapedNovelMeta(
                title="N",
                cover_url=None,
                synopsis=None,
                status=None,
                chapters=list(self.site),
            )

        async def contents(urls, on_chapter_done=None, delay_seconds=None):
            self.positions_while_downloading = await self.order()
            for i, url in enumerate(urls, 1):
                await on_chapter_done(i, len(urls), url, True, "<p>x</p>")

        self.patches = [
            patch.object(schedule, "_now", lambda: self.clock),
            patch.object(sync_mod, "fetch_novel_index", index),
            patch.object(sync_mod, "fetch_all_chapter_contents", contents),
            patch.object(schedule.random, "uniform", return_value=0),
            patch.object(settings, "novel_scrape_interval_minutes", 360),
            patch.object(settings, "novel_scrape_request_delay_seconds", 0),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(schedule.stop)
        self.idle = asyncio.Event()
        self.wait_delays = []
        original_wait = schedule._wait

        async def observed_wait(delay):
            self.wait_delays.append(delay)
            # A pending notification returns immediately; only acknowledge a
            # genuinely idle worker, after all earlier notifications drained.
            if not schedule._changed.is_set():
                self.idle.set()
            await original_wait(delay)

        p = patch.object(schedule, "_wait", observed_wait)
        p.start()
        self.addCleanup(p.stop)

    async def sync(self, *, manual=False):
        return await self.actual_sync(1, manual=manual)

    async def state(self, novel=1):
        rows = await self.rows(
            "SELECT * FROM novel_sync_schedule WHERE novel_id=?", (novel,)
        )
        return rows[0] if rows else None

    async def seed(self, due, running=0, novel=1):
        await self.execute(
            "INSERT INTO novel_sync_schedule(novel_id,next_run_at,running) VALUES(?,?,?)",
            (novel, due, running),
        )

    async def pulse(self):
        self.idle.clear()
        schedule.notify()
        await asyncio.wait_for(self.idle.wait(), 3)

    async def test_future_deadline_survives_fresh_workers_and_interval_changes(self):
        due = self.clock + 10_000
        await self.seed(due)
        with patch.object(sync_mod, "sync_novel") as scrape:
            for interval in (360, 120):
                settings.novel_scrape_interval_minutes = interval
                with self.assertLogs("novels.schedule", "INFO") as log:
                    await schedule.start()
                self.assertIn("restored", log.output[0])
                await self.pulse()
                await schedule.stop()
                self.clock += 100
                self.assertEqual(
                    (await self.state())["next_run_at"],
                    due,
                    "A restart must retain the saved deadline",
                )
            scrape.assert_not_called()
        await self.sync(manual=True)
        self.assertEqual((await self.state())["next_run_at"], self.clock + 120 * 60)

    async def test_missing_overdue_and_interrupted_schedules_recover_once(self):
        for kind in ("missing", "overdue", "interrupted"):
            with self.subTest(kind=kind):
                await self.execute("DELETE FROM novel_sync_schedule")
                if kind != "missing":
                    await self.seed(
                        self.clock + (10000 if kind == "interrupted" else -100000),
                        running=int(kind == "interrupted"),
                    )
                calls = []
                actual = self.sync

                async def scrape(identity, **kwargs):
                    calls.append(identity)
                    return await actual()

                with patch.object(sync_mod, "sync_novel", scrape):
                    await schedule.start()
                    await self.pulse()
                    self.assertEqual(
                        calls, [1], "Recover exactly one missed or interrupted attempt"
                    )
                    self.assertEqual(
                        await self.state(),
                        dict(novel_id=1, next_run_at=self.clock + 21600, running=0),
                    )
                    await schedule.stop()
                    await schedule.start()
                    await self.pulse()
                    self.assertEqual(
                        calls, [1], "A completed recovery must not repeat on restart"
                    )
                    await schedule.stop()

    async def test_manual_route_postpones_only_its_novel(self):
        from app.novels import router

        await self.execute(
            "INSERT INTO novels(id,title,source_url) VALUES(2,'Other','https://x/other')"
        )
        await self.seed(self.clock + 100)
        await self.seed(self.clock + 200, novel=2)
        tasks = []

        def launch(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        with patch.object(router, "_run_background", launch):
            response = await router.sync_novel_now(1, None, {"id": 1})
            await asyncio.gather(*tasks)
        self.assertEqual(response.status_code, 302)
        self.assertEqual((await self.state())["next_run_at"], self.clock + 21600)
        self.assertEqual(
            (await self.state(2))["next_run_at"],
            self.clock + 200,
            "Manual synchronization must not postpone another novel",
        )

    async def test_an_old_automatic_selection_rechecks_the_manual_deadline(self):
        await self.seed(self.clock - 1)
        await self.sync(manual=True)
        saved = await self.state()
        meta = ScrapedNovelMeta(
            title="N", cover_url=None, synopsis=None, status=None, chapters=[]
        )
        with patch.object(sync_mod, "fetch_novel_index", return_value=meta) as fetch:
            await sync_mod.sync_novel(1, scheduled=True)
            fetch.assert_not_called()
        self.assertEqual(
            await self.state(),
            saved,
            "A superseded automatic trigger must not advance the schedule",
        )

    async def test_duplicate_trigger_does_not_postpone_a_running_attempt(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(url):
            if entered.is_set():
                return ScrapedNovelMeta(
                    title="N", cover_url=None, synopsis=None, status=None, chapters=[]
                )
            entered.set()
            await release.wait()
            raise asyncio.CancelledError

        with patch.object(sync_mod, "fetch_novel_index", hold):
            task = asyncio.create_task(sync_mod.sync_novel(1, manual=True))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                saved = await self.state()
                self.clock += 500
                await sync_mod.sync_novel(1, manual=True)
                self.assertEqual(
                    await self.state(),
                    saved,
                    "A skipped duplicate must not postpone the actual attempt",
                )
            finally:
                release.set()
                with suppress(asyncio.CancelledError):
                    await task

    async def test_cancellation_keeps_recovery_marker_and_downloaded_chapters(self):
        self.site = [ref(1, 1)]
        await self.sync()
        saved_content = (await self.rows("SELECT content_html FROM chapters"))[0][
            "content_html"
        ]

        async def interrupted(url):
            raise asyncio.CancelledError

        with patch.object(sync_mod, "fetch_novel_index", interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await sync_mod.sync_novel(1, manual=True)
        self.assertEqual(
            (await self.state())["running"],
            1,
            "Cancellation must leave a durable recovery marker",
        )
        self.positions_while_downloading = None
        await self.sync()
        self.assertEqual((await self.state())["running"], 0)
        self.assertEqual(
            (await self.rows("SELECT content_html FROM chapters"))[0]["content_html"],
            saved_content,
        )
        self.assertIsNone(
            self.positions_while_downloading,
            "Recovery must reuse already downloaded chapter bodies",
        )

    async def test_recorded_error_waits_for_the_normal_interval(self):
        async def unavailable(url):
            raise RuntimeError("scripted source failure")

        with (
            patch.object(sync_mod, "fetch_novel_index", unavailable),
            self.assertLogs("novels.sync", "ERROR"),
        ):
            with self.assertRaisesRegex(RuntimeError, "scripted source failure"):
                await sync_mod.sync_novel(1, manual=True)
        self.assertEqual(
            await self.state(),
            dict(novel_id=1, next_run_at=self.clock + 21600, running=0),
            "An ordinary error must not become an interrupted attempt",
        )
        with patch.object(sync_mod, "sync_novel") as scrape:
            await schedule.start()
            await self.pulse()
            scrape.assert_not_called()

    async def test_pause_resume_and_manual_paused_sync_preserve_eligibility(self):
        from app.novels.router import toggle_novel_active

        await self.execute("UPDATE novels SET active=0")
        await schedule.start()
        await self.pulse()
        self.assertIsNone(await self.state())
        await self.sync(manual=True)
        self.assertIsNotNone(
            await self.state(), "A paused novel must still accept a manual attempt"
        )
        await self.pulse()
        self.clock += 21601
        actual = self.sync

        async def scrape(identity, **kwargs):
            return await actual()

        with patch.object(sync_mod, "sync_novel", scrape):
            self.idle.clear()
            await toggle_novel_active(1, None, {"id": 1})
            try:
                await asyncio.wait_for(self.idle.wait(), 3)
            except asyncio.TimeoutError:
                self.fail("Re-enabling must wake the due scheduler")
        self.assertEqual(
            (await self.state())["next_run_at"],
            self.clock + 21600,
            "Re-enabling must wake the due scheduler",
        )

    async def test_deleted_novel_has_no_orphan_schedule(self):
        await self.seed(self.clock - 1)
        await self.execute("DELETE FROM novels WHERE id=1")
        self.assertIsNone(
            await self.state(), "Deleting a novel must remove its schedule"
        )
        with patch.object(sync_mod, "fetch_novel_index") as fetch:
            await sync_mod.sync_novel(1, scheduled=True)
            fetch.assert_not_called()

    async def test_jitter_is_selected_once_and_preserved_on_restart(self):
        with patch.object(schedule.random, "uniform", return_value=17) as jitter:
            await self.sync(manual=True)
            due = (await self.state())["next_run_at"]
            self.assertEqual(due, self.clock + 21617)
            jitter.assert_called_once_with(0, 2160)
            await schedule.start()
            await self.pulse()
            self.assertEqual((await self.state())["next_run_at"], due)
            jitter.assert_called_once()

    async def test_shutdown_cancels_and_restart_recovers_an_active_worker(self):
        entered = asyncio.Event()

        async def interrupted(url):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(sync_mod, "fetch_novel_index", interrupted):
            await sync_mod.start_scheduler()
            await asyncio.wait_for(entered.wait(), 3)
            await sync_mod.stop_scheduler()
        self.assertEqual(
            (await self.state())["running"],
            1,
            "Shutdown must retain an unfinished attempt",
        )
        self.assertNotIn(1, sync_mod._syncing_novel_ids)
        await sync_mod.start_scheduler()
        await self.pulse()
        self.assertEqual(
            (await self.state())["running"],
            0,
            "A fresh worker must recover the interrupted attempt",
        )

    async def test_queued_automatic_attempt_yields_to_manual_sync_of_same_work(self):
        await self.execute(
            "INSERT INTO novels(id,title,source_url) VALUES(2,'Other','https://x/other')"
        )
        await self.seed(self.clock - 1)
        await self.seed(self.clock - 1, novel=2)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def index(url):
            calls.append(url)
            if url == "https://x/n":
                entered.set()
                await release.wait()
            return ScrapedNovelMeta(
                title="N", cover_url=None, synopsis=None, status=None, chapters=[]
            )

        with patch.object(sync_mod, "fetch_novel_index", index):
            try:
                await schedule.start()
                await asyncio.wait_for(entered.wait(), 3)
                await self.actual_sync(2, manual=True)
                saved = await self.state(2)
                release.set()
                await self.pulse()
                self.assertEqual(
                    calls,
                    ["https://x/n", "https://x/other"],
                    "An old automatic queue must not repeat a manual sync",
                )
                self.assertEqual(await self.state(2), saved)
            finally:
                release.set()

    async def test_saved_deadline_fires_without_an_external_notification(self):
        import time

        finished = asyncio.Event()
        original = schedule.finish

        async def finish(identity):
            await original(identity)
            finished.set()

        with (
            patch.object(schedule, "_now", time.time),
            patch.object(schedule, "finish", finish),
        ):
            await self.seed(time.time() + 0.15)
            await schedule.start()
            try:
                await asyncio.wait_for(finished.wait(), 3)
            except asyncio.TimeoutError:
                self.fail("The persisted deadline must execute automatically")
            self.assertEqual(
                (await self.state())["running"],
                0,
                "The persisted deadline must execute automatically",
            )

    async def test_persistence_failure_backs_off_instead_of_spinning(self):
        with patch.object(
            schedule, "claim", side_effect=OSError("scripted persistence failure")
        ) as claim:
            with self.assertLogs("novels", "ERROR"):
                await schedule.start()
                try:
                    await self.pulse()
                except asyncio.TimeoutError:
                    self.fail("An unsettled schedule must back off")
            claim.assert_called_once()
            self.assertEqual(
                self.wait_delays[-1], 60, "An unsettled schedule must back off"
            )
