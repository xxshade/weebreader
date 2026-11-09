"""Structural positions, parser parity, and freshness across restored readers."""

import json
import unittest

from app.reader.source_anchors import _convert_position, capture_anchor, remaining_html
from app.stats.activity import (
    readable_text,
    passage_positions,
    claim_session,
    record_activity,
)
from tests.test_stats import StatsCase
from tests.test_progress_validation import ProgressCase

CASES = [
    (
        "inline markup",
        "<p>name target</p>",
        [{"find": "name", "replace": "<em>new name</em>"}],
        (0, 5),
        (0, 9),
    ),
    (
        "removed block",
        "<div>name</div><p>target here</p>",
        [{"find": "name", "replace": ""}],
        (1, 0),
        (0, 0),
    ),
    (
        "removed anchor",
        "<div>name</div><p>target here</p>",
        [{"find": "name", "replace": ""}],
        (0, 2),
        (0, 0),
    ),
    (
        "entity spelling",
        "<p>&amp; target</p>",
        [{"find": "amp", "replace": "lt"}],
        (0, 2),
        (0, 2),
    ),
    (
        "split block",
        "<p>name target here</p>",
        [{"find": "name", "replace": "first</p><p>second"}],
        (0, 5),
        (1, 7),
    ),
    (
        "inside rewritten block",
        "<p>name target here</p>",
        [{"find": "name", "replace": "first</p><p>second"}],
        (0, 2),
        (0, 0),
    ),
    (
        "image after removal",
        "<div>name</div><p><img src=x></p><p>target</p>",
        [{"find": "name", "replace": ""}],
        (1, 0),
        (0, 0),
    ),
    (
        "foster parenting",
        "<table><tr><td>inside marker</td></tr>outside marker</table>",
        [{"find": "inside", "replace": "<em>inner</em>"}],
        (0, 3),
        (0, 3),
    ),
    (
        "unicode entity",
        "<p>😀 name target</p>",
        [{"find": "name", "replace": "<b>é &amp; 名</b>"}],
        (0, 8),
        (0, 9),
    ),
]


class Structural(unittest.TestCase):
    def test_browser_structural_matrix_has_equivalent_backend_coordinates(self):
        for name, source, rules, start, expected in CASES:
            with self.subTest(case=name):
                self.assertEqual(_convert_position(source, rules, *start), expected)

    def test_changed_rules_count_remaining_words_from_the_original_block(self):
        source = "<div>remove me</div><p>target here tail</p>"
        value = capture_anchor(source, [], 0, 1, 7)
        self.assertEqual(
            remaining_html(source, [{"find": "remove me", "replace": ""}], value), 2
        )

    def test_html5_parity_keeps_invalid_passages_invalid(self):
        source = "<table><tr><td>inside marker</td></tr>outside marker</table>"
        repaired = readable_text(source, [], html5=True)
        parts = [
            {"text": "outside marker", "start": 0, "end": 14},
            {"text": "inside marker", "start": 0, "end": 13},
        ]
        self.assertEqual(sum(map(len, passage_positions(parts, repaired))), 4)
        with self.assertRaisesRegex(ValueError, "does not match"):
            passage_positions([dict(text="forged passage", start=0, end=14)], repaired)
        with self.assertRaisesRegex(ValueError, "does not match"):
            passage_positions(list(reversed(parts)), repaired)


