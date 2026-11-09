"""Delayed first deliveries keep their measured text, not subsequently edited rules."""

import asyncio
import copy
import json
import os
import sys
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from app import database, text_replacements
from app.stats import activity_rules, ready_estimates
from app.stats.activity import claim_session, record_activity
from tests import test_estimate_api_timing as fixtures


class ActivityRules(fixtures.EstimateApiTiming):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.activity_number = 0
        await self.novel_chapter(1)
        await self.db.execute(
            "UPDATE chapters SET content_html='<p>one two three four</p>',content_size=25 WHERE id=1"
        )
        await self.db.commit()

    async def pending(self, kind="epub", text="one two three four", *, closed=True):
        self.activity_number += 1
        identity = f"activity-rules-{self.activity_number:016d}"
        session = f"session-rules-{self.activity_number:016d}"
        started = 1000 + self.activity_number * 100
        await claim_session(
            self.db, 1, session, f"claim-rules-{self.activity_number:016d}", now=started
        )
        await self.db.commit()
        path = "/reader/1" if kind == "epub" else "/novels/1/read/1"
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text[:100])
        root = BeautifulSoup(response.text, "html.parser").find(id="reader-root")
        reference = root.get("data-activity-rule-set")
        self.assertIsNotNone(
            reference, "The actual reader must carry its measured rule basis"
        )
        self.assertEqual(len(reference), 64)
        value = dict(
            id=identity,
            session_id=session,
            revision=1,
            page_index=0,
            character_offset=0,
            replacement_version=2,
            rule_set_ref=reference,
            passage=[dict(text=text, start=0, end=len(text))],
            intervals=[[started, started + 10]],
            closed=closed,
            completed=closed,
        )
        if kind == "epub":
            value.update(item_id=1, section_index=0, source_version="h1")
        else:
            value["chapter_id"] = 1
        return value

    async def add(self, kind="epub", replacement="changed into several words"):
        return await self.client.post(
            "/settings/text-replacements",
            data={
                "target": "series:1" if kind == "epub" else "novel:1",
                "find_text": "one",
                "replace_text": replacement,
            },
            headers={"HX-Request": "true"},
        )

    async def deliver(self, value):
        response = await self.client.post("/reading-time", json={"activities": [value]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rejected"], [], response.json())
        self.assertEqual(
            response.json()["accepted"],
            [{"id": value["id"], "revision": value["revision"]}],
        )

    async def test_actual_saves_preserve_old_epub_and_novel_passages(self):
        for kind in ("epub", "novel"):
            for operation in ("add", "edit", "delete"):
                with self.subTest(kind=kind, operation=operation):
                    await self.db.execute("DELETE FROM text_replacements")
                    if operation != "add":
                        await self.db.execute(
                            """INSERT INTO text_replacements(user_id,series_id,novel_id,find_text,replace_text)
                            VALUES(1,?,?, 'one','old words')""",
                            (
                                1 if kind == "epub" else None,
                                1 if kind == "novel" else None,
                            ),
                        )
                    await self.db.commit()
                    text = (
                        "one two three four"
                        if operation == "add"
                        else "old words two three four"
                    )
                    value = await self.pending(kind, text)
                    if operation == "add":
                        response = await self.add(kind)
                    else:
                        rule = await self.row("SELECT id FROM text_replacements")
                        response = await self.client.post(
                            f"/settings/text-replacements/{rule['id']}"
                            + ("/delete" if operation == "delete" else ""),
                            data={"find_text": "one", "replace_text": "latest words"},
                            headers={"HX-Request": "true"},
                        )
                    self.assertEqual(response.status_code, 200)
                    await self.deliver(value)
                    recorded = await self.row(
                        "SELECT word_count FROM reading_activity WHERE id=?",
                        (value["id"],),
                    )
                    self.assertEqual(
                        recorded["word_count"], 4 if operation == "add" else 5
                    )
                    current = await self.pending(kind)
                    self.assertNotEqual(current["rule_set_ref"], value["rule_set_ref"])

    async def test_reference_survives_new_process_and_replay_is_idempotent(self):
        value = await self.pending()
        await self.add()
        await ready_estimates.cache.stop()
        await database.close_pool()
        await self.db.close()
        program = """import asyncio,json,sys
from app.database import init_db,get_connection
from app.stats.activity import record_activity
async def main():
    value=json.load(sys.stdin)
    await init_db()
    db=await get_connection()
    try:
        await record_activity(db,1,180,180,value,now=2000)
        await db.commit()
    finally: await db.close()
asyncio.run(main())"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "APP_DATA_DIR": self._tmp.name},
        )
        _, error = await process.communicate(json.dumps(value).encode())
        self.db = await database.get_connection()
        self.assertEqual(process.returncode, 0, error.decode())
        sql = "SELECT COUNT(*),SUM(seconds),SUM(words),SUM(pace_words) FROM page_reads WHERE visit_id=?"
        before = tuple(await self.row(sql, (value["id"],)))
        self.assertEqual(
            before[3],
            4,
            "Credit the four measured words, not the later seven-word display",
        )
        for revision in (1, 2, 2):
            value["revision"] = revision
            await self.deliver(value)
        self.assertEqual(tuple(await self.row(sql, (value["id"],))), before)

    async def test_open_checkpoint_keeps_its_reference_after_edit(self):
        value = await self.pending(closed=False)
        await self.deliver(value)
        await self.add()
        value["revision"] = 2
        value["intervals"][0][1] += 5
        await self.deliver(value)
        current = await self.pending()
        changed = copy.deepcopy(value)
        changed.update(revision=3, rule_set_ref=current["rule_set_ref"])
        with self.assertRaisesRegex(ValueError, "cannot change its reading passage"):
            await record_activity(self.db, 1, 180, 180, changed, now=2000)
        saved = await self.row(
            "SELECT revision FROM reading_activity WHERE id=?", (value["id"],)
        )
        self.assertEqual(saved["revision"], 2)

    async def test_unknown_cross_scope_and_forged_bases_fail_closed(self):
        value = await self.pending()
        await self.add()
        await self.db.execute(
            "INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(2,1,'S2','s2','/s2')"
        )
        await self.book(2)
        await self.db.execute("UPDATE items SET series_id=2 WHERE id=2")
        await self.db.execute(
            "INSERT INTO users(id,username,password_hash) VALUES(2,'other','x')"
        )
        await claim_session(
            self.db, 2, "other-session-00000000", "other-claim-0000000000", now=1100
        )
        await self.db.commit()
        cases = [
            ({"rule_set_ref": "0" * 64}, "Unknown reading rule reference"),
            ({"item_id": 2, "source_version": "h2"}, "Unknown reading rule reference"),
            ({"replacement_version": 1}, "Unknown reading rule reference"),
            ({"source_version": "changed-source"}, "content has changed"),
            (
                {"passage": [{"text": "invented words here", "start": 0, "end": 19}]},
                "Passage does not match",
            ),
        ]
        for changes, reason in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, reason),
            ):
                await record_activity(
                    self.db, 1, 180, 180, {**value, **changes}, now=2000
                )
        for malformed in (None, "", "F" * 64, "a" * 65, ["a" * 64]):
            with (
                self.subTest(malformed=malformed),
                self.assertRaisesRegex(ValueError, "Invalid reading rule reference"),
            ):
                await record_activity(
                    self.db, 1, 180, 180, {**value, "rule_set_ref": malformed}, now=2000
                )
        with self.assertRaisesRegex(ValueError, "Unknown reading rule reference"):
            await record_activity(
                self.db,
                2,
                180,
                180,
                {**value, "session_id": "other-session-00000000"},
                now=2000,
            )
        await self.db.execute(
            "UPDATE activity_rule_sets SET rules_json=? WHERE reference=?",
            ('[{"find":"absent token","replace":"different"}]', value["rule_set_ref"]),
        )
        with self.assertRaisesRegex(ValueError, "Invalid stored reading rule set"):
            await record_activity(self.db, 1, 180, 180, value, now=2000)
        self.assertEqual(
            (await self.row("SELECT COUNT(*) FROM reading_activity"))[0], 0
        )

    async def test_failed_rule_write_rolls_back_the_preserved_basis(self):
        await self.db.execute(
            "INSERT INTO text_replacements(user_id,series_id,find_text,replace_text) VALUES(1,1,'one','old words')"
        )
        await self.db.commit()
        rule = await self.row("SELECT id FROM text_replacements")
        original = activity_rules.preserve_rule

        async def interrupted(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("controlled interrupted replacement")

        with (
            patch.object(activity_rules, "preserve_rule", interrupted),
            self.assertRaisesRegex(RuntimeError, "controlled interrupted"),
        ):
            await self.client.post(
                f"/settings/text-replacements/{rule['id']}",
                data={"find_text": "one", "replace_text": "new"},
            )
        self.assertEqual(
            (await self.row("SELECT COUNT(*) FROM activity_rule_sets"))[0], 0
        )
        self.assertEqual(
            (await self.row("SELECT replace_text FROM text_replacements"))[0],
            "old words",
        )

    async def test_duplicate_cleanup_preserves_ordered_basis_once(self):
        for _ in range(2):
            await self.db.execute(
                "INSERT INTO text_replacements(user_id,series_id,find_text,replace_text) VALUES(1,1,'one','one one')"
            )
        await self.db.commit()
        value = await self.pending(text="one one one one two three four")
        await text_replacements.remove_duplicates()
        self.assertEqual(
            (await self.row("SELECT COUNT(*) FROM text_replacements"))[0], 1
        )
        await self.deliver(value)
        self.assertEqual(
            (
                await self.row(
                    "SELECT word_count FROM reading_activity WHERE id=?", (value["id"],)
                )
            )[0],
            7,
        )
        count = (await self.row("SELECT COUNT(*) FROM activity_rule_sets"))[0]
        await text_replacements.remove_duplicates()
        self.assertEqual(
            (await self.row("SELECT COUNT(*) FROM activity_rule_sets"))[0], count
        )

    async def test_reused_scope_cannot_inherit_a_reference(self):
        value = await self.pending("novel")
        await self.add("novel")
        await ready_estimates.cache.flush()
        await self.db.execute("DELETE FROM novels WHERE id=1")
        self.assertEqual(
            (
                await self.row(
                    "SELECT COUNT(*) FROM activity_rule_sets WHERE novel_id=1"
                )
            )[0],
            0,
        )
        await self.novel_chapter(1)
        await self.db.execute(
            "UPDATE chapters SET content_html='<p>one two three four</p>',content_size=25 WHERE id=1"
        )
        await self.db.commit()
        with self.assertRaisesRegex(ValueError, "Unknown reading rule reference"):
            await record_activity(self.db, 1, 180, 180, value, now=2000)

    async def test_reused_account_cannot_inherit_a_reference(self):
        value = await self.pending()
        await self.add()
        await ready_estimates.cache.flush()
        await self.db.execute("DELETE FROM users WHERE id=1")
        self.assertEqual(
            (await self.row("SELECT COUNT(*) FROM activity_rule_sets"))[0], 0
        )
        await self.db.execute(
            "INSERT INTO users(id,username,password_hash) VALUES(1,'u','x')"
        )
        session = "reused-session-0000000"
        await claim_session(self.db, 1, session, "reused-claim-000000000", now=1100)
        await self.db.commit()
        with self.assertRaisesRegex(ValueError, "Unknown reading rule reference"):
            await record_activity(
                self.db, 1, 180, 180, {**value, "session_id": session}, now=2000
            )


def load_tests(loader, suite, pattern):
    return unittest.TestSuite(
        ActivityRules(name)
        for name in ActivityRules.__dict__
        if name.startswith("test_")
    )
