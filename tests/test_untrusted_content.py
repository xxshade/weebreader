"""Bytes the app serves but did not write run nothing when opened as a page.

A file inside an EPUB and a novel cover as its source site sent it are
served from this origin. Opened as a page -- the book's own
`target="_blank"` link, or the URL typed in -- such a file ran its scripts
with the reader's session: measured in Chromium and Firefox, a book's page
fetched /settings/security and got 200, and so did an SVG cover. The answer
is `Content-Security-Policy: sandbox` (no `allow-scripts`).
"""
import unittest
from pathlib import Path

from app.http_utils import UNTRUSTED_CONTENT_HEADERS
from app.novels.router import novel_cover
from app.reader.router import epub_resource
from tests.test_reader_caching import PageDimensionsCase, _request, _write_epub


class TheHeaders(unittest.TestCase):
    def test_the_sandbox_allows_no_scripts(self):
        policy = UNTRUSTED_CONTENT_HEADERS["Content-Security-Policy"]
        self.assertTrue(policy.startswith("sandbox"))
        self.assertNotIn("allow-scripts", policy)

    def test_the_type_is_not_sniffed(self):
        self.assertEqual(UNTRUSTED_CONTENT_HEADERS["X-Content-Type-Options"], "nosniff")


class TheRoutesSendThem(PageDimensionsCase):
    def assertSandboxed(self, response):
        for name, value in UNTRUSTED_CONTENT_HEADERS.items():
            self.assertEqual(response.headers.get(name), value, name)

    async def _book(self):
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
        return await (await self.db.execute(
            "SELECT id, file_hash FROM items WHERE file_format = 'epub'")).fetchone()

    async def test_a_file_out_of_an_epub(self):
        item = await self._book()
        response = await epub_resource(item["id"], "ch1.xhtml", _request(), v=None,
                                       user={"id": 1}, db=self.db)
        self.assertEqual(response.status_code, 200)
        self.assertSandboxed(response)

    async def test_its_revalidation_too(self):
        # A 304 updates the headers a browser stored, so a copy cached
        # before this change picks the sandbox up on its next revalidation.
        item = await self._book()
        first = await epub_resource(item["id"], "ch1.xhtml", _request(), v=None,
                                    user={"id": 1}, db=self.db)
        again = await epub_resource(item["id"], "ch1.xhtml",
                                    _request({"if-none-match": first.headers["etag"]}),
                                    v=None, user={"id": 1}, db=self.db)
        self.assertEqual(again.status_code, 304)
        self.assertSandboxed(again)

    async def test_a_novel_cover_stored_as_the_source_sent_it(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url, cover_image, cover_image_content_type, "
            "cover_image_hash) VALUES (3, 'N', 'https://example.test/n', ?, 'image/svg+xml', 'h')",
            (svg,))
        await self.db.commit()
        response = await novel_cover(3, v="h", user={"id": 1}, db=self.db)
        self.assertEqual(response.body, svg)
        self.assertSandboxed(response)


if __name__ == "__main__":
    unittest.main()
