"""A book's own scripts never run with the app's origin.

A srcdoc iframe with no `sandbox` attribute IS the app's origin: measured,
an EPUB's <script> fetched /library/job-status with the reader's session
(200) in Continuous scroll, and an `<img onerror>` ran in both modes, in
Chromium and Firefox. Every chapter iframe now comes from one factory that
sandboxes it; this pins that nothing creates one any other way, and that
the sandbox never grows `allow-scripts`.
"""
import re
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "app/static/js"


class ChapterIframesAreSandboxed(unittest.TestCase):
    def test_one_factory_makes_every_chapter_iframe(self):
        source = (JS / "reader-text.js").read_text()
        self.assertEqual(source.count('document.createElement("iframe")'), 1)
        factory = re.search(r"function createChapterIframe\(\) \{(.*?)\n    \}", source, re.S).group(1)
        self.assertIn('document.createElement("iframe")', factory)
        sandbox = re.search(r'setAttribute\("sandbox", "([^"]*)"\)', factory).group(1).split()
        self.assertIn("allow-same-origin", sandbox)
        self.assertNotIn("allow-scripts", sandbox)

    def test_no_other_script_builds_a_content_iframe(self):
        for path in JS.glob("*.js"):
            if path.name == "reader-text.js":
                continue
            self.assertNotIn('createElement("iframe")', path.read_text(), path.name)
