"""A volume published in parts ("v08 Part 1", "v08 Part 2") -- see Part in
GLOSSARY.md.

Two rules: a Part's number is never the series' number, and items with the
same number are ordered by their FILENAME, not by an EPUB's dc:title, which
can differ between two releases in ways nobody sees on screen.
"""
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.config import settings
from app.database import close_pool, get_connection, init_db
from app.library import events, scanner
from app.library.naturalsort import extract_candidates, natural_sort_key, resolve_series_numbers
from tests.test_embedded_metadata import CONTAINER

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title></metadata>
  <manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""


def write_titled_epub(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF.format(title=title))
        z.writestr("OEBPS/c1.xhtml", "<html><body><p>x</p></body></html>")


def numbers(names):
    """(title, filename stem) pairs -> the numbers the series resolves to."""
    resolved = resolve_series_numbers([(i, extract_candidates(t, s)) for i, (t, s) in enumerate(names)])
    return [resolved.numbers.get(i) for i in range(len(names))], resolved.kind


class APartIsNotTheSeriesNumber(unittest.TestCase):
    def test_its_number_is_no_candidate(self):
        bare = [c.value for c in extract_candidates("Novel Name: Volume 8 Part 2") if c.kind == "bare"]
        self.assertNotIn(2.0, bare)

    def test_roman_and_abbreviated(self):
        for name in ("Novel v08 Part II", "Novel v08 Pt. 2", "Novel v08 pt2"):
            self.assertEqual(numbers([(name, name)])[0], [8.0], name)

    def test_a_short_series_keeps_its_volume_numbers(self):
        # THE case: with only these three, "Part 1"/"Part 2" outvoted the
        # volume markers and numbered them 7, 1, 2 -- v07 sorted last.
        names = [("Novel Name: Volume 7", "Novel Name v07 - Seven"),
                 ("Novel Name: Volume 8 Part 1", "Novel Name v08 Part 1 - One"),
                 ("Novel Name: Volume 8 Part 2", "Novel Name v08 Part 2 - Two")]
        self.assertEqual(numbers(names), ([7.0, 8.0, 8.0], "volume"))

    def test_a_name_that_only_says_part_still_has_its_number(self):
        # Nothing else in it: the part is taken out and nothing is left.
        self.assertEqual(numbers([("Novel Part 3", "Novel Part 3")])[0], [None])


class SameNumberOrderedByFilename(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        original = settings.data_dir
        settings.data_dir = self.tmp / "data"
        settings.data_dir.mkdir()
        (settings.data_dir / "covers").mkdir()
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        original_publish = events.publish
        events.publish = lambda event: None
        self.addCleanup(lambda: setattr(events, "publish", original_publish))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        await init_db()
        self.root = self.tmp / "lib"
        db = await get_connection()
        try:
            await db.execute("INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'L', ?, 'comic')",
                             (str(self.root),))
            await db.commit()
        finally:
            await db.close()
        folder = self.root / "Novel Name"
        write_titled_epub(folder / "Novel Name v07 - Seven.epub", "Novel Name: Volume 7")
        # The same title on screen; Part 1's has a non-breaking space where
        # Part 2's has a space, which sorts it AFTER Part 2 by title.
        write_titled_epub(folder / "Novel Name v08 Part 1 - Zone 3.epub", "Novel Name: Volume 8 Part 1")
        write_titled_epub(folder / "Novel Name v08 Part 2 - Doppelgänger.epub", "Novel Name: Volume 8 Part 2")
        await scanner.scan_library(1, str(self.root))

    async def order(self):
        db = await get_connection()
        try:
            rows = await (await db.execute(
                "SELECT file_path FROM items "
                "ORDER BY (number_in_series IS NULL), number_in_series, sort_title")).fetchall()
        finally:
            await db.close()
        return [Path(r["file_path"]).stem.split(" - ")[0] for r in rows]

    async def test_the_parts_follow_their_filenames(self):
        self.assertEqual(await self.order(),
                         ["Novel Name v07", "Novel Name v08 Part 1", "Novel Name v08 Part 2"])

    async def test_rows_keyed_by_title_are_rekeyed_once(self):
        db = await get_connection()
        try:
            for row in await (await db.execute("SELECT id, title FROM items")).fetchall():
                await db.execute("UPDATE items SET sort_title = ? WHERE id = ?",
                                 (natural_sort_key(row["title"]), row["id"]))
            await db.commit()
        finally:
            await db.close()
        self.assertEqual((await self.order())[1:], ["Novel Name v08 Part 2", "Novel Name v08 Part 1"])

        self.assertEqual(await scanner.backfill_sort_title(), 3)
        self.assertEqual(await self.order(),
                         ["Novel Name v07", "Novel Name v08 Part 1", "Novel Name v08 Part 2"])
        self.assertEqual(await scanner.backfill_sort_title(), 0)
