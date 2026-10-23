"""The two copies of format_duration agree, value for value.

Python renders a card's duration and library-events.js re-renders it when
the card is refreshed live; when they disagreed (round half to even
against Math.round's half up) a card changed its wording with nothing
having changed -- 150s was "2m" in one and "3m" in the other. AGENTS.md
promised a test comparing them; this is it, run through node.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from app.template_filters import format_duration

EVENTS_JS = Path(__file__).resolve().parent.parent / "app/static/js/library-events.js"
VALUES = [0, 0.4, 0.5, 1, 29.5, 59, 59.4, 59.6, 60, 89, 90, 149, 150, 151, 3570, 3599, 3600,
          5430, 5429, 7170, 86400] + list(range(0, 14400, 7))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DurationParity(unittest.TestCase):
    def test_python_and_javascript_render_the_same(self):
        source = EVENTS_JS.read_text()
        body = re.search(r"\n    function formatDuration\(seconds\) \{.*?\n    \}\n", source, re.S).group(0)
        script = body + f"\nconsole.log(JSON.stringify({json.dumps(VALUES)}.map(formatDuration)));"
        js = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                       check=True).stdout)
        py = [format_duration(v) for v in VALUES]
        mismatches = [(v, p, j) for v, p, j in zip(VALUES, py, js) if p != j]
        self.assertEqual(mismatches, [])
