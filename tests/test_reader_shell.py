"""The readers extend base.html, and what that must NOT have changed.

They used to be three standalone documents, each repeating the doctype,
the icon, the manifest, the stylesheet and three `<script>` tags -- with a
comment in all three saying "the readers do not extend base.html, so they
load these themselves". Extending it removes that, and puts the readers on
the same `<html>` the rest of the app renders from cookies.

What extending it must not do is make a reader look like a library page.
A reader is the page, edge to edge, and four things say so, each of
which a later edit could drop without anything failing:

1. `viewport-fit=cover`, without which a phone letterboxes the page
   instead of painting into the safe areas.
2. A body that OWNS the viewport and does not scroll. This one is NOT a
   block: it is `body:has(#reader-root)` in theme.css, so that it lives
   inside the markup a swap replaces rather than on an attribute a swap
   leaves behind (measured under hx-boost: #reader-root came out 0px tall
   in the comic reader and 37px in the novel one, because `<main
   class="h-full">` is a percentage height against a body left at
   `min-h-full`; 800px of 800px with the rule).
3. No header, no user menu.
4. No live-update stream. There is no card on a reader page for an event
   to update, and the connection is not free: one open HTTP request per
   tab, and a reader tab is the one most likely to sit open for hours.

Driven in a browser on all three, at 420x800 and 1200x900: viewport,
absent header and stream, `#reader-root` at 800px of 800px, no scroll, no
duplicate scripts, no JS errors. A reader opened in its own tab made 0
requests to /library/events in 5s, against 1 the moment it navigated to
the home page. Moving the shape into CSS is pixel-identical on all three
at both sizes (the only differing pixels are the clock's, which differ
between two runs of the unchanged page too).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app/templates"
BASE = TEMPLATES / "base.html"
THEME_CSS = ROOT / "app/static/src/theme.css"
READERS = ("reader/comic.html", "reader/epub.html", "novels/reader.html")


def _source(name: str) -> str:
    return (TEMPLATES / name).read_text()


def _markup(name: str) -> str:
    """The template with its comments blanked out.

    Every scan below is for MARKUP, and the comments here explain the
    rules by naming what they forbid -- `<body>`, `#reader-root`, the
    scripts the base already loads. Two of these tests failed on their own
    documentation before this existed. Jinja `{# #}` and HTML `<!-- -->`
    hide the same way; same treatment, same reason, as test_ui_prefs.py.
    """
    source = re.sub(r"\{#.*?#\}", "", _source(name), flags=re.S)
    return re.sub(r"<!--.*?-->", "", source, flags=re.S)


def _css() -> str:
    """theme.css with its comments blanked out.

    A scan for the RULE, and the comment above it explains the rule by
    quoting it -- `overflow: hidden`, 100dvh and `min-h-full` all appear
    in the prose. Same treatment, same reason, as tests/test_ui_prefs.py.
    """
    return re.sub(r"/\*.*?\*/", "", THEME_CSS.read_text(), flags=re.S)


def _declarations(css: str, selector: str) -> "dict[str, str] | None":
    """One rule as {property: value}, or None when the selector is absent.

    Parsed rather than searched as text: `height: 100dvh` is a SUBSTRING
    of `min-height: 100dvh`, and a mutation that changed the height to
    100% went unnoticed while the min-height carried the assertion.
    """
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    if not match:
        return None
    return {d.split(":", 1)[0].strip(): d.split(":", 1)[1].strip()
            for d in match.group(1).split(";") if ":" in d}


def _block(source: str, name: str) -> "str | None":
    """The body of `{% block <name> %}...{% endblock %}`, or None if absent.

    None and "" are DIFFERENT and the difference is the whole point: a
    reader that overrides `chrome` to nothing has no header, and a reader
    that simply stops overriding it inherits base.html's. Returning "" for
    both let two mutations through -- deleting the override outright, which
    is exactly how this would be lost in practice.
    """
    match = re.search(r"\{%-?\s*block\s+" + name + r"\s*%\}(.*?)\{%-?\s*endblock\s*%\}",
                      source, re.S)
    return match.group(1) if match else None


def _overridden_to_nothing(test, name: str, block: str) -> None:
    body = _block(_source(name), block)
    test.assertIsNotNone(body, f"{name} does not override {block}, so it inherits base.html's")
    test.assertEqual(body.strip(), "", f"{name} renders something for {block}")


class EveryReaderExtendsTheBase(unittest.TestCase):
    def test_all_three_do(self):
        for name in READERS:
            self.assertTrue(_source(name).lstrip().startswith("{% extends \"base.html\" %}")
                            or '{% extends "base.html" %}' in _source(name).split("\n")[1],
                            f"{name} does not extend base.html")

    def test_none_of_them_still_writes_its_own_document(self):
        # The duplication this removed: doctype, <html>, <head>, the icon,
        # the manifest and the stylesheet, three times over.
        for name in READERS:
            markup = _markup(name)
            for tag in ("<!DOCTYPE html>", "<html", "<head>", "<body"):
                self.assertNotIn(tag, markup, f"{name} still writes {tag}")

    def test_none_reloads_what_the_base_already_loads(self):
        # The three the base has in <head>. Loading them again would run
        # each one twice, which is what the old comment in these files was
        # apologising for.
        already = ("js/fullscreen.js", "js/range-value.js", "js/app-fullscreen.js",
                   "js/ui-prefs.js")
        for name in READERS:
            markup = _markup(name)
            for script in already:
                self.assertNotIn(script, markup, f"{name} loads {script}, which base.html already does")

    def test_the_base_really_is_where_those_come_from(self):
        # The negative control for the test above: it passes trivially if
        # base.html stopped loading them too.
        base = BASE.read_text()
        for script in ("js/fullscreen.js", "js/range-value.js", "js/app-fullscreen.js",
                       "js/ui-prefs.js"):
            self.assertIn(script, base, f"base.html no longer loads {script}")


class AReaderIsThePageEdgeToEdge(unittest.TestCase):
    def test_each_one_asks_for_the_safe_areas(self):
        # Silent on a desktop, and a letterboxed page on a notched phone.
        for name in READERS:
            viewport = _block(_source(name), "viewport_content")
            self.assertIsNotNone(viewport, f"{name} does not override viewport_content")
            self.assertIn("viewport-fit=cover", viewport, f"{name} lost viewport-fit=cover")

    def test_the_base_does_not_ask_for_them(self):
        # The other half of the rule: the library pages must NOT have it,
        # or the sticky header slides under the notch.
        self.assertNotIn("viewport-fit", _block(BASE.read_text(), "viewport_content"))

    def test_the_viewport_is_one_string_rendered_twice(self):
        # <head> is what a <body> swap leaves behind, so the value is also
        # put on <main> and reconciled from there. Two literals would be
        # the drift this block exists to prevent -- and the copy that went
        # stale would be the one nothing on a desktop can see.
        base = BASE.read_text()
        self.assertIn('content="{{ viewport_content }}"', base,
                      "the <head> meta no longer comes from the block")
        self.assertIn('data-viewport="{{ viewport_content }}"', base,
                      "<main> no longer carries what <head> should say")
        self.assertIn('meta.content = wanted', base,
                      "nothing reconciles <head> with the page that arrived")

    def test_the_body_owns_the_viewport_wherever_a_reader_root_is(self):
        # The rule, not a class: a class on <body> is an ATTRIBUTE, and the
        # one swap that would carry a reader in replaces <body>'s children
        # and leaves its attributes. 100dvh twice over because a
        # min-height clamps a height and the base body is `min-h-full`.
        rule = _declarations(_css(), "body:has(#reader-root)")
        self.assertIsNotNone(rule, "theme.css no longer gives a reader page its shape")
        self.assertEqual(rule.get("height"), "100dvh", "the reader body does not fill the viewport")
        self.assertEqual(rule.get("min-height"), "100dvh", "`min-h-full` still wins over it")
        self.assertEqual(rule.get("overflow"), "hidden", "a reader page can scroll")

    def test_the_stripper_is_not_what_makes_that_pass(self):
        # Negative control. The comment above the rule quotes the rule, so
        # a scan of the raw file passes on the prose alone -- and a
        # stripper that ate too much would pass on nothing at all.
        self.assertIn("overflow: hidden", THEME_CSS.read_text())
        self.assertIn("body:has(#reader-root)", _css())
        self.assertNotIn("hx-boost", _css(), "the prose survived the stripper")

    def test_every_reader_carries_the_hook_that_rule_needs(self):
        # Renaming #reader-root in one template would silently give that
        # one reader a library page's shape. It is already the JS hook in
        # all three, which is why the rule is keyed off it.
        for name in READERS:
            self.assertIn('id="reader-root"', _markup(name), f"{name} has no #reader-root")

    def test_nothing_else_claims_to_be_a_reader(self):
        # The other half: the rule is scoped by that id alone, so a page
        # that borrows it takes the reader's no-scroll shape with it.
        for path in TEMPLATES.rglob("*.html"):
            name = str(path.relative_to(TEMPLATES))
            if name in READERS:
                continue
            self.assertNotIn('id="reader-root"', _markup(name),
                             f"{name} is not a reader but carries #reader-root")

    def test_none_of_them_sets_a_body_class_of_its_own(self):
        # What the rule above replaced. An override coming back would work
        # on a full load and be the same trap again on a swap.
        for name in READERS:
            self.assertIsNone(_block(_markup(name), "body_class"),
                              f"{name} is back to shaping the page from <body>'s attributes")

    def test_the_base_body_is_the_opposite(self):
        # Control: a library page grows with its content and scrolls.
        base_body = _block(BASE.read_text(), "body_class")
        self.assertIn("min-h-full", base_body)
        self.assertNotIn("overflow-hidden", base_body)

    def test_the_comic_readers_black_is_on_the_element_that_shows_it(self):
        # It used to be the body's. #reader-root covers the viewport and is
        # inside the swap; the other two readers paint their own surface
        # from the reader theme (reader-text.js), so only this one needs it.
        root = _markup("reader/comic.html").split('id="reader-root"', 1)[1].split(">", 1)[0]
        self.assertIn("bg-black", root, "a comic page is no longer letterboxed against black")

    def test_no_reader_carries_the_chrome(self):
        for name in READERS:
            _overridden_to_nothing(self, name, "chrome")

    def test_no_reader_opens_the_live_update_stream(self):
        # The one with a running cost. Measured: 0 requests to
        # /library/events in 5s on a reader, 1 on reaching the home page.
        for name in READERS:
            _overridden_to_nothing(self, name, "live_updates")

    def test_the_base_still_has_both(self):
        # Control for the two above: they pass on an empty base too.
        base = BASE.read_text()
        self.assertIn('id="app-header"', _block(base, "chrome"))
        self.assertIn('hx-sse:connect="/library/events?since=', _block(base, "live_updates"))

    def test_the_reading_area_is_given_the_full_height(self):
        # <main> is the base's, and its p-4 would box a comic page in.
        for name in READERS:
            main_attrs = _block(_source(name), "main_attrs")
            self.assertIsNotNone(main_attrs, f"{name} does not override main_attrs")
            self.assertIn('class="h-full"', main_attrs,
                          f"{name} does not hand the full height to its reader")
        self.assertIn("p-4", _block(BASE.read_text(), "main_attrs"))


if __name__ == "__main__":
    unittest.main()
