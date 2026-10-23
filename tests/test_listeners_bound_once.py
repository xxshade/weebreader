"""A script a boosted visit runs again binds its global listeners once.

A boosted visit swaps <body> and runs every script in it again, in the SAME
document -- so a listener added to `document` or `window` by a script in
the body is added again on every visit, and never removed. Two things went
wrong because of it, both measured in Chromium and Firefox:

* delegated toggles undid themselves. The settings page's "?" bubbles
  opened on every OTHER visit, the statistics page's "Custom" range stopped
  opening after one round trip, and "Show more" on a series did the same:
  two copies of one click listener, the first opening and the second
  closing;
* the popovers bound to elements kept the old page alive. Each copy held
  the previous page's button and panel, seventeen copies of the user
  menu's pair after four laps of the app.

So every such script checks a `window.<name>` flag before its first global
listener, and reads its elements when an event arrives rather than
holding them. Scripts in <head> are not run again by a body swap and need
no flag, which is why this looks only at what the body loads.
"""
import re
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app/templates"
JS = ROOT / "app/static/js"

GLOBAL_LISTENER = re.compile(r"\b(?:document|window|document\.body)\.addEventListener\(")
GUARD = re.compile(r"if \(!?window\.\w+\)")


def _body_scripts() -> set[str]:
    """Every js/ file a template loads from inside <body>."""
    found = set()
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text()
        if template.name == "base.html":
            text = re.split(r"^<body", text, maxsplit=1, flags=re.M)[1]
        tags = '\n'.join(re.findall(r'<script\b[^>]*\bsrc\s*=[^>]*>', text, flags=re.I))
        found.update(re.findall(r"asset_url\('js/([\w.-]+\.js)'\)", tags))
    return found


def _inline_body_blocks() -> dict[str, list[str]]:
    """Every inline <script> in what a template puts in <body>, by template.

    The same rule applies to them: a boosted visit re-runs an inline block
    exactly as it re-runs a file. The "Saved" note on the series and novel
    pages each added a <body> listener per visit, holding that page's
    indicator -- and with it the page."""
    found = {}
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text()
        if template.name == "base.html":
            text = re.split(r"^<body", text, maxsplit=1, flags=re.M)[1]
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S)
        if blocks:
            found[str(template.relative_to(TEMPLATES))] = blocks
    return found


class BodyScriptsBindOnce(unittest.TestCase):
    def test_asset_data_attributes_are_not_executed_body_scripts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'base.html').write_text('''<head><script src="{{ asset_url('js/head.js') }}"></script></head>
<body><main data-reading-tracker-url="{{ asset_url('js/reading-time.js') }}"></main>
<script defer src="{{ asset_url('js/tooltip.js') }}"></script>''')
            with patch(__name__+'.TEMPLATES',root):
                self.assertEqual(_body_scripts(),{'tooltip.js'})

    def test_the_scan_finds_the_scripts_that_broke(self):
        # The control: these are the files whose second copy was measured
        # misbehaving, so a scan that cannot see them proves nothing.
        scripts = _body_scripts()
        for name in ("tooltip.js", "stats-range.js", "read-more.js", "user-menu.js"):
            with self.subTest(script=name):
                self.assertIn(name, scripts)

    def test_every_global_listener_sits_behind_a_guard(self):
        for name in sorted(_body_scripts()):
            source = (JS / name).read_text()
            first = GLOBAL_LISTENER.search(source)
            if first is None:
                continue
            with self.subTest(script=name):
                guard = GUARD.search(source)
                self.assertIsNotNone(guard, f"{name} binds to document/window with no guard")
                self.assertLess(guard.start(), first.start(),
                                f"{name}'s guard comes after its first global listener")

    def test_the_inline_scan_finds_the_blocks_that_leaked(self):
        # The control: the two "Saved" blocks bind to <body>, so a scan
        # that cannot see them proves nothing.
        blocks = _inline_body_blocks()
        for page, event in (("library/series.html", "series-edit-saved"),
                            ("novels/detail.html", "novel-edit-saved")):
            with self.subTest(page=page):
                self.assertTrue(any(f'addEventListener("{event}"' in b for b in blocks[page]))

    def test_every_inline_global_listener_sits_behind_a_guard(self):
        for page, blocks in sorted(_inline_body_blocks().items()):
            for block in blocks:
                first = GLOBAL_LISTENER.search(block)
                if first is None:
                    continue
                with self.subTest(page=page, listener=block[first.start():first.start() + 60]):
                    guard = GUARD.search(block)
                    self.assertIsNotNone(guard, f"an inline block in {page} binds with no guard")
                    self.assertLess(guard.start(), first.start())

    def test_the_popovers_hold_no_element_between_events(self):
        # Looked up inside the listener, so the page a boosted visit brings
        # is the one they act on.
        for name in ("filters-panel.js", "thumbnail-size.js", "user-menu.js"):
            with self.subTest(script=name):
                source = (JS / name).read_text()
                self.assertIn("const parts = () => ({", source)
                self.assertNotRegex(source, r"\n    const (btn|popover|dropdown|control|menu) = document")


if __name__ == "__main__":
    unittest.main()
