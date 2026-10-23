"""Server-side search over series names and item titles (app/library/search.py)."""
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.library.naturalsort import natural_sort_key
from app.library.search import RESULT_LIMIT, search_items


class SearchCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'Manga', '/lib', 'comic')"
        )
        self.next_item = 1

    async def _close(self):
        await self.db.close()

    async def series(self, series_id, name):
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path) VALUES (?, 1, ?, ?, ?)",
            (series_id, name, natural_sort_key(name), name),
        )
        await self.db.commit()

    async def item(self, title, *, series_id=None, number=None):
        item_id = self.next_item
        self.next_item += 1
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path, "
            "file_format, file_hash, file_size, number_in_series) "
            "VALUES (?, 1, ?, ?, ?, ?, 'cbz', ?, 1, ?)",
            (item_id, series_id, title, natural_sort_key(title), f"/lib/{title}.cbz",
             f"hash{item_id}", number),
        )
        await self.db.commit()
        return item_id

    async def titles(self, query, **kwargs):
        return [r["title"] for r in await search_items(self.db, query, **kwargs)]


class ItemSearch(SearchCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.series(1, "Oyasumi Punpun")
        for v in (1, 2, 7, 10):
            await self.item(f"Oyasumi Punpun v{v:02d}", series_id=1, number=float(v))
        await self.series(2, "Vinland Saga")
        await self.item("Vinland Saga v01", series_id=2, number=1.0)

    async def test_finds_a_volume_by_a_word_in_its_title(self):
        self.assertEqual(len(await self.titles("punpun")), 4)

    async def test_matching_is_case_insensitive(self):
        self.assertEqual(await self.titles("PUNPUN"), await self.titles("punpun"))

    async def test_terms_are_combined(self):
        # "punpun 07" must mean both words, not either -- otherwise every
        # volume of every series numbered 07 comes back too.
        self.assertEqual(await self.titles("punpun v07"), ["Oyasumi Punpun v07"])

    async def test_terms_may_appear_in_any_order(self):
        self.assertEqual(await self.titles("v07 punpun"), ["Oyasumi Punpun v07"])

    async def test_a_number_in_the_query_matches(self):
        # The trap this pins: sort_title holds natural_sort_key's output,
        # which zero-pads digit runs ("Oyasumi Punpun v07" is stored as
        # "...v0000000007"), so searching that column for "v07" silently
        # returns nothing. The match has to be against `title`.
        self.assertEqual(await self.titles("v07"), ["Oyasumi Punpun v07"])
        self.assertEqual(await self.titles("v10"), ["Oyasumi Punpun v10"])

    async def test_results_carry_their_series_name(self):
        # A hit is only actionable if it says which series it belongs to --
        # the whole point is finding a volume when you don't already know.
        rows = await search_items(self.db, "punpun v01")
        self.assertEqual(rows[0]["series_name"], "Oyasumi Punpun")

    async def test_a_standalone_item_has_no_series_name(self):
        await self.item("Avulso Sem Serie")
        rows = await search_items(self.db, "avulso")
        self.assertIsNone(rows[0]["series_name"])

    async def test_results_are_ordered_by_series_then_number(self):
        self.assertEqual(await self.titles("v01"),
                         ["Oyasumi Punpun v01", "Vinland Saga v01"])

    async def test_an_empty_query_returns_nothing(self):
        # Not "everything": this backs a search box that is empty most of
        # the time, and the endpoint is hit on page load.
        self.assertEqual(await self.titles(""), [])
        self.assertEqual(await self.titles("   "), [])

    async def test_a_series_name_alone_does_not_expand_into_its_volumes(self):
        # "vinland" matches that SERIES, which is reported as a series --
        # listing every volume under it as well would bury the item hits.
        await self.series(3, "Vinland Companion")
        self.assertEqual(await self.titles("companion"), [])

    async def test_wildcards_in_the_query_are_not_special(self):
        # A user typing % or _ should search for those characters, not turn
        # the query into a match-anything pattern.
        await self.item("100% Sure", series_id=1)
        self.assertEqual(await self.titles("100%"), ["100% Sure"])
        self.assertEqual(await self.titles("%"), ["100% Sure"])


class ItemSearchLimits(SearchCase):
    async def test_one_row_past_the_limit_says_it_clipped(self):
        await self.series(1, "Serie")
        for v in range(1, RESULT_LIMIT + 5):
            await self.item(f"Serie v{v:03d}", series_id=1, number=float(v))
        self.assertEqual(len(await search_items(self.db, "serie")), RESULT_LIMIT + 1)

    async def test_a_query_matching_nothing_finds_nothing(self):
        await self.series(1, "Oyasumi Punpun")
        await self.item("Oyasumi Punpun v01", series_id=1, number=1.0)
        self.assertEqual(await search_items(self.db, "berserk"), [])

    async def test_an_absurd_query_is_capped_not_refused(self):
        # MAX_TERMS keeps a pasted paragraph from becoming dozens of LIKE
        # clauses per row; it must still answer rather than error.
        await self.series(1, "Oyasumi Punpun")
        await self.item("Oyasumi Punpun v01", series_id=1, number=1.0)
        self.assertEqual(len(await search_items(self.db, "oyasumi " * 50)), 1)


if __name__ == "__main__":
    unittest.main()
