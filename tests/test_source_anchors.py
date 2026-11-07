"""Original-source position integration across rule edits and resumed saves."""

import hashlib
import json
import unittest
import zipfile

from app.reader.source_anchors import display_position, remaining_html, validate_anchor
from app.reading_progress import (
    mark_item_read,
    mark_series_read,
    mark_series_read_up_to,
)
from fastapi import HTTPException
from tests.test_estimates import EstimateCase
from tests import test_word_estimates as fixtures


def anchor(source, offset, **extra):
    return json.dumps(
        dict(
            version=1,
            section=0,
            position=dict(index=0, offset=offset),
            originalDigest=hashlib.sha256(source.encode()).hexdigest(),
            rulesDigest="0" * 64,
            **extra,
        )
    )


class Mapping(unittest.TestCase):
    def test_utf16_chained_and_entity_spelling(self):
        source = "<p>😀 name target tail</p>"
        rules = [
            dict(find="name", replace="NAME"),
            dict(find="NAME", replace="a longer name"),
        ]
        self.assertEqual(
            display_position(source, rules, json.loads(anchor(source, 8))), (0, 17)
        )
        self.assertEqual(remaining_html(source, rules, anchor(source, 8)), 2)
        entity = "<p>&amp; target</p>"
        self.assertEqual(
            display_position(
                entity, [dict(find="amp", replace="lt")], json.loads(anchor(entity, 2))
            ),
            (0, 2),
        )

    def test_boundary_refuses_wrong_identity_and_boolean_coordinates(self):
        value = anchor("<p>word</p>", 0)
        self.assertEqual(validate_anchor(value, section=0), value)
        for damaged in [
            value.replace('"offset": 0', '"offset": true'),
            value.replace('"version": 1', '"version": true'),
        ]:
            with self.assertRaises(HTTPException):
                validate_anchor(damaged)
        with self.assertRaises(HTTPException):
            validate_anchor(value, section=1)
        self.assertIsNone(display_position("<p>new source</p>", [], json.loads(value)))


