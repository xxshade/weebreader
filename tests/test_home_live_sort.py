"""A series card updated live on the home page takes its place in the sort.

A new volume in an existing series refetches the series' card and swaps it
in (libraryGrid.replaceCard in home.js). The fresh card carries the values
the sorts read -- data-modified above all -- but the swap only re-applied
the filters, so under "Date modified" the series kept its old position,
with the newer date already on its card, until a reload. Measured in both
browsers; the live behaviour is measured there (see CLAUDE.md), this pins
the rule in the source.
"""
import re
import unittest
from pathlib import Path

HOME_JS = Path(__file__).resolve().parents[1] / "app/static/js/home.js"


def _code() -> str:
    source = HOME_JS.read_text()
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


class ReplaceCardSorts(unittest.TestCase):
    def test_replace_card_sorts_then_filters(self):
        body = re.search(r"replaceCard\(kind, id, cardEl\) \{(.*?)\n        \},", _code(), re.S).group(1)
        self.assertIn("applySort();", body)
        self.assertLess(body.index("applySort();"), body.index("applyFilters();"))
