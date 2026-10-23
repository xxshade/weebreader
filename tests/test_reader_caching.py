"""What a browser is told it may keep, and for how long.

`immutable` means "never ask about this again". It is safe on /covers
because those names carry the hash of what is in them, so different bytes
always mean a different URL (see AGENTS.md). The reader's own routes broke
that rule: /reader/7/page/3 goes on naming page 3 of item 7 after the file
behind it is replaced by a better scan, and it answered `immutable`
unconditionally -- so every browser that had already fetched a page kept
serving it, without asking, for a year. The ETag changed underneath and
nothing ever looked at it.

The fix is the one the novel covers already use: the URL says which version
it wants, and only a request naming the current one gets the long answer.

`page_dimensions` had the same shape of bug from the other side -- a cache
with no invalidation -- and it is here rather than with the scanner's tests
because it is the same question: what is still true about an item after its
file changes.
"""
import re
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app.asset_versioning import STATIC_DIR, asset_url, digest_for
from app.config import settings
from app.database import get_connection, init_db
from app.main import VersionedStaticFiles
from app.reader.router import _content_cache_headers, comic_page, epub_resource

ROOT = Path(__file__).resolve().parent.parent
HASH = "a" * 64


def _request(headers=None):
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("test", 1), "server": ("test", 80),
    })


class TheTwoAnswers(unittest.TestCase):
    def test_the_current_version_may_be_kept_for_good(self):
        header = _content_cache_headers(HASH, HASH)["Cache-Control"]
        self.assertIn("immutable", header)
        self.assertIn("max-age=31536000", header)

    def test_no_version_revalidates(self):
        # An image inside an EPUB chapter, resolved against a `<base href>`
        # -- a relative URL drops the query, so these arrive bare and must
        # not be told to stop asking.
        header = _content_cache_headers(None, HASH)["Cache-Control"]
        self.assertNotIn("immutable", header)
        self.assertIn("must-revalidate", header)

    def test_a_stale_version_revalidates(self):
        # THE case. A URL naming a version that is no longer current is
        # exactly the one that must never be answered `immutable`.
        header = _content_cache_headers("b" * 64, HASH)["Cache-Control"]
        self.assertNotIn("immutable", header)

    def test_an_empty_version_is_not_a_match(self):
        # Guards `asked == current` against both being falsy, which would
        # hand `immutable` to a request that named nothing.
        self.assertNotIn("immutable", _content_cache_headers("", "")["Cache-Control"])


class TheReaderSendsIt(unittest.TestCase):
    """The rule above only works if the client actually puts the version on
    the URLs it builds -- and the reader builds them in four places."""

    JS = Path(__file__).resolve().parent.parent / "app/static/js"

    def test_the_comic_reader_builds_every_page_url_through_one_helper(self):
        source = (self.JS / "reader-comic.js").read_text()
        self.assertIn("function pageUrlFor(chapterItemId, pageIndex)", source)
        # Exactly one place writes the URL out, and it is the helper -- a
        # second call site is how one of the three (the page in view, the
        # prefetched next chapter, the scrolled-to one) quietly goes back
        # to fetching without a version.
        built = re.findall(r"`/reader/\$\{[A-Za-z.]+\}/page/", source)
        self.assertEqual(len(built), 1, f"page URLs built in {len(built)} places, expected 1")
        self.assertIn("${query}", source.split("function pageUrlFor")[1][:300])

    def test_the_comic_reader_learns_each_chapters_version(self):
        source = (self.JS / "reader-comic.js").read_text()
        # The item it opened with, off the page...
        self.assertIn("root.dataset.version", source)
        # ...and every chapter it appends or hands over to, off
        # /chapter-info, which is why that endpoint reports it.
        self.assertIn("versions.set(String(info.id), info.version)", source)

    def test_the_text_reader_puts_it_on_chapter_urls(self):
        source = (self.JS / "reader-text.js").read_text()
        self.assertIn("resource/${entry.href}${versionQuery()}", source)
        self.assertIn("contentVersion = data.version", source)

    def test_the_comic_template_carries_the_hash(self):
        template = (Path(__file__).resolve().parent.parent
                    / "app/templates/reader/comic.html").read_text()
        self.assertIn('data-version="{{ item.file_hash }}"', template)


class PageDimensionsCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)

    async def _close(self):
        await self.db.close()


class TheRouteAnswersWithThem(PageDimensionsCase):
    """The helper above decides; these two are the routes actually using
    it, because a header is only as good as the response carrying it."""

    async def _item(self):
        library = Path(self._tmp.name) / "served"
        library.mkdir()
        comic = library / "c.cbz"
        _write_cbz(comic, pages=2, size=(60, 90))
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (9, 'L', ?, 'comic')",
            (str(library),))
        await self.db.commit()
        from app.library import scanner
        await scanner.process_file(9, str(library), comic)
        return await (await self.db.execute("SELECT id, file_hash FROM items")).fetchone()

    async def test_a_page_asked_for_by_version_may_be_kept(self):
        item = await self._item()
        response = await comic_page(item["id"], 0, _request(), v=item["file_hash"],
                                    user={"id": 1}, db=self.db)
        self.assertIn("immutable", response.headers["cache-control"])

    async def test_a_page_asked_for_without_one_revalidates(self):
        item = await self._item()
        response = await comic_page(item["id"], 0, _request(), v=None, user={"id": 1}, db=self.db)
        self.assertNotIn("immutable", response.headers["cache-control"])
        # The validator is still there, so revalidating is one round trip
        # with no body rather than the page again.
        self.assertTrue(response.headers["etag"])

    async def test_the_etag_still_answers_304(self):
        item = await self._item()
        etag = f'"{item["file_hash"]}-0"'
        response = await comic_page(item["id"], 0, _request({"if-none-match": etag}),
                                    v=None, user={"id": 1}, db=self.db)
        self.assertEqual(response.status_code, 304)

    async def test_an_epub_resource_follows_the_same_rule(self):
        library = Path(self._tmp.name) / "books"
        library.mkdir()
        book = library / "b.epub"
        _write_epub(book)
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (8, 'B', ?, 'ebook')",
            (str(library),))
        await self.db.commit()
        from app.library import scanner
        await scanner.process_file(8, str(library), book)
        item = await (await self.db.execute(
            "SELECT id, file_hash FROM items WHERE file_format = 'epub'")).fetchone()

        current = await epub_resource(item["id"], "ch1.xhtml", _request(), v=item["file_hash"],
                                      user={"id": 1}, db=self.db)
        self.assertIn("immutable", current.headers["cache-control"])
        stale = await epub_resource(item["id"], "ch1.xhtml", _request(), v="other",
                                    user={"id": 1}, db=self.db)
        self.assertNotIn("immutable", stale.headers["cache-control"])


