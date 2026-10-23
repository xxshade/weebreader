"""The number beside a slider (range-value.js) after a boosted visit.

A boosted visit swaps the page without loading one, so range-value.js's
load-time pass never runs for it: the thumbnail slider showed its number on
a full load of home or a series and none at all on either reached by a link
(measured in Chromium and Firefox). The swap listener fixes that for a
slider whose value is in the markup -- but it fires before the swapped-in
scripts run, so the thumbnail slider, whose saved value thumbnail-size.js
sets, read the markup's midpoint (260px beside a slider at 180) until that
script asked for a redraw after setting it.

These pin both halves in the source; the behaviour itself is measured in the
browsers (see CLAUDE.md).
"""
import re
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "app/static/js"


def _code(name: str) -> str:
    """The file with its comments removed, so a rule is found only where it
    is actually written, not where a comment explains it."""
    source = (JS / name).read_text()
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


class ReadoutAfterASwap(unittest.TestCase):
    def test_range_value_redraws_after_every_swap(self):
        self.assertRegex(_code("range-value.js"),
                         r'addEventListener\(\s*"htmx:after:process"\s*,\s*refresh\s*\)')

    def test_thumbnail_size_redraws_after_setting_the_saved_value(self):
        code = _code("thumbnail-size.js")
        set_at = code.index("slider.value = saved;")
        refresh_at = code.index("window.rangeValues?.refresh()")
        self.assertLess(set_at, refresh_at)
