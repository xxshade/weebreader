"""The browser does not get to restore a control's value on reload.

Firefox restores form controls on a reload, and under hx-boost the whole
app is ONE document: the state it put back was another page's. Measured in
Firefox: a novel's "Unread only" turned on and the page reloaded, then home
and a series by boosted links, then a reload of the series -- the series
came back filtered with nothing saved for it (its checkbox is the second
control on the page, like the novel's). And the novel, switched off (its
cookie deleted) and reloaded, came back filtered again. Chromium did
neither.

Every value here is decided by the server or by the page's own scripts
from what they saved, so every control opts out with autocomplete="off".
The exceptions are the sign-in and setup forms and any field that names a
credential purpose, where the browser filling in a username or password is
the point, and file inputs, which no browser restores.
"""
import re
import unittest
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app/templates"
BROWSER_FILLS = {"auth/login.html", "auth/setup.html", "auth/setup_totp.html"}


def _controls():
    for template in sorted(TEMPLATES.rglob("*.html")):
        page = str(template.relative_to(TEMPLATES))
        if page in BROWSER_FILLS:
            continue
        text = re.sub(r"\{#.*?#\}|<!--.*?-->", "", template.read_text(), flags=re.S)
        for match in re.finditer(r"<(input|select|textarea)\b([^>]*)>", text, re.S):
            attrs = match.group(2)
            if re.search(r'type="(hidden|file)"', attrs):
                continue
            yield page, match.group(0)


class NoControlIsRestoredByTheBrowser(unittest.TestCase):
    def test_the_scan_finds_the_ones_that_were_restored(self):
        # The control: the two checkboxes measured coming back wrong.
        found = " ".join(tag for _, tag in _controls())
        self.assertIn('id="item-unread-only"', found)
        self.assertIn('id="chapter-unread-only"', found)

    def test_every_control_opts_out(self):
        # A credential field names its purpose instead (the trackers'
        # sign-in on /settings), which is for a password manager to fill.
        credential = re.compile(r'autocomplete="(username|current-password|new-password|one-time-code)"')
        for page, tag in _controls():
            with self.subTest(page=page, control=" ".join(tag.split())[:80]):
                self.assertTrue('autocomplete="off"' in tag or credential.search(tag), tag)


if __name__ == "__main__":
    unittest.main()