class TheDimensionsCacheFollowsTheFile(PageDimensionsCase):
    """A comic's page sizes are measured once and cached in
    `page_dimensions`, and /dimensions only recomputes when the ROW COUNT
    disagrees with page_count. So a file replaced by one with the SAME
    number of pages -- a re-encode, a better scan, the ordinary kind --
    left the old sizes in place and served them forever: every placeholder
    in continuous scroll reserved at the wrong aspect ratio, which is the
    one thing the cache exists to prevent.

    Reproduced end to end before this: swapping a 1600x2400 CBZ for an
    800x1200 one with the same 200 pages, /dimensions kept answering
    1600x2400 while the pages themselves came back 800x1200.
    """

    async def test_a_changed_file_drops_the_sizes_measured_from_the_old_one(self):
        from app.library import scanner

        library = Path(self._tmp.name) / "lib"
        library.mkdir()
        comic = library / "a.cbz"
        _write_cbz(comic, pages=2, size=(100, 200))

        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'L', ?, 'comic')",
            (str(library),))
        await self.db.commit()
        await scanner.process_file(1, str(library), comic)

        item = await (await self.db.execute("SELECT id, file_hash FROM items")).fetchone()
        await self.db.execute(
            "INSERT INTO page_dimensions (item_id, page_index, width, height) VALUES (?, 0, 100, 200)",
            (item["id"],))
        await self.db.execute(
            "INSERT INTO page_dimensions (item_id, page_index, width, height) VALUES (?, 1, 100, 200)",
            (item["id"],))
        await self.db.commit()

        # Same page count, different pages.
        _write_cbz(comic, pages=2, size=(50, 80))
        await scanner.process_file(1, str(library), comic)

        changed = await (await self.db.execute("SELECT file_hash FROM items")).fetchone()
        self.assertNotEqual(changed["file_hash"], item["file_hash"], "the file did not change")
        left = await (await self.db.execute(
            "SELECT COUNT(*) AS n FROM page_dimensions WHERE item_id = ?", (item["id"],))).fetchone()
        self.assertEqual(left["n"], 0, "sizes measured from the replaced file were kept")

    async def test_an_unchanged_file_keeps_them(self):
        # The cache is worth having: a rescan that finds the same bytes
        # must not throw away a measurement it would only redo.
        from app.library import scanner

        library = Path(self._tmp.name) / "lib2"
        library.mkdir()
        comic = library / "b.cbz"
        _write_cbz(comic, pages=2, size=(100, 200))
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (2, 'L2', ?, 'comic')",
            (str(library),))
        await self.db.commit()
        await scanner.process_file(2, str(library), comic)

        item = await (await self.db.execute("SELECT id FROM items")).fetchone()
        await self.db.execute(
            "INSERT INTO page_dimensions (item_id, page_index, width, height) VALUES (?, 0, 100, 200)",
            (item["id"],))
        await self.db.commit()

        await scanner.process_file(2, str(library), comic)
        left = await (await self.db.execute(
            "SELECT COUNT(*) AS n FROM page_dimensions WHERE item_id = ?", (item["id"],))).fetchone()
        self.assertEqual(left["n"], 1)


def _write_epub(path: Path) -> None:
    """The smallest EPUB the parser will accept, with one chapter to ask
    for by name."""
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml",
                    '<?xml version="1.0"?><container version="1.0" '
                    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="content.opf" '
                    'media-type="application/oebps-package+xml"/></rootfiles></container>')
        zf.writestr("content.opf",
                    '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                    'version="3.0" unique-identifier="id"><metadata '
                    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                    '<dc:identifier id="id">x</dc:identifier><dc:title>B</dc:title></metadata>'
                    '<manifest><item id="c1" href="ch1.xhtml" '
                    'media-type="application/xhtml+xml"/></manifest>'
                    '<spine><itemref idref="c1"/></spine></package>')
        zf.writestr("ch1.xhtml",
                    '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                    '<body><p>text</p></body></html>')


def _static_cache_control(named: str, query: str) -> str:
    """What /static answers for one file requested with one query string."""
    files = VersionedStaticFiles(directory=str(STATIC_DIR))
    full = STATIC_DIR / named
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": f"/{named}", "raw_path": f"/{named}".encode(), "root_path": "",
        "query_string": query.encode(), "headers": [],
        "client": ("test", 1), "server": ("test", 80),
    }
    response = files.file_response(str(full), full.stat(), scope)
    return response.headers["Cache-Control"]


