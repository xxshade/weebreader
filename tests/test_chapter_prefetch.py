"""Paginated reading fetches the chapter after the one on screen as soon as
that one is shown, instead of waiting for the turn past its last page.

Measured in both browsers: opening a book requested c1 alone, and each next
chapter only when the turn into it asked for it; now c1 and c2 on opening,
no chapter twice, and a failed early fetch is retried by the turn without a
warning shown before the reader gets there. This pins the shape in the
source; the behaviour is measured in the browsers (see CLAUDE.md).
"""
import re
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "app/static/js/reader-text.js"


def _code() -> str:
    source = JS.read_text()
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def _function(code: str, name: str) -> str:
    start = code.index(f"function {name}(")
    return code[start:code.index("\n    }\n", start)]


class NextChapterPrefetch(unittest.TestCase):
    def test_showing_a_page_fetches_the_next_chapter(self):
        self.assertIn("prefetchChapter(spineIndex + 1);", _function(_code(), "renderPage"))

    def test_the_turn_uses_the_early_fetch_and_retries_a_failed_one(self):
        body = _function(_code(), "loadChapterBlocks")
        self.assertIn("pgPrefetched.get(spineIndex)", body)
        self.assertIn("pending.catch(() => fetchChapterText(spineIndex))", body)

    def test_a_failed_early_fetch_is_forgotten_not_reported(self):
        body = _function(_code(), "prefetchChapter")
        self.assertIn("pending.catch(() => pgPrefetched.delete(spineIndex));", body)
        self.assertNotIn("showLoadWarning", body)
