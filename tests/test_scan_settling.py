"""A scan settles each series it changed once, and only those.

- Every scan renumbered and re-covered EVERY series of the library inside
  one write transaction, whether or not anything had changed: 400 series
  held the write lock 651 ms on a scan that found nothing.
- Every imported file renumbered its whole series, so importing a series
  was quadratic: 1000 files took 25.9 s, 18.4 s of it renumbering.
- The cover (the first item IN ORDER) was recomputed before the numbers
  that order it.
"""
from app.library import scanner
from tests.test_library_moves import Case, png, write_cbz

# Each file's own reading is 10, 20, 30 and 4; the series as a whole is
# numbered by volume, 1 to 4 -- so the first item changes once it is settled.
NAMES = ["[G] Serie - c010 (v01)", "[G] Serie - c020 (v02)", "[G] Serie - c030 (v03)", "Serie v04"]


class Counting(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.renumbered = []
        original = scanner._renumber_series

        async def counted(db, series_id):
            self.renumbered.append(series_id)
            return await original(db, series_id)

        scanner._renumber_series = counted
        self.addCleanup(lambda: setattr(scanner, "_renumber_series", original))

    def write_series(self, folder="Serie", names=NAMES):
        for index, name in enumerate(names):
            write_cbz(self.root / folder / f"{name}.cbz", cover=png((index * 60, 0, 0)), tag=name)


class AScan(Counting):
    async def test_renumbers_a_series_it_imported_once(self):
        self.write_series()
        await self.scan()
        self.assertEqual(len(self.renumbered), 1)

    async def test_leaves_alone_what_it_did_not_change(self):
        self.write_series()
        self.write_series("Other", ["Other v01", "Other v02"])
        await self.scan()
        self.renumbered.clear()
        await self.scan()
        self.assertEqual(self.renumbered, [])

    async def test_settles_a_series_an_item_left(self):
        self.write_series()
        await self.scan()
        self.renumbered.clear()
        (self.root / "Serie" / f"{NAMES[0]}.cbz").unlink()
        await self.scan()
        series = await self.rows("SELECT id FROM series")
        self.assertEqual(self.renumbered, [series[0]["id"]])

    async def test_numbers_the_series_as_a_whole(self):
        self.write_series()
        await self.scan()
        rows = await self.rows("SELECT title, number_in_series FROM items ORDER BY number_in_series")
        self.assertEqual([row["number_in_series"] for row in rows], [1.0, 2.0, 3.0, 4.0])

    async def test_the_cover_is_the_first_item_by_the_settled_numbers(self):
        self.write_series()
        await self.scan()
        first = await self.rows("SELECT cover_path FROM items WHERE number_in_series = 1")
        series = await self.rows("SELECT cover_path FROM series")
        self.assertEqual(series[0]["cover_path"], first[0]["cover_path"])

    async def test_says_when_the_numbers_moved(self):
        self.write_series()
        await self.scan()
        series_id = (await self.rows("SELECT id FROM series"))[0]["id"]
        self.assertIn({"type": "item_updated", "series_id": series_id}, self.published)


class TheWatcher(Counting):
    async def test_still_renumbers_each_file_it_imports(self):
        self.write_series()
        for name in NAMES:
            await scanner.process_file(1, str(self.root), self.root / "Serie" / f"{name}.cbz")
        self.assertEqual(len(self.renumbered), len(NAMES))
        rows = await self.rows("SELECT number_in_series FROM items ORDER BY number_in_series")
        self.assertEqual([row["number_in_series"] for row in rows], [1.0, 2.0, 3.0, 4.0])
        first = await self.rows("SELECT cover_path FROM items WHERE number_in_series = 1")
        series = await self.rows("SELECT cover_path FROM series")
        self.assertEqual(series[0]["cover_path"], first[0]["cover_path"])


if __name__ == "__main__":
    import unittest
    unittest.main()
