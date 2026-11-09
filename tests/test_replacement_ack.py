"""Saving a rule protects its passage before acknowledging, without waiting for ETA work."""

import asyncio
import json
from unittest.mock import patch

from bs4 import BeautifulSoup

from app import database
from app.config import settings
from app.reader.source_anchors import parse_anchor
from app.stats import ready_estimates as ready
from app.stats.estimates import build_estimator
from app.stats.ready_estimates import ReadyEstimateUpdates
from tests.test_detailed_timing import metrics
from tests import test_estimate_api_timing as api_fixtures


class ReplacementAcknowledgement(api_fixtures.EstimateApiTiming):
    async def post_rule(self, path="/settings/text-replacements", **fields):
        values = {
            "target": "series:1",
            "find_text": "four",
            "replace_text": "four extra extra",
        }
        values.update(fields)
        return await self.client.post(path, data=values, headers={"HX-Request": "true"})

    async def legacy_position(self):
        await self.db.execute(
            "INSERT INTO reading_progress(user_id,item_id,status,current_page,current_location,progress_percent,replacement_version) VALUES(1,1,'reading',0,'elidx:0:4',10,2)"
        )
        await self.db.commit()
        native = await build_estimator(self.db, 1)
        await native.prepare_items([await self.row("SELECT * FROM items WHERE id=1")])
        await ready.cache.get(
            self.db, 1, await build_estimator(self.db, 1, cached_only=True)
        )

    async def test_add_edit_delete_acknowledge_while_refresh_is_pending(self):
        await self.legacy_position()
        original = ready.cache._build
        for operation in ("add", "edit", "delete"):
            with self.subTest(operation=operation):
                entered, release = asyncio.Event(), asyncio.Event()

                async def held(*args, **kwargs):
                    if args[6] and not entered.is_set():
                        entered.set()
                        await release.wait()
                    return await original(*args, **kwargs)

                task = None
                try:
                    with patch.object(ready.cache, "_build", held):
                        rule = await self.row(
                            "SELECT id FROM text_replacements WHERE user_id=1"
                        )
                        path = (
                            "/settings/text-replacements"
                            if operation == "add"
                            else f"/settings/text-replacements/{rule['id']}"
                            + ("/delete" if operation == "delete" else "")
                        )
                        task = asyncio.create_task(
                            self.post_rule(path, replace_text="four extra extra extra")
                        )
                        await asyncio.wait_for(entered.wait(), 2)
                        try:
                            response = await asyncio.wait_for(asyncio.shield(task), 2)
                        except TimeoutError:
                            self.fail(
                                "Rule acknowledgement must not wait for the held estimate refresh"
                            )
                        self.assertEqual(response.status_code, 200)
                        progress = await self.row(
                            "SELECT source_anchor FROM reading_progress WHERE item_id=1"
                        )
                        anchor = parse_anchor(progress["source_anchor"])
                        self.assertIsNotNone(
                            anchor,
                            "The saved passage must be protected before acknowledgement",
                        )
                        self.assertEqual(anchor["position"], {"index": 0, "offset": 4})
                finally:
                    release.set()
                    if task:
                        await task
                    await ready.cache.flush()

    async def test_novel_save_protects_the_passage_before_background_refresh(self):
        await self.novel_chapter(1)
        await self.db.execute(
            "UPDATE chapters SET content_html='<p>one two three four</p>',content_size=25 WHERE id=1"
        )
        await self.db.execute(
            "INSERT INTO novel_reading_progress(user_id,novel_id,chapter_id,paragraph_index,character_offset) VALUES(1,1,1,0,4)"
        )
        await self.db.commit()
        native = await build_estimator(self.db, 1)
        await native.prepare_novel(1)
        await ready.cache.get(
            self.db, 1, await build_estimator(self.db, 1, cached_only=True)
        )
        entered, release = asyncio.Event(), asyncio.Event()
        original = ready.cache._build

        async def held(*args, **kwargs):
            if args[6] and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        pending = None
        try:
            with patch.object(ready.cache, "_build", held):
                pending = asyncio.create_task(self.post_rule(target="novel:1"))
                await asyncio.wait_for(entered.wait(), 2)
                try:
                    saved = await asyncio.wait_for(asyncio.shield(pending), 2)
                except TimeoutError:
                    self.fail(
                        "A novel rule must acknowledge before the held refresh completes"
                    )
                self.assertEqual(saved.status_code, 200)
                row = await self.row(
                    "SELECT source_anchor FROM novel_reading_progress WHERE novel_id=1"
                )
                anchor = parse_anchor(row["source_anchor"])
                self.assertEqual(anchor["chapterId"], 1)
                self.assertEqual(anchor["position"], {"index": 0, "offset": 4})
        finally:
            release.set()
            if pending:
                await pending
            await ready.cache.flush()

    async def test_known_number_survives_refresh_with_current_rules_and_passage(self):
        await self.legacy_position()
        before = ready.cache.states[1].item_estimates[1].remaining
        entered, release = asyncio.Event(), asyncio.Event()
        original = ready.cache._build
        from app.library import events

        published = []
        publish = events.publish

        def observed(event):
            if (
                event["type"] == "reading_estimate_updated"
                and event.get("user_id") == 1
            ):
                published.append(event)
            publish(event)

        async def held(*args, **kwargs):
            if args[6] and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        try:
            with (
                patch.object(ready.cache, "_build", held),
                patch.object(events, "publish", observed),
            ):
                self.assertEqual(
                    (await asyncio.wait_for(self.post_rule(), 2)).status_code, 200
                )
                await asyncio.wait_for(entered.wait(), 2)
                home = await asyncio.wait_for(self.client.get("/"), 2)
                node = BeautifulSoup(home.text, "html.parser").find(
                    attrs={"data-text-eta-kind": "series", "data-text-eta-id": "1"}
                )
                self.assertTrue(node.get_text(strip=True).startswith("~"), node)
                response = await asyncio.wait_for(
                    self.client.post(
                        "/library/reading-estimates",
                        json={"targets": [{"kind": "item", "id": 1}]},
                    ),
                    2,
                )
                self.assertAlmostEqual(
                    response.json()["estimates"][0]["remaining"], before
                )
                rule = await self.row(
                    "SELECT id FROM text_replacements WHERE user_id=1"
                )
                await self.post_rule(
                    f"/settings/text-replacements/{rule['id']}",
                    replace_text="four extra extra extra extra",
                )
                reader = await self.client.get("/reader/1")
                root = BeautifulSoup(reader.text, "html.parser").find(id="reader-root")
                self.assertEqual(
                    json.loads(root["data-replacements"])[0]["replace"],
                    "four extra extra extra extra",
                )
                self.assertIsNotNone(parse_anchor(root["data-source-anchor"]))
                self.assertEqual(
                    published, [], "Pending work is not a published new result"
                )
                release.set()
                await ready.cache.flush()
                self.assertTrue(
                    published, "Completion must trigger the numeric refresh"
                )
                current = ready.cache.states[1].item_estimates[1].remaining
                self.assertGreater(current, before)
                native = await build_estimator(self.db, 1)
                item = await self.row("SELECT * FROM items WHERE id=1")
                progress = await self.row(
                    "SELECT * FROM reading_progress WHERE item_id=1"
                )
                await native.prepare_items([item])
                self.assertAlmostEqual(
                    current, native.for_item(item, progress).remaining
                )
                self.assertEqual(
                    ready.cache.states[1].revision, await ready.revisions(self.db, 1)
                )
                count = len(published)
                await ready.cache.get(
                    self.db, 1, await build_estimator(self.db, 1, cached_only=True)
                )
                self.assertEqual(
                    len(published),
                    count,
                    "A stable cache hit must not loop SSE updates",
                )
        finally:
            release.set()
            await ready.cache.flush()

    async def test_unknown_number_remains_calculating_during_refresh(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = ready.cache._build

        async def held(*args, **kwargs):
            if args[6] and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        try:
            with patch.object(ready.cache, "_build", held):
                await self.post_rule()
                await asyncio.wait_for(entered.wait(), 2)
                ready.cache.last.clear()
                home = await self.client.get("/")
                node = BeautifulSoup(home.text, "html.parser").find(
                    attrs={"data-text-eta-kind": "series", "data-text-eta-id": "1"}
                )
                self.assertEqual(node["data-estimate-state"], "calculating")
                self.assertIn("Calculating", node.get_text())
        finally:
            release.set()
            await ready.cache.flush()

    async def test_failed_maintenance_preserves_the_confirmed_rule_and_passage(self):
        await self.legacy_position()
        entered, release = asyncio.Event(), asyncio.Event()
        original_flush = ready.cache.flush

        async def failing(*args, **kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("controlled estimate refresh failure")

        try:
            with patch.object(ready.cache, "_build", failing):
                try:
                    saved = await asyncio.wait_for(self.post_rule(), 2)
                except TimeoutError:
                    self.fail("A saved rule must not wait for estimate maintenance")
                self.assertEqual(saved.status_code, 200)
                await asyncio.wait_for(entered.wait(), 2)
                with self.assertLogs("app.stats.ready_estimates", level="ERROR"):
                    release.set()
                    await original_flush()
            rule = await self.row(
                "SELECT find_text,replace_text FROM text_replacements WHERE user_id=1"
            )
            self.assertEqual(
                dict(rule), {"find_text": "four", "replace_text": "four extra extra"}
            )
            progress = await self.row(
                "SELECT source_anchor FROM reading_progress WHERE item_id=1"
            )
            anchor = parse_anchor(progress["source_anchor"])
            self.assertIsNotNone(
                anchor, "The saved passage must survive failed estimate maintenance"
            )
            self.assertEqual(anchor["position"], {"index": 0, "offset": 4})
            response = await self.client.post(
                "/library/reading-estimates",
                json={"targets": [{"kind": "item", "id": 1}]},
            )
            self.assertEqual(response.status_code, 200)
            self.assertAlmostEqual(response.json()["estimates"][0]["remaining"], 2.1)
        finally:
            release.set()
            await original_flush()

    async def test_only_exact_save_routes_and_validated_api_skip_the_generic_gate(self):
        paths = [
            ("/settings/text-replacements", "POST", False, False),
            ("/settings/text-replacements/1", "POST", False, False),
            ("/settings/text-replacements/1/delete", "POST", False, False),
            ("/settings/text-replacements-extra", "POST", False, True),
            ("/settings/text-replacements/1/extra", "POST", False, True),
            ("/reader/1/progress", "POST", True, True),
            ("/library/reading-estimates", "POST", True, False),
            ("/library/reading-estimates", "POST", False, True),
            ("/settings/text-replacements", "PATCH", False, True),
        ]
        from unittest.mock import AsyncMock

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        for path, method, checked, should_flush in paths:
            with self.subTest(path=path, method=method, checked=checked):
                flush = AsyncMock()

                async def send(message):
                    pass

                with patch.object(ready.cache, "flush", flush):
                    await ReadyEstimateUpdates(app)(
                        {
                            "type": "http",
                            "path": path,
                            "method": method,
                            "ready_estimates_preview": checked,
                        },
                        None,
                        send,
                    )
                self.assertEqual(
                    flush.await_count,
                    int(should_flush),
                    "Only the intended request may skip maintenance acknowledgement",
                )

    async def test_default_off_save_does_not_read_detailed_clocks(self):
        with patch(
            "app.request_timing.perf_counter",
            side_effect=AssertionError("disabled detailed clock"),
        ):
            response = await self.post_rule()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("replacement_", response.headers.get("server-timing", ""))
        await ready.cache.flush()

    async def test_opt_in_reports_disjoint_save_phases(self):
        settings.detailed_server_timing = True
        response = await self.post_rule()
        self.assertEqual(response.status_code, 200)
        values = metrics(response)
        phases = (
            "db_acquire",
            "auth",
            "replacement_legacy",
            "replacement_writer_wait",
            "replacement_validate",
            "replacement_write",
            "render",
        )
        self.assertEqual(set(values), {"app", *phases, "replacement_other"})
        self.assertTrue(all(value >= 0 for value in values.values()))
        self.assertAlmostEqual(
            values["app"],
            sum(values[name] for name in phases) + values["replacement_other"],
            delta=0.02,
        )
        for name in (
            "replacement_legacy",
            "replacement_writer_wait",
            "replacement_validate",
            "replacement_write",
            "render",
        ):
            self.assertGreater(
                values[name],
                0,
                "The enabled save phase must contain actual work: " + name,
            )
        self.assertNotIn("api_flush", values)
        await ready.cache.flush()


def load_tests(loader, tests, pattern):
    import unittest

    return unittest.TestSuite(
        ReplacementAcknowledgement(name)
        for name in ReplacementAcknowledgement.__dict__
        if name.startswith(loader.testMethodPrefix)
    )
