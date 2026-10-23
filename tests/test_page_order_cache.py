"""A comic's page order is worked out once per file version, not per page.

Natural-sorting a CBZ's names was 0.32 of the 1.17 ms a page request took
on a 200-page volume -- for every page and every preload. The order is
kept by (path, file hash): 1.34 -> 0.92 ms a page, measured warm.
"""
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.reader import streaming


class PageOrderCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "c.cbz")
        self.write(["10.png", "2.png", "1.png"])
        streaming._page_order.clear()

    def write(self, names):
        with zipfile.ZipFile(self.path, "w") as z:
            for name in names:
                z.writestr(name, name.encode())

    def test_pages_come_in_natural_order(self):
        self.assertEqual(streaming.comic_page_bytes(self.path, 0, "h1")[0], b"1.png")
        self.assertEqual(streaming.comic_page_bytes(self.path, 2, "h1")[0], b"10.png")

    def test_the_order_is_sorted_once_per_version(self):
        calls = []
        original = streaming.page_names_from
        streaming.page_names_from = lambda names: calls.append(1) or original(names)
        self.addCleanup(lambda: setattr(streaming, "page_names_from", original))
        for page in range(3):
            streaming.comic_page_bytes(self.path, page, "h1")
        self.assertEqual(len(calls), 1)

    def test_a_replaced_file_is_listed_afresh(self):
        streaming.comic_page_bytes(self.path, 0, "h1")
        self.write(["b.png", "a.png"])
        self.assertEqual(streaming.comic_page_bytes(self.path, 0, "h2")[0], b"a.png")

    def test_it_is_bounded(self):
        for n in range(streaming._PAGE_ORDER_SIZE + 5):
            streaming.comic_page_bytes(self.path, 0, f"h{n}")
        self.assertEqual(len(streaming._page_order), streaming._PAGE_ORDER_SIZE)