class ActivityParity(StatsCase):
    async def test_table_activity_is_accepted_and_replay_after_rule_edit_is_idempotent(
        self,
    ):
        await self.novel_chapter(1, novel_id=1)
        source = "<table><tr><td>inside marker</td></tr>outside marker</table>"
        await self.db.execute(
            "UPDATE chapters SET content_html=? WHERE id=1", (source,)
        )
        session = "final-session-0000000000"
        await claim_session(self.db, 1, session, "final-claim-00000000000", now=1000)
        activity = dict(
            id="final-visit-00000000000",
            session_id=session,
            revision=1,
            chapter_id=1,
            page_index=0,
            character_offset=0,
            passage=[
                dict(text="outside marker", start=0, end=14),
                dict(text="inside marker", start=0, end=13),
            ],
            intervals=[[1000, 1020]],
            closed=True,
            completed=False,
        )
        invalid = {
            **activity,
            "id": "reverse-visit-000000000",
            "passage": list(reversed(activity["passage"])),
        }
        with self.assertRaisesRegex(ValueError, "does not match"):
            await record_activity(self.db, 1, 180, 180, invalid, now=1030)
        await record_activity(self.db, 1, 180, 180, activity, now=1030)
        await self.db.execute(
            "INSERT INTO text_replacements(user_id,novel_id,find_text,replace_text) VALUES(1,1,'inside','renamed')"
        )
        await record_activity(self.db, 1, 180, 180, activity, now=1040)
        row = await (
            await self.db.execute(
                "SELECT COUNT(*) AS n,SUM(word_count) AS words FROM reading_activity"
            )
        ).fetchone()
        self.assertEqual(tuple(row), (1, 4))


class Freshness(ProgressCase):
    async def test_current_rules_are_reported_even_without_a_new_progress_save(self):
        from app.reader.router import read_progress

        first = json.loads((await read_progress(1, self.user, self.db)).body)
        await self.db.execute(
            "INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(1,1,'S','s','/s')"
        )
        await self.db.execute("UPDATE items SET series_id=1 WHERE id=1")
        await self.db.execute(
            "INSERT INTO text_replacements(user_id,series_id,find_text,replace_text) VALUES(1,1,'old','new')"
        )
        current = json.loads((await read_progress(1, self.user, self.db)).body)
        self.assertEqual(first["position"], current["position"])
        self.assertEqual(
            [r for r in current["current_replacements"] if r.get("find")], [dict(find="old", replace="new")]
        )
        self.assertEqual(current["source_version"], "h")


class RepairCache(unittest.TestCase):
    def test_repaired_cache_is_bounded_and_large_inputs_are_not_rejected(self):
        from app.reader import html_source as parser

        for n in range(24):
            source = (
                "<table><tr><td>" + str(n) + " " + "text " * 5000 + "</td></tr></table>"
            )
            self.assertTrue(readable_text(source, []))
        self.assertLessEqual(len(parser._TEXT_CACHE), parser._TEXT_CACHE_ENTRIES)
        self.assertLessEqual(parser._TEXT_CACHE_UNITS, parser._TEXT_CACHE_LIMIT)
        source = "<table><tr><td>" + "word " * 110000 + "</td></tr></table>"
        self.assertGreater(len(readable_text(source, [])), parser._TEXT_CACHE_LIMIT)
        self.assertLessEqual(parser._TEXT_CACHE_UNITS, parser._TEXT_CACHE_LIMIT)


class FirstOpeningFreshness(ProgressCase):
    async def test_novel_with_no_saved_position_can_refresh_its_current_chapter(self):
        from app.novels.router import read_novel_progress

        await self.db.execute(
            "INSERT INTO text_replacements(user_id,novel_id,find_text,replace_text) VALUES(1,1,'old','new')"
        )
        current = json.loads(
            (await read_novel_progress(1, self.user, self.db, chapter_id=1)).body
        )
        self.assertIsNone(current["position"])
        self.assertEqual(current["resume_url"], "/novels/1/read/1")
        self.assertEqual(
            [r for r in current["current_replacements"] if r.get("find")], [dict(find="old", replace="new")]
        )
        row = await (
            await self.db.execute("SELECT 1 FROM novel_reading_progress")
        ).fetchone()
        self.assertIsNone(row)

    async def test_current_chapter_context_cannot_cross_work_boundaries(self):
        from app.novels.router import read_novel_progress

        current = json.loads(
            (await read_novel_progress(1, self.user, self.db, chapter_id=999)).body
        )
        self.assertIsNone(current["resume_url"])
