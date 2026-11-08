"""Matching-space equivalence across rendered text, inventories and positions."""

import unittest
from app.stats.activity import readable_text
from app.stats.text_inventory import inventory_html
from app.reader.source_anchors import mapped_offset
from tests.test_progress_validation import ProgressCase, _JsonRequest
from app.novels.router import save_novel_progress
from fastapi import HTTPException

RULES = [{"find": "Mr. Dentelle", "replace": "Dentelle-san"}]


class ProgressMatchingVersion(ProgressCase):
    async def test_both_readers_persist_new_and_legacy_coordinate_versions(self):
        for novel in (False, True):
            for version in (2, 1):
                with self.subTest(novel=novel, version=version):
                    body = {"chapter_id": 1} if novel else {"current_page": 0}
                    if version == 2:
                        body["replacement_version"] = version
                    if novel:
                        await save_novel_progress(
                            1, _JsonRequest(body), self.user, self.db
                        )
                    else:
                        await self.post(body)
                    table = "novel_reading_progress" if novel else "reading_progress"
                    row = await (
                        await self.db.execute(
                            "SELECT replacement_version FROM " + table
                        )
                    ).fetchone()
                    self.assertEqual(
                        row[0],
                        version,
                        "Persist the matching version of the displayed coordinates",
                    )

    async def test_invalid_coordinate_versions_are_refused_before_writing(self):
        for novel in (False, True):
            for version in (True, 0, 3, "2"):
                with self.subTest(novel=novel, version=version):
                    body = {"chapter_id": 1, "replacement_version": version}
                    with self.assertRaises(HTTPException) as error:
                        if novel:
                            await save_novel_progress(
                                1, _JsonRequest(body), self.user, self.db
                            )
                        else:
                            await self.post(body)
                    self.assertEqual(error.exception.status_code, 400)
                    table = "novel_reading_progress" if novel else "reading_progress"
                    self.assertIsNone(
                        await (
                            await self.db.execute("SELECT 1 FROM " + table)
                        ).fetchone()
                    )


class ReplacementSpaces(unittest.TestCase):
    def test_every_visual_space_spelling_matches_the_same_rule(self):
        for space in [
            " ",
            "\u00a0",
            "&nbsp;",
            "&#160;",
            "&#xA0;",
            "&#0160;",
            "&NonBreakingSpace;",
        ]:
            with self.subTest(space=space):
                source = f"<p>Mr.{space}Dentelle Mr. Dentelle</p>"
                self.assertEqual(
                    readable_text(source, RULES), "Dentelle-san Dentelle-san"
                )
                self.assertEqual(
                    inventory_html(source, RULES).sections[0][0].positions,
                    (0, 9, 13, 22),
                )

    def test_decoded_mapping_uses_real_consumed_lengths(self):
        text = "😀 Mr.\u00a0Dentelle after"
        self.assertEqual(mapped_offset(text, 16, RULES), ("😀 Dentelle-san after", 16))

    def test_case_punctuation_tabs_and_escaped_entity_remain_distinct(self):
        for value in [
            "mr. Dentelle",
            "MrX Dentelle",
            "Mr.\tDentelle",
            "Mr.\nDentelle",
            "Mr.&amp;nbsp;Dentelle",
            "Mr.&#1600;Dentelle",
        ]:
            with self.subTest(value=value):
                self.assertNotIn(
                    "Dentelle-san", readable_text("<p>" + value + "</p>", RULES)
                )

    def test_literal_replacement_metacharacters_and_legacy_entity_rules(self):
        self.assertEqual(
            readable_text(
                "<p>Mr.&nbsp;Dentelle</p>",
                [{"find": "Mr. Dentelle", "replace": "$1 \\ $&"}],
            ),
            "$1 \\ $&",
        )
        self.assertEqual(
            readable_text("<p>&amp;</p>", [{"find": "amp", "replace": "lt"}]), "<"
        )


class LegacyPositions(unittest.TestCase):
    def test_unchanged_inserted_passage_retains_every_inside_offset(self):
        from app.reader.raw_positions import migrate_position

        source = "<p>😀 Mr.&nbsp;Dentelle name target</p>"
        rules = [
            {"find": "Mr. Dentelle", "replace": "D"},
            {"find": "name", "replace": "expanded name"},
        ]
        old_prefix = "😀 Mr.\u00a0Dentelle "
        new_prefix = "😀 D "
        u = lambda value: len(value.encode("utf-16-le")) // 2
        for inside in range(len("expanded name")):
            with self.subTest(inside=inside):
                self.assertEqual(
                    migrate_position(source, rules, 0, u(old_prefix) + inside),
                    (0, u(new_prefix) + inside),
                )

    def test_rewritten_passage_starts_at_its_new_beginning(self):
        from app.reader.raw_positions import migrate_position

        for spelling in ["\u00a0", "&nbsp;", "&#160;", "&#xA0;"]:
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    migrate_position(
                        "<p>prefix Mr." + spelling + "Dentelle after</p>", RULES, 0, 12
                    ),
                    (0, 7),
                )

    def test_unaffected_output_keeps_exact_coordinates(self):
        from app.reader.raw_positions import migrate_position

        source = "<p>Mr. Dentelle target</p>"
        for offset in range(len("Dentelle-san target") + 1):
            self.assertEqual(migrate_position(source, RULES, 0, offset), (0, offset))

    def test_shared_trace_migration_preserves_nested_unchanged_edits(self):
        from app.replacement_matching import migrate, trace

        source = "Mr.\u00a0Dentelle name tail"
        rules = [
            {"find": "Mr. Dentelle", "replace": "D"},
            {"find": "name", "replace": "long name"},
            {"find": "long", "replace": "very long"},
        ]
        old, _ = trace(source, rules, raw=True, version=1)
        new, _ = trace(source, rules, raw=True)
        for inside in range(len("very long name")):
            self.assertEqual(
                migrate(source, rules, old.index("very long name") + inside),
                new.index("very long name") + inside,
            )


class InventoryCompatibility(unittest.TestCase):
    def test_old_unaffected_disk_inventories_are_reused_and_affected_ones_are_not(self):
        import hashlib, tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app.config import settings
        from app.stats import text_inventory as store

        source = "<p>Mr.\u00a0Dentelle target</p>"
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(settings, "data_dir", Path(directory)),
        ):
            for rules in [
                [],
                [{"find": "target", "replace": "kept"}],
                [{"find": "Mr. Dentelle", "replace": "D"}],
            ]:
                normalized = store._rules(rules)
                old_sections = (store._section(source, normalized, matching_version=1),)
                old = store.Inventory(
                    old_sections, sum(len(b.positions) for b in old_sections[0])
                )
                legacy_key = (
                    "html",
                    hashlib.sha256(source.encode()).hexdigest(),
                    normalized,
                )
                path = store._disk_path(legacy_key)
                store._write_disk(path, old)
                before = path.read_bytes()
                store._cache.clear()
                store._cache_units = 0
                if rules and rules[0]["find"] == "Mr. Dentelle":
                    self.assertEqual(
                        store.inventory_html(source, rules).total_words,
                        2,
                        "Affected old inventory must not be reused",
                    )
                else:
                    with patch.object(
                        store,
                        "_section",
                        side_effect=AssertionError("Unchanged inventory was reparsed"),
                    ):
                        self.assertEqual(store.inventory_html(source, rules), old)
                self.assertEqual(
                    path.read_bytes(),
                    before,
                    "Unrelated persisted data must not be rewritten",
                )