class Integration(EstimateCase):
    book = fixtures.WordEstimates.book
    evidence = fixtures.WordEstimates.evidence

    async def test_epub_estimate_uses_original_anchor_immediately_after_rule_edit(self):
        item = await self.book(1, text="name one two three four", other="last word")
        with zipfile.ZipFile(item["file_path"]) as z:
            source = z.read("a.xhtml").decode()
        await self.db.execute(
            "INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(1,1,'S','s','/s')"
        )
        await self.db.execute("UPDATE items SET series_id=1 WHERE id=1")
        await self.db.execute(
            "INSERT INTO reading_progress(user_id,item_id,current_page,current_location,progress_percent,source_anchor) VALUES(1,1,0,'elidx:0:9',40,?)",
            (anchor(source, 9),),
        )
        await self.evidence()
        for replacement in ["longer character name", "N", ""]:
            await self.db.execute("DELETE FROM text_replacements")
            await self.db.execute(
                "INSERT INTO text_replacements(user_id,series_id,find_text,replace_text) VALUES(1,1,?,?)",
                ("name", replacement),
            )
            await self.db.commit()
            model = await self.estimator()
            await model.prepare_items([item])
            self.assertAlmostEqual(model.item_estimates[1].remaining, 5 * 0.3)
            # Legacy coordinate is deliberately unchanged before any resumed save.
            row = await self.row("SELECT current_location FROM reading_progress")
            self.assertEqual(row[0], "elidx:0:9")

    async def test_novel_estimate_maps_canonical_current_chapter(self):
        await self.novel_chapter(1)
        source = "<p>name one two three four</p>"
        await self.db.execute(
            "UPDATE chapters SET content_html=?,content_size=? WHERE id=1",
            (source, len(source)),
        )
        await self.db.execute(
            "INSERT INTO novel_reading_progress(user_id,novel_id,chapter_id,paragraph_index,character_offset,chapter_percent,source_anchor) VALUES(1,1,1,0,9,40,?)",
            (anchor(source, 9, chapterId=1),),
        )
        await self.db.execute(
            "INSERT INTO text_replacements(user_id,novel_id,find_text,replace_text) VALUES(1,1,'name','longer character name')"
        )
        await self.evidence(novel_id=1)
        model = await self.estimator()
        await model.prepare_novel(1)
        self.assertAlmostEqual(model.for_novel(1).remaining, 3 * 0.3)

    async def test_bulk_completion_clears_original_reference_with_legacy_position(self):
        await self.book(1)
        await self.db.execute(
            "INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(1,1,'S','s','/s')"
        )
        await self.db.execute(
            "UPDATE items SET series_id=1,number_in_series=1 WHERE id=1"
        )
        for operation in [mark_item_read, mark_series_read, mark_series_read_up_to]:
            await self.db.execute(
                "INSERT OR REPLACE INTO reading_progress(user_id,item_id,status,current_page,current_location,source_anchor) VALUES(1,1,'reading',0,'elidx:0:9',?)",
                (anchor("<p>word</p>", 0),),
            )
            if operation is mark_item_read:
                await operation(self.db, 1, 1, None)
            elif operation is mark_series_read:
                await operation(self.db, 1, 1)
            else:
                await operation(self.db, 1, 1, 1)
            row = await self.row(
                "SELECT current_location,source_anchor FROM reading_progress"
            )
            self.assertEqual(tuple(row), (None, None))

    async def test_novel_incremental_counts_use_original_before_and_after(self):
        from app.stats.estimate_snapshots import WordCounts
        from app.stats.text_inventory import inventory_html

        await self.novel_chapter(1)
        source = "<p>name one two three four</p>"
        await self.db.execute(
            "UPDATE chapters SET content_html=?,content_size=? WHERE id=1",
            (source, len(source)),
        )
        rules = [dict(find="name", replace="longer character name")]
        old_anchor, new_anchor = (
            anchor(source, 9, chapterId=1),
            anchor(source, 13, chapterId=1),
        )
        # Canonical anchors identify two/three while deliberately stale displayed offsets remain 9/13.
        old = (1, 0, 9, 40, old_anchor)
        position = {
            "chapter_id": 1,
            "paragraph_index": 0,
            "character_offset": 13,
            "chapter_percent": 60,
            "source_anchor": new_anchor,
        }
        model = await self.estimator()
        model.previous_novel_counts[(1, None)] = (
            WordCounts(3, 3),
            ("set", "rules", old, True),
        )
        model.ready_novel_basis[(1, None)] = (
            "set",
            "rules",
            tuple(position.values()),
            True,
        )
        result = await model._novel_delta((1, None), position, rules)
        self.assertEqual(result, WordCounts(2, 2))
        self.assertGreater(inventory_html(source, rules).remaining(0, 0, 13), 2)

    async def _legacy_before_mutation(self, novel):
        from app.library.router import (
            add_text_replacement,
            update_text_replacement,
            delete_text_replacement,
        )
        from app import text_replacements
        from app.novels.scraper import tag_paragraphs
        from app.reader.source_anchors import parse_anchor

        class Form:
            headers = {}

            def __init__(self, values):
                self.values = values

            async def form(self):
                return self.values

        if novel:
            await self.novel_chapter(1)
            source = tag_paragraphs("<p>name one two three four</p>")
            await self.db.execute(
                "UPDATE chapters SET content_html=?,content_size=? WHERE id=1",
                (source, len(source)),
            )
        else:
            item = await self.book(1, text="name one two three four")
            with zipfile.ZipFile(item["file_path"]) as z:
                source = z.read("a.xhtml").decode()
            await self.db.execute(
                "INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(1,1,'S','s','/s')"
            )
            await self.db.execute("UPDATE items SET series_id=1 WHERE id=1")
        target = "novel" if novel else "series"
        for operation in ["add", "edit", "delete"]:
            await self.db.execute("DELETE FROM text_replacements")
            cursor = await self.db.execute(
                f"INSERT INTO text_replacements(user_id,{target}_id,find_text,replace_text) VALUES(1,1,?,?)",
                ("name", "old long name"),
            )
            identity = cursor.lastrowid
            if novel:
                await self.db.execute(
                    "INSERT OR REPLACE INTO novel_reading_progress(user_id,novel_id,chapter_id,paragraph_index,character_offset) VALUES(1,1,1,0,18)"
                )
            else:
                await self.db.execute(
                    "INSERT OR REPLACE INTO reading_progress(user_id,item_id,status,current_page,current_location) VALUES(1,1,'reading',0,'elidx:0:18')"
                )
            await self.db.commit()
            if operation == "add":
                await add_text_replacement(
                    Form(
                        dict(
                            target=f"{target}:1",
                            find_text="one",
                            replace_text="extended one",
                        )
                    ),
                    {"id": 1},
                    self.db,
                )
            elif operation == "edit":
                await update_text_replacement(
                    identity,
                    Form(dict(find_text="name", replace_text="new name")),
                    {"id": 1},
                    self.db,
                )
            else:
                await delete_text_replacement(identity, Form({}), {"id": 1}, self.db)
            table = "novel_reading_progress" if novel else "reading_progress"
            row = await self.row("SELECT * FROM " + table)
            saved = parse_anchor(row["source_anchor"])
            self.assertIsNotNone(
                saved, "Legacy anchor must be captured before old rules disappear"
            )
            self.assertEqual(saved["position"], {"index": 0, "offset": 9})
            self.assertEqual(
                row["character_offset"] if novel else row["current_location"],
                18 if novel else "elidx:0:18",
            )
            rules = await text_replacements.rules_for(self.db, 1, **{target + "_id": 1})
            self.assertEqual(remaining_html(source, rules, row["source_anchor"]), 3)

    async def test_legacy_epub_position_is_prepared_before_each_rule_mutation(self):
        await self._legacy_before_mutation(False)

    async def test_legacy_novel_position_is_prepared_before_each_rule_mutation(self):
        await self._legacy_before_mutation(True)

    async def test_legacy_archive_preparation_does_not_hold_the_reader_writer(self):
        import asyncio, threading
        from unittest.mock import patch
        from app.reader import source_anchors
        from app.reader.router import save_progress
        from app.library.router import add_text_replacement
        from tests.test_progress_validation import _JsonRequest

        class Form:
            headers = {}

            async def form(self):
                return dict(
                    target="series:1", find_text="name", replace_text="new name"
                )

        await self.book(1, text="name one two three four")
        await self.db.execute(
            "INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(1,1,'S','s','/s')"
        )
        await self.db.execute("UPDATE items SET series_id=1 WHERE id=1")
        await self.db.execute(
            "INSERT INTO reading_progress(user_id,item_id,status,current_page,current_location) VALUES(1,1,'reading',0,'elidx:0:9')"
        )
        await self.db.commit()
        entered, release = threading.Event(), threading.Event()
        real = source_anchors._capture_item

        def blocked(row, rules):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Archive control timed out")
            return real(row, rules)

        with patch.object(source_anchors, "_capture_item", side_effect=blocked):
            adding = asyncio.create_task(
                add_text_replacement(Form(), {"id": 1}, self.db)
            )
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                try:
                    await asyncio.wait_for(
                        save_progress(
                            1,
                            _JsonRequest(
                                dict(
                                    current_page=0,
                                    current_location="elidx:0:13",
                                    progress_percent=50,
                                )
                            ),
                            {"id": 1},
                            self.db,
                        ),
                        0.5,
                    )
                except TimeoutError:
                    self.fail("Legacy archive parsing held the reader writer")
            finally:
                release.set()
                await adding
        from app.reader.source_anchors import parse_anchor

        row = await self.row(
            "SELECT current_location,source_anchor FROM reading_progress"
        )
        self.assertEqual(row[0], "elidx:0:13")
        self.assertEqual(
            parse_anchor(row[1])["position"]["offset"],
            13,
            "Preparation must retry when a reader saved a newer position",
        )

    async def test_legacy_novel_preparation_uses_resource_cleanup_and_manifest_ordinal(
        self,
    ):
        from app.reader.source_anchors import (
            prepare_legacy_target,
            parse_anchor,
            display_position,
        )
        from app.novels.scraper import tag_paragraphs
        from app.novels.router import read_novel_progress

        await self.novel_chapter(1)
        await self.novel_chapter(2)
        raw = "<p>name one two</p><p><br></p><p>saved passage here</p>"
        await self.db.execute(
            "UPDATE chapters SET order_index=id*10,content_html=?,content_size=?",
            (raw, len(raw)),
        )
        await self.db.execute(
            "INSERT INTO novel_reading_progress(user_id,novel_id,chapter_id,paragraph_index,character_offset) VALUES(1,1,2,1,0)"
        )
        await self.db.commit()
        prepared = await prepare_legacy_target(self.db, 1, novel_id=1)
        value = prepared["values"][0][1]
        saved = parse_anchor(value)
        self.assertEqual(
            saved["section"], 1, "Section is manifest ordinal, not order_index=20"
        )
        self.assertEqual(saved["chapterId"], 2)
        self.assertEqual(display_position(tag_paragraphs(raw), [], saved), (1, 0))
        self.assertEqual(remaining_html(raw, [], value), 3)
        response = await read_novel_progress(1, {"id": 1}, self.db)
        self.assertEqual(json.loads(response.body)["position"][0], 1)
