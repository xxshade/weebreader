"""An EPUB resource's ETag is a valid header whatever the file is called.

Headers are Latin-1. With the path written into the tag, a chapter named
Text/第一章.xhtml raised UnicodeEncodeError: a 500, and "Couldn't load a
chapter" for every CJK- or Cyrillic-named file in a book.
"""
import unittest

from app.reader.router import _resource_etag


class ResourceEtag(unittest.TestCase):
    def test_any_name_makes_a_latin1_header(self):
        for path in ("Text/第一章.xhtml", "Глава 1.xhtml", "chapitre-été.xhtml", "c1.xhtml"):
            _resource_etag("abc", path).encode("latin-1")

    def test_it_still_tells_two_resources_apart(self):
        self.assertNotEqual(_resource_etag("abc", "a.xhtml"), _resource_etag("abc", "b.xhtml"))
        self.assertNotEqual(_resource_etag("abc", "a.xhtml"), _resource_etag("abd", "a.xhtml"))
