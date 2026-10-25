"""Large cold estimates cannot block page rendering or consume interactive DB slots."""
import asyncio
import threading
from unittest.mock import patch

import httpx
from app.main import app
from app.auth.dependencies import get_current_user, get_streaming_user
from app.database import close_pool
from tests.test_stats import StatsCase


class RequestAvailability(StatsCase):
    async def test_large_novel_and_queued_estimates_leave_login_and_home_available(self):
        self.addAsyncCleanup(close_pool)
        for identity in range(1, 66):
            await self.novel_chapter(identity)
        await self.db.execute("UPDATE chapters SET content_html='<p>reading words</p>', content_size=28")
        await self.db.execute("""INSERT INTO page_reads
            (user_id,kind,started_at,seconds,cap_seconds,visit_id,pace_words,pace_seconds,pace_samples)
            VALUES(1,'epub',datetime('now'),300,300,'valid-history',1000,300,3)""")
        await self.db.commit()
        user = {'id':1,'username':'u','avatar_path':None,'cover_blur_px':0,
                'page_time_cap_seconds':180,'text_time_cap_seconds':180}
        saved = dict(app.dependency_overrides)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_streaming_user] = lambda: user
        entered, release = threading.Event(), threading.Event()
        def slow_inventory(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError('Inventory release never arrived')
            return 2
        tasks = []
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                with patch('app.stats.estimates.inventory_html_total', side_effect=AssertionError('Home attempted a large cold inventory')):
                    response = await asyncio.wait_for(client.get('/'), 2)
                    self.assertEqual(response.status_code, 200)
                with patch('app.stats.estimates.inventory_html_total', side_effect=slow_inventory):
                    tasks = [asyncio.create_task(client.post('/library/reading-estimates', json={
                        'targets':[{'kind':'novel','id':1}]})) for _ in range(4)]
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    response = await asyncio.wait_for(client.get('/login'), 1)
                    self.assertEqual(response.status_code, 200, 'Slow estimates must leave login available')
                    release.set()
                    responses = await asyncio.wait_for(asyncio.gather(*tasks), 10)
                    self.assertTrue(all(r.status_code == 200 for r in responses))
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            app.dependency_overrides.clear()
            app.dependency_overrides.update(saved)
