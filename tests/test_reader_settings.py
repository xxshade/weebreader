"""The two text readers' display settings are ONE panel, and it floats.

Two failures this guards, both of which shipped before:

The panel used to be hand-written twice -- nine controls in
`reader/epub.html` and the same nine in `novels/reader.html`, differing
only in one <option>'s wording. reader-text.js reaches every one of them
by id and never checks whether it found anything, so a control added to
one template and forgotten in the other is a setting that exists in one
reader and silently does nothing in the other. It is a single macro now,
and this is what keeps the engine and the markup in step.

And it hangs UNDER the "Aa" button instead of sitting at the foot of the
reading area. As a strip it was #viewer's neighbour in the flex column, so
opening it made the reading area shorter under a page that had already
been laid out for the taller one -- the last lines went under the fold for
as long as the panel was open, which is the bug the panel is partly here
to stop causing.
"""
import re
import unittest
from pathlib import Path

from starlette.requests import Request

from app.library.router import templates

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "app/static/js/reader-text.js"
APP_CSS = ROOT / "app/static/src/app.css"


def _request():
    """The readers extend base.html now, and base.html reads the cookies.

    Rendering one straight from the environment skips the TemplateResponse
    that would normally put `request` in the context, so it goes in by
    hand -- with no cookies, which is the default-preferences case.
    """
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
        "headers": [], "client": ("test", 1), "server": ("test", 80),
    })


def render(name, **context):
    context.setdefault("request", _request())
    return templates.env.get_template(name).render(**context)


def epub_page():
    return render(
        "reader/epub.html",
        item={"id": 1, "title": "A Book", "series_id": 2},
        progress=None,
        next_item_id=None,
        next_item_title=None,
        replacements_json="[]",
    )


def novel_page():
    return render(
        "novels/reader.html",
        novel={"id": 1},
        chapter={"title": "One", "order_index": 0},
        saved_paragraph=None,
        replacements_json="[]",
    )


def panel_of(html):
    """The settings panel as rendered, from its own <div> to the viewer."""
    start = html.index('<div id="settings-panel"')
    return html[start:html.index('<div id="viewer"')]


class ReaderSettingsTests(unittest.TestCase):
    def setUp(self):
        self.epub = epub_page()
        self.novel = novel_page()

    # Ids the engine writes into a CHAPTER's document rather than looking
    # up in the reader's own page -- nothing for a template to render.
    ENGINE_OWN_IDS = {"reader-theme-style"}

    def test_every_control_the_engine_reaches_exists_in_both_readers(self):
        wanted = sorted(set(re.findall(r'getElementById\("([^"]+)"\)', ENGINE.read_text()))
                        - self.ENGINE_OWN_IDS)
        self.assertIn("font-size-value", wanted)  # the extractor still matches something real
        for name, html in (("epub", self.epub), ("novel", self.novel)):
            missing = [i for i in wanted if f'id="{i}"' not in html]
            self.assertEqual(missing, [], f"reader-text.js looks up ids the {name} reader does not render")

    def test_the_panel_is_the_same_markup_in_both_readers(self):
        # Apart from the one word that is genuinely different: an EPUB's
        # own typography is "the book's", a scraped chapter's is not.
        self.assertEqual(
            panel_of(self.epub).replace("Book&#39;s original", "ORIGINAL-LABEL"),
            panel_of(self.novel).replace("Original<", "ORIGINAL-LABEL<", 1),
        )

    def test_the_panel_floats_instead_of_taking_height_from_the_reading_area(self):
        rule = re.search(r"\.reader-settings\s*\{([^}]*)\}", APP_CSS.read_text())
        self.assertIsNotNone(rule, ".reader-settings has no rule in the frozen stylesheet")
        self.assertIn("position: absolute", rule.group(1))

    def test_the_panel_hangs_off_the_header_row(self):
        # Absolute against the row holding the button that opens it, which
        # only works while that row is the positioning context.
        for html in (self.epub, self.novel):
            header = html[html.index("<body"):html.index('<div id="viewer"')]
            row = header[header.rindex("<div class=", 0, header.index('id="btn-settings"')):]
            self.assertIn("relative", row.split(">")[0])
            self.assertIn('id="settings-panel"', row)


if __name__ == "__main__":
    unittest.main()