class StaticIsImmutableOnlyOnTheFingerprintedUrl(unittest.TestCase):
    """The same rule as the reader's `?v=`, one mount over.

    /static/js/home.js?v=9142fa4762 always answers with the same bytes --
    the file changing is what changes the URL -- so `immutable` is true
    there. The BARE path is not: it goes on naming the same file after a
    deploy replaces it, and `immutable` on that pins a stale script in
    every cache that holds it, with no deploy able to reach in.

    `public` rather than the reader's `private` because nothing under
    /static is personal to a session.
    """

    NAMED = "js/home.js"
    OTHER = "js/user-menu.js"

    def setUp(self):
        for name in (self.NAMED, self.OTHER):
            if not (STATIC_DIR / name).is_file():
                self.skipTest(f"{name} is gone; point this test at a file that exists")

    def test_the_current_version_may_be_kept_for_good(self):
        header = _static_cache_control(self.NAMED, f"v={digest_for(self.NAMED)}")
        self.assertIn("immutable", header)
        self.assertIn("max-age=31536000", header)
        self.assertIn("public", header)

    def test_the_bare_path_revalidates(self):
        # THE case this exists for.
        header = _static_cache_control(self.NAMED, "")
        self.assertNotIn("immutable", header)
        self.assertIn("must-revalidate", header)

    def test_a_version_from_another_deploy_revalidates(self):
        header = _static_cache_control(self.NAMED, "v=deadbeef00")
        self.assertNotIn("immutable", header)
        self.assertIn("must-revalidate", header)

    def test_an_empty_version_revalidates(self):
        header = _static_cache_control(self.NAMED, "v=")
        self.assertNotIn("immutable", header)

    def test_another_files_current_version_revalidates(self):
        # The hash is real and current -- for a DIFFERENT file. Comparing
        # against "some known hash" rather than against THIS file's would
        # pass this by accident.
        header = _static_cache_control(self.NAMED, f"v={digest_for(self.OTHER)}")
        self.assertNotIn("immutable", header)


class TheUrlTemplatesRenderIsTheOneThatEarnsImmutable(unittest.TestCase):
    """The invariant the whole thing rests on, and the way it would rot.

    asset_url() writes the `v`; VersionedStaticFiles reads it back. If those
    two ever computed it differently -- a different hash length, a prefix,
    one of them normalising the path -- nothing would break loudly: every
    asset would quietly fall to the 60-second answer and the feature would
    be off with all of its tests still green.
    """

    def test_every_asset_url_a_template_could_render_earns_the_long_answer(self):
        served = sorted(p.relative_to(STATIC_DIR).as_posix()
                        for p in STATIC_DIR.rglob("*")
                        if p.is_file() and p.suffix in {".js", ".css", ".svg"})
        self.assertGreater(len(served), 5, "found almost nothing under /static")
        for named in served:
            url = asset_url(named)
            self.assertTrue(url.startswith(f"/static/{named}?v="), url)
            query = url.split("?", 1)[1]
            self.assertIn("immutable", _static_cache_control(named, query),
                          f"{named}: the URL asset_url renders does not earn immutable")

    def test_a_page_is_still_answered_by_the_server(self):
        """The middleware that keeps a page from being served stale.

        A separate rule from the one above, and this change must not have
        shadowed it: /static answering `public` is only correct because
        nothing personal is served from there.

        `no-cache`, not `no-store`: both forbid a STALE answer, and only
        `no-cache` lets the browser keep the page, which is what the
        back/forward cache is. Whichever word, the page may not come back
        from a cache without the server being asked.
        """
        source = (ROOT / "app/main.py").read_text()
        # The middleware's own body: main.py sets Cache-Control in three
        # places, and the first two are the static mounts.
        self.assertIn("async def revalidate_pages", source, "the page rule is gone")
        middleware = source.split("async def revalidate_pages", 1)[1]
        directive = re.search(r'response\.headers\["Cache-Control"\] = "([^"]+)"', middleware)
        self.assertIsNotNone(directive, "nothing sets Cache-Control on a page any more")
        self.assertIn("no-cache", directive.group(1),
                      "a page may now be answered from a cache without asking")
        self.assertNotIn("max-age", directive.group(1),
                         "a page can now be served stale for that long")
        # Scoped by content type, so /static and /covers keep theirs.
        self.assertIn('"text/html" not in response.headers.get("content-type", "")', middleware)
        self.assertFalse(list(STATIC_DIR.rglob("*.html")),
                         "an .html under /static would be served public and cacheable")


def _write_cbz(path: Path, *, pages: int, size: tuple[int, int]) -> None:
    import io
    import zipfile

    from PIL import Image

    with zipfile.ZipFile(path, "w") as zf:
        for i in range(pages):
            buffer = io.BytesIO()
            Image.new("RGB", size, (i * 10 % 256, 0, 0)).save(buffer, "PNG")
            zf.writestr(f"{i + 1:03}.png", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
