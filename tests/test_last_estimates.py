"""Old numeric predictions must never become old library metadata or a new work's ETA."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

from app.stats import ready_estimates as ready
from app.stats.last_estimates import LastEstimates
from app.stats.estimates import build_estimator
from app.library.router import _series_reading, _novel_reading
from tests import test_ready_estimates as fixtures


class LastKnownEstimates(fixtures.ReadyEstimates):
    async def test_publication_during_identity_lookup_keeps_a_captured_value(self):
        await self.state()
        estimator = await build_estimator(self.db, 1, cached_only=True)
        key = ("item", 1, None)
        from app.stats import last_estimates

        original = last_estimates.identities

        async def publish_during_lookup(db, keys):
            found = await original(db, keys)
            ready.cache.last.accounts[1].values.clear()
            return found

        with patch.object(last_estimates, "identities", publish_during_lookup):
            result = await ready.cache.last.get(self.db, 1, estimator, {key})
        self.assertIn(
            key,
            result,
            "A concurrent publication must not break a captured numeric response",
        )
        self.assertGreater(result[key].remaining, 0)

    async def test_unavailable_projection_signals_a_fresh_client_request(self):
        await self.state()
        import sqlite3
        from app.library import events

        with (
            patch.object(
                ready,
                "revisions",
                side_effect=sqlite3.OperationalError(
                    "controlled derived revision failure"
                ),
            ),
            patch.object(events, "publish") as published,
        ):
            self.assertIsNone(await ready.cache._refresh(1, authoritative=True))
        self.assertNotIn(1, ready.cache.states)
        published.assert_called_with({"type": "reading_estimate_updated", "user_id": 1})

    async def test_scope_identity_account_and_bounds(self):
        state = await self.state()
        values = LastEstimates(1, 2)
        values.remember(1, state)
        self.assertEqual(len(values.accounts[1].values), 2)
        key = next(iter(values.accounts[1].values))
        estimator = await build_estimator(self.db, 1, cached_only=True)
        self.assertIn(key, await values.get(self.db, 1, estimator, {key}))
        kind, identity, volume = key
        target_kind, target_id = (
            ("volume", volume) if volume is not None else (kind, identity)
        )
        await self.db.execute(
            "UPDATE estimate_target_incarnations SET incarnation=? WHERE kind=? AND identity=?",
            ("replacement-identity", target_kind, target_id),
        )
        await self.db.commit()
        self.assertEqual(await values.get(self.db, 1, estimator, {key}), {})
        other = replace(state, account_identity="different-account")
        values.remember(2, other)
        self.assertNotIn(1, values.accounts)
        values.clear()
        self.assertEqual(values.accounts, {})

    async def test_pending_numbers_keep_current_unread_counts_for_all_formats(self):
        state = await self.state()
        series_before, novel_before = (
            state.series[1].remaining,
            state.novels[1].remaining,
        )
        entered, release = asyncio.Event(), asyncio.Event()
        original = ready.cache._build

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        try:
            with patch.object(ready.cache, "_build", held):
                await self.db.execute(
                    "INSERT INTO reading_progress(user_id,item_id,status,progress_percent) VALUES(1,10,'completed',100)"
                )
                await self.db.execute(
                    "INSERT INTO novel_read_chapters(user_id,chapter_id) VALUES(1,1)"
                )
                await self.db.commit()
                ready.cache.notify()
                await asyncio.wait_for(entered.wait(), 2)
                estimator = await build_estimator(self.db, 1, cached_only=True)
                series = await _series_reading(self.db, 1, estimator)
                novels = await _novel_reading(self.db, 1, estimator)
                self.assertEqual(series[1].unread, state.series[1].unread - 1)
                self.assertEqual(series[1].remaining, series_before)
                self.assertEqual(novels[1].unread, state.novels[1].unread - 1)
                self.assertEqual(novels[1].remaining, novel_before)
        finally:
            release.set()
            await ready.cache.flush()
        self.assertNotEqual(ready.cache.states[1].series[1].remaining, series_before)
        self.assertLess(ready.cache.states[1].novels[1].remaining, novel_before)

    async def test_deleted_and_reinserted_target_has_no_previous_number(self):
        await self.state()
        estimator = await build_estimator(self.db, 1, cached_only=True)
        key = ("item", 1, None)
        self.assertIn(key, await ready.cache.last.get(self.db, 1, estimator, {key}))
        item = dict(await self.row("SELECT * FROM items WHERE id=1"))
        await self.db.execute("DELETE FROM items WHERE id=1")
        columns = ",".join(item)
        await self.db.execute(
            f"INSERT INTO items({columns}) VALUES({','.join('?' for _ in item)})",
            tuple(item.values()),
        )
        await self.db.commit()
        self.assertEqual(await ready.cache.last.get(self.db, 1, estimator, {key}), {})


def load_tests(loader, suite, pattern):
    import unittest

    return unittest.TestSuite(
        LastKnownEstimates(name)
        for name in LastKnownEstimates.__dict__
        if name.startswith("test_")
    )
