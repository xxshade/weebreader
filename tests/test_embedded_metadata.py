"""An item's embedded metadata: what the parsers read, the backfill that
re-reads the library under new rules, and the card that shows it.

The author is every creator credited as the author (not the first
dc:creator, which is the illustrator often enough), the item description is
plain text whatever markup the file wrapped it in, and the card shows both
in a bubble -- see GLOSSARY.md for the terms.
"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.config import settings
from app.database import close_pool, get_connection, init_db
from app.library import events, scanner
from app.library.parsers import cbz as cbz_parser
from app.library.parsers import epub as epub_parser
from app.library.parsers.plain_text import plain_text
from app.library.router import templates
from tests.test_archives import PNG_1PX

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:opf="http://www.idpf.org/2007/opf" version="{version}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Book</dc:title>
    {metadata}
  </metadata>
  <manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>"""



def write_epub(path: Path, metadata: str, version: str = "2.0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF.format(version=version, metadata=metadata))
        z.writestr("OEBPS/c1.xhtml", "<html><body><p>Text</p></body></html>")
    return path


def write_cbz(path: Path, comicinfo: str | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("001.png", PNG_1PX)
        if comicinfo is not None:
            z.writestr("ComicInfo.xml", f"<ComicInfo>{comicinfo}</ComicInfo>")
    return path


class PlainText(unittest.TestCase):
    def test_tags_go_and_paragraphs_stay(self):
        self.assertEqual(plain_text("<p>Tom &amp; Jerry</p><p>Second</p>"), "Tom & Jerry\n\nSecond")

    def test_a_br_is_a_line_break_not_a_paragraph(self):
        self.assertEqual(plain_text("<p>One<br/>two</p>"), "One\ntwo")

    def test_the_files_own_wrapping_collapses(self):
        # A pretty-printed OPF indents a description across lines; only a
        # blank line is a paragraph.
        self.assertEqual(plain_text("\n    A long\n    wrapped text\n\n    Next\n"),
                         "A long wrapped text\n\nNext")

    def test_plain_text_is_left_alone(self):
        self.assertEqual(plain_text("a < b & c"), "a < b & c")

    def test_nothing_is_none(self):
        for value in (None, "", "   ", "<p></p>", "<p> </p>"):
            self.assertIsNone(plain_text(value), value)


class EpubAuthors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def author(self, metadata, version="2.0"):
        path = write_epub(self.tmp / "b.epub", metadata, version)
        return epub_parser.parse_epub(str(path)).metadata.get("author")

    def test_an_illustrator_listed_first_is_not_the_author(self):
        self.assertEqual(self.author(
            '<dc:creator opf:role="ill">Artist</dc:creator>'
            '<dc:creator opf:role="aut">Writer</dc:creator>'), "Writer")

    def test_every_author_in_order(self):
        self.assertEqual(self.author(
            '<dc:creator opf:role="aut">One</dc:creator>'
            '<dc:creator opf:role="trl">Translator</dc:creator>'
            '<dc:creator>Two</dc:creator>'
            '<dc:creator opf:role="aut">One</dc:creator>'), "One, Two")

    def test_epub3_roles_come_from_refines(self):
        self.assertEqual(self.author(
            '<dc:creator id="a">Artist</dc:creator>'
            '<meta refines="#a" property="role" scheme="marc:relators">ill</meta>'
            '<dc:creator id="w">Writer</dc:creator>'
            '<meta refines="#w" property="role" scheme="marc:relators">aut</meta>', "3.0"), "Writer")

    def test_no_author_when_nobody_wrote_it(self):
        self.assertIsNone(self.author('<dc:creator opf:role="edt">Editor</dc:creator>'))


class EpubDescription(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def description(self, metadata):
        path = write_epub(self.tmp / "b.epub", metadata)
        return epub_parser.parse_epub(str(path)).metadata.get("description")

    def test_escaped_html(self):
        self.assertEqual(self.description(
            "<dc:description>&lt;p&gt;First&lt;/p&gt;&lt;p&gt;Second&lt;/p&gt;</dc:description>"),
            "First\n\nSecond")

    def test_html_written_as_child_elements(self):
        self.assertEqual(self.description(
            '<dc:description>Intro <p xmlns="http://www.w3.org/1999/xhtml">More</p></dc:description>'),
            "Intro More")

    def test_read_metadata_agrees_with_parse(self):
        path = write_epub(self.tmp / "b.epub",
                          '<dc:creator>W</dc:creator><dc:description>&lt;b&gt;D&lt;/b&gt;</dc:description>')
        self.assertEqual(epub_parser.read_metadata(str(path)), epub_parser.parse_epub(str(path)).metadata)


class ComicInfo(unittest.TestCase):
    def test_summary_loses_its_markup_and_writer_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cbz(Path(tmp) / "c.cbz",
                             "<Writer>A, B</Writer><Summary>&lt;p&gt;One&lt;/p&gt;&lt;p&gt;Two&lt;/p&gt;</Summary>")
            metadata = cbz_parser.parse_cbz(str(path)).metadata
            self.assertEqual(metadata["author"], "A, B")
            self.assertEqual(metadata["description"], "One\n\nTwo")
            self.assertEqual(cbz_parser.read_metadata(str(path)), metadata)


class Backfill(unittest.IsolatedAsyncioTestCase):
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
        self.root.mkdir()
        await self.execute("INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'L', ?, 'comic')",
                           (str(self.root),))

    async def execute(self, sql, params=()):
        db = await get_connection()
        try:
            await db.execute(sql, params)
            await db.commit()
        finally:
            await db.close()

    async def rows(self):
        db = await get_connection()
        try:
            return {r["file_path"].rsplit("/", 1)[-1]: dict(r) for r in await (await db.execute(
                "SELECT file_path, metadata, metadata_version FROM items")).fetchall()}
        finally:
            await db.close()

    async def test_an_import_records_the_rules_it_read_under(self):
        write_epub(self.root / "S" / "v1.epub", '<dc:creator opf:role="aut">W</dc:creator>')
        await scanner.scan_library(1, str(self.root))
        row = (await self.rows())["v1.epub"]
        self.assertEqual(row["metadata_version"], epub_parser.METADATA_VERSION)
        self.assertEqual(await scanner.backfill_metadata(), 0)

    async def test_old_rows_are_reread_once(self):
        write_epub(self.root / "S" / "v1.epub",
                   '<dc:creator opf:role="ill">Artist</dc:creator><dc:creator opf:role="aut">Writer</dc:creator>'
                   '<dc:description>&lt;p&gt;Hi&lt;/p&gt;</dc:description>')
        write_cbz(self.root / "S" / "v2.cbz", "<Summary>&lt;i&gt;Hey&lt;/i&gt;</Summary>")
        await scanner.scan_library(1, str(self.root))
        # As an import before this change left them: the first creator,
        # the description as the file had it, and no version.
        await self.execute("UPDATE items SET metadata = ?, metadata_version = NULL",
                           (json.dumps({"author": "Artist", "description": "<p>Hi</p>"}),))

        self.assertEqual(await scanner.backfill_metadata(), 2)
        rows = await self.rows()
        self.assertEqual(json.loads(rows["v1.epub"]["metadata"]), {"author": "Writer", "description": "Hi"})
        self.assertEqual(json.loads(rows["v2.cbz"]["metadata"]), {"description": "Hey"})
        self.assertEqual({r["metadata_version"] for r in rows.values()}, {epub_parser.METADATA_VERSION})

        self.assertEqual(await scanner.backfill_metadata(), 0)

    async def test_an_unreadable_file_keeps_what_it_had_and_stays_pending(self):
        path = write_epub(self.root / "S" / "v1.epub", "<dc:creator>W</dc:creator>")
        await scanner.scan_library(1, str(self.root))
        await self.execute("UPDATE items SET metadata = '{\"author\": \"Old\"}', metadata_version = NULL")
        path.write_bytes(b"not a zip")

        self.assertEqual(await scanner.backfill_metadata(), 0)
        row = (await self.rows())["v1.epub"]
        self.assertEqual(json.loads(row["metadata"]), {"author": "Old"})
        self.assertIsNone(row["metadata_version"])


class Card(unittest.TestCase):
    def render(self, metadata):
        item = {"id": 7, "title": "Vol 1", "metadata": metadata, "cover_path": None,
                "page_count": 10, "file_format": "cbz", "manual_order": None}
        series = {"id": 1, "manual_sort": 0, "manual_sort_editing": 0}
        return templates.env.get_template("library/_item_card.html").render(
            series=series, item=item, progress_map={}, eta_map={}, original_rank={})

    def test_author_and_description_are_in_the_bubble(self):
        html = self.render(json.dumps({"author": "Writer", "description": "One\n\nTwo"}))
        self.assertIn("data-tip-toggle", html)
        self.assertIn('class="block font-semibold text-neutral-100">Writer<', html)
        self.assertIn('class="item-about-text">One\n\nTwo<', html)

    def test_either_one_is_enough(self):
        self.assertIn("data-tip-toggle", self.render(json.dumps({"author": "Writer"})))
        self.assertIn("data-tip-toggle", self.render(json.dumps({"description": "Text"})))

    def test_nothing_to_say_means_no_marker(self):
        for metadata in (None, "", "{}", json.dumps({"publisher": "P"}), "not json", "[1]"):
            self.assertNotIn("data-tip-toggle", self.render(metadata), metadata)

    def test_the_description_is_text_not_markup(self):
        html = self.render(json.dumps({"description": "<script>alert(1)</script>"}))
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
