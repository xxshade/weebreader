"""The item card's author/description bubble is as tall as its text.

Pinned to both edges of the card (`bottom: 0`), an item with only an author
showed one line in a panel the whole height of the cover. Its height now
follows the text, with the card's bottom edge as the ceiling. Measured in
both browsers: 35px for an author on a 250px card, and a long description
still ends inside the card and scrolls.
"""
import re
import unittest
from pathlib import Path

THEME = Path(__file__).resolve().parents[1] / "app/static/src/theme.css"


class BubbleHeight(unittest.TestCase):
    def rule(self):
        css = re.sub(r"/\*.*?\*/", "", THEME.read_text(), flags=re.S)
        return re.search(r"\.card-overlay \.item-about\s*\{([^}]*)\}", css).group(1)

    def test_capped_by_the_card_not_stretched_to_it(self):
        rule = self.rule()
        self.assertIn("max-height: calc(100% - 1.75rem);", rule)
        self.assertNotRegex(rule, r"(?m)^\s*bottom\s*:")
        self.assertIn("overflow-y: auto;", rule)
