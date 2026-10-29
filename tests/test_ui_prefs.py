"""The three display preferences, now that the SERVER decides them.

Each one has to be on `<html>` before the first paint. They used to be
localStorage plus an inline `<script>` in `<head>` racing the parser; they
are cookies now, so the server renders the attribute in the first byte and
no script is involved.

Two things are worth pinning and neither is the happy path:

1. **The values are attacker-controlled.** They are whatever the request's
   own cookie says, and they land in an HTML attribute and a CSS custom
   property. A cookie of `1px; background: url(...)` must not paint itself
   into every page.
2. **Two sides have to agree.** ui_prefs.py reads the cookie names;
   static/js/ui-prefs.js writes them; base.html renders from them. If any
   pair drifts, nothing raises -- the preference simply stops being
   remembered, which is exactly the failure this change was meant to end.
3. **The name the browser SENDS is not the name either side spells.** A
   cookie name is an RFC 6265 token, so `thumbnail-size:series` has to
   travel percent-encoded. Agreeing on the literal string is not enough
   and looked like enough for two releases.
"""
import re
import unittest
from pathlib import Path
from urllib.parse import quote

from app import ui_prefs

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app/templates"
BASE_HTML = ROOT / "app/templates/base.html"
THEME_CSS = ROOT / "app/static/src/theme.css"
NOVEL_HTML = ROOT / "app/templates/novels/detail.html"
JS = ROOT / "app/static/js/ui-prefs.js"
JS_DIR = ROOT / "app/static/js"


def _markup_of(path: Path) -> str:
    """The template with its comments blanked out.

    This is a scan for CODE, and the comments here explain why localStorage
    is gone by naming it -- prose that reads as a breach of the rule it is
    documenting. Jinja `{# #}`, HTML `<!-- -->` and JS `//` all hide the
    same way. Same treatment, same reason, as tests/test_events.py.
    """
    source = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join("" if line.lstrip().startswith("//") else line
                      for line in source.splitlines())


class _Request:
    """Just enough of a request to carry cookies."""

    def __init__(self, **cookies):
        self.cookies = cookies


def _as_a_browser_sends(**prefs):
    """A request carrying the Cookie header a real browser would send.

    ui-prefs.js writes `encodeURIComponent(name)=encodeURIComponent(value)`
    -- it has no choice, a name is a token and `:` is a separator -- and
    Starlette decodes neither half. So THIS is the shape ui_prefs.py is
    handed, and the literal-name requests above are a shape no browser
    produces for a key with a `:` in it.
    """
    return _Request(**{quote(name, safe=""): quote(str(value), safe="")
                       for name, value in prefs.items()})


class TheThemeIsValidatedNotEchoed(unittest.TestCase):
    def test_a_real_choice_comes_back(self):
        self.assertEqual(ui_prefs.theme(_Request(**{"app-theme": "amoled"})), "amoled")

    def test_legacy_missing_and_hostile_cookies_use_amoled(self):
        for value in ('dark', 'light', 'amoled', 'x" onload="alert(1)', '', 'AMOLED'):
            self.assertEqual(ui_prefs.theme(_Request(**{"app-theme": value})), "amoled")
        self.assertEqual(ui_prefs.theme(_Request()), "amoled")

    def test_theme_controls_are_removed(self):
        self.assertNotIn('id="app-theme-select"', (ROOT / "app/templates/settings.html").read_text())
        self.assertNotIn('id="theme-select"', (ROOT / "app/templates/reader/_settings_panel.html").read_text())


class TheThumbnailSizeIsValidatedNotEchoed(unittest.TestCase):
    KEY = "thumbnail-size:series"

    def test_the_pages_own_size_wins(self):
        request = _Request(**{"thumbnail-size": "300", self.KEY: "200"})
        self.assertEqual(ui_prefs.thumb_size(request, self.KEY), 200)

    def test_a_page_never_adjusted_inherits_the_home_size(self):
        # Not the default: a library sized up once still opens its series
        # at that size. thumbnail-size.js reads the same two in the same
        # order and this has to agree with it.
        request = _Request(**{"thumbnail-size": "300"})
        self.assertEqual(ui_prefs.thumb_size(request, self.KEY), 300)

    def test_no_cookie_leaves_the_stylesheet_alone(self):
        self.assertIsNone(ui_prefs.thumb_size(_Request(), self.KEY))

    def test_css_cannot_be_smuggled_through_it(self):
        # THE case. This value is interpolated into a style attribute.
        # " 300" is NOT here: int() tolerates the space and the result is
        # still an int this re-renders itself, so nothing can ride along.
        for hostile in ("1px; background: url(https://evil/x)", "100px", "0", "-40",
                        "1e3", "300px", "999999999999999999999"):
            self.assertIsNone(ui_prefs.thumb_size(_Request(**{"thumbnail-size": hostile}), self.KEY),
                              f"{hostile!r} came back")

    def test_the_bounds_are_the_sliders_own(self):
        control = (ROOT / "app/templates/_thumb_size_control.html").read_text()
        slider = control.split('id="thumb-size-slider"', 1)[1].split(">", 1)[0]
        self.assertIn(f'min="{ui_prefs.THUMB_MIN}"', slider)
        self.assertIn(f'max="{ui_prefs.THUMB_MAX}"', slider)

    def test_both_ends_are_accepted(self):
        for px in (ui_prefs.THUMB_MIN, ui_prefs.THUMB_MAX):
            request = _Request(**{"thumbnail-size": str(px)})
            self.assertEqual(ui_prefs.thumb_size(request, "thumbnail-size"), px)


class TheNameThatTravelsIsEncoded(unittest.TestCase):
    """The bug this class exists for, and it was silent on one page only.

    `thumbnail-size:series` never reached the server: it arrives as
    `thumbnail-size%3Aseries`, the lookup was by the literal string, and
    the miss fell through to the home page's key -- which is the documented
    behaviour for a page never adjusted, so nothing looked wrong. Measured
    on /series/1 with 300px saved there and 260px on the home page:
    `<main>` came back with no `style` at all, and the card went
    260px -> 300px when the deferred script caught up (thumbnail-size.js
    delayed 400ms; the home page, whose key has nothing to escape,
    measured 260px -> 260px in the same run). After: 300px in the first
    byte and 300px -> 300px.
    """

    KEY = "thumbnail-size:series"

    def test_the_series_key_arrives_escaped_and_is_still_found(self):
        request = _as_a_browser_sends(**{self.KEY: 300})
        self.assertEqual(ui_prefs.thumb_size(request, self.KEY), 300)

    def test_that_request_really_is_the_awkward_shape(self):
        # Control. The test above passes for free if the helper hands back
        # the literal name -- which is exactly what every other test here
        # does, and why this went unseen.
        sent = list(_as_a_browser_sends(**{self.KEY: 300}).cookies)
        self.assertEqual(sent, ["thumbnail-size%3Aseries"])
        self.assertNotIn(self.KEY, sent)

    def test_every_key_a_page_can_pick_survives_the_trip(self):
        # Forward-looking: the next page to want its own size picks a key
        # in a template, and whether it works depends on characters. Each
        # one is asked for through the encoder a browser actually applies.
        keys = {"thumbnail-size"}
        for path in TEMPLATES.rglob("*.html"):
            keys.update(re.findall(r"\{%-?\s*block\s+thumb_size_key\s*%\}(.*?)\{%-?\s*endblock",
                                   _markup_of(path), re.S))
        self.assertIn(self.KEY, keys, "the scan found no page key; it is anchored wrong")
        for key in sorted(k.strip() for k in keys):
            self.assertEqual(ui_prefs.thumb_size(_as_a_browser_sends(**{key: 210}), key), 210,
                             f"a page saving under {key!r} would not be read back")

    def test_the_home_key_never_needed_this(self):
        # The other half of why it was silent: the fallback key has no
        # escapable character, so that page was always right.
        self.assertEqual(quote("thumbnail-size", safe=""), "thumbnail-size")

    def test_the_other_two_still_come_through(self):
        # Control for the helper being in the read path of all three: a
        # plain name must cost nothing.
        self.assertEqual(ui_prefs.theme(_as_a_browser_sends(**{"app-theme": "amoled"})), "amoled")
        self.assertTrue(ui_prefs.unread_only(
            _as_a_browser_sends(**{"novel-chapters-unread-only:5": "1"}), 5))

    def test_the_value_is_decoded_too(self):
        # The writer encodes both halves, so the reader decodes both. No
        # value here holds an escapable character today; this is what says
        # the symmetry is deliberate rather than an accident of the data.
        self.assertEqual(ui_prefs.thumb_size(_Request(**{"thumbnail-size": "%33%30%30"}),
                                             "thumbnail-size"), 300)

    def test_decoding_does_not_smuggle_anything_past_the_validator(self):
        # THE case, now that there is a decode between the header and the
        # check: the value lands in a style attribute, and an escaped
        # payload must fail the same way the plain one does.
        hostile = "1px%3B%20background%3A%20url(https%3A%2F%2Fevil%2Fx)"
        self.assertIsNone(ui_prefs.thumb_size(_Request(**{"thumbnail-size": hostile}),
                                              "thumbnail-size"))
        # And the control for the decode being what it had to get past: the
        # same payload unescaped is refused by the same line.
        self.assertIsNone(ui_prefs.thumb_size(
            _Request(**{"thumbnail-size": "1px; background: url(https://evil/x)"}),
            "thumbnail-size"))


class TheUnreadFilterIsOnlyEverTheOneValue(unittest.TestCase):
    def test_on(self):
        self.assertTrue(ui_prefs.unread_only(_Request(**{"novel-chapters-unread-only:5": "1"}), 5))

    def test_off(self):
        # novel-chapters.js deletes the cookie to turn it off; a "0" left
        # by an older copy of it still reads as off.
        self.assertFalse(ui_prefs.unread_only(_Request(**{"novel-chapters-unread-only:5": "0"}), 5))

    def test_never_asked(self):
        self.assertFalse(ui_prefs.unread_only(_Request(), 5))

    def test_anything_else_is_off(self):
        for value in ("true", "yes", "1 ", ""):
            self.assertFalse(ui_prefs.unread_only(
                _Request(**{"novel-chapters-unread-only:5": value}), 5))


class TheUnreadFilterIsPerNovel(unittest.TestCase):
    """It was one cookie for every novel: turned on for one, it opened
    all the others narrowed to what was unread."""

    def test_another_novels_cookie_does_not_filter_this_one(self):
        self.assertFalse(ui_prefs.unread_only(
            _as_a_browser_sends(**{"novel-chapters-unread-only:7": "1"}), 5))

    def test_the_old_cookie_for_every_novel_filters_none(self):
        self.assertFalse(ui_prefs.unread_only(_Request(**{"novel-chapters-unread-only": "1"}), 5))

    def test_a_novel_whose_id_prefixes_another_is_its_own(self):
        self.assertFalse(ui_prefs.unread_only(
            _as_a_browser_sends(**{"novel-chapters-unread-only:15": "1"}), 1))

    def test_the_page_carries_its_novel_for_the_writer(self):
        html = NOVEL_HTML.read_text()
        self.assertIn('data-novel-id="{{ novel.id }}"', html)
        js = (JS_DIR / "novel-chapters.js").read_text()
        self.assertIn("window.uiPrefs.unreadKey(novelId)", js)
        self.assertIn("window.uiPrefs.remove(key)", js)
        self.assertIn("return `${UNREAD}:${novelId}`;", JS.read_text())

    def test_the_old_cookie_is_deleted_not_carried(self):
        source = JS.read_text()
        self.assertIn("if (read(UNREAD) !== null) remove(UNREAD);", source)
        self.assertIn("new Set([HOME_THUMB, pageThumb])", source)


class TheTwoSidesAgree(unittest.TestCase):
    """The drift that would turn this off without failing anything."""

    def test_the_writer_uses_the_names_the_reader_reads(self):
        source = JS.read_text()
        for name in (ui_prefs.THEME_COOKIE, ui_prefs.THUMB_COOKIE, ui_prefs.UNREAD_COOKIE):
            self.assertIn(f'"{name}"', source, f"ui-prefs.js never names {name}")

    def test_the_owners_persist_through_the_one_writer(self):
        # Each owner used to call localStorage directly. Going through
        # window.uiPrefs is what puts the value somewhere the server reads.
        for name in ("thumbnail-size.js", "novel-chapters.js"):
            source = (JS_DIR / name).read_text()
            self.assertIn("window.uiPrefs.write(", source, f"{name} never persists a preference")

    def test_the_writer_is_loaded_before_anything_that_calls_it(self):
        """Deferred scripts run in document order, so the file that defines
        window.uiPrefs has to come first or the owners throw.

        Matched on the whole <script> tag, in comment-blanked markup: the
        bare filename appears in a comment 6000 characters earlier, and an
        earlier version of this test compared THAT against the real tag and
        passed with the order reversed.
        """
        html = _markup_of(BASE_HTML)
        writer = html.index("""<script src="{{ asset_url('js/ui-prefs.js') }}" defer></script>""")
        first_caller = html.index("""<script src="{{ asset_url('js/fullscreen.js') }}" defer></script>""")
        self.assertLess(writer, first_caller)

    def test_the_template_renders_all_three(self):
        html = BASE_HTML.read_text()
        self.assertIn("ui_theme(request)", html)
        self.assertIn("ui_thumb_size(request, thumb_key)", html)
        self.assertIn("ui_unread_only(request, novel.id)", NOVEL_HTML.read_text())

    def test_every_jinja_environment_has_them(self):
        # base.html calls ui_theme() on EVERY page, so a router that builds
        # its own Jinja2Templates and forgets this raises at render time.
        missing = []
        for path in (ROOT / "app").rglob("*.py"):
            source = path.read_text()
            if "Jinja2Templates(" not in source:
                continue
            if "register_asset_versioning(templates)" not in source:
                continue        # not an env that renders base.html
            code = "\n".join(line for line in source.splitlines()
                             if not line.lstrip().startswith("#"))
            if "register_ui_prefs(templates)" not in code:
                missing.append(path.name)
        self.assertEqual(missing, [], "these envs render base.html without the prefs globals")
        # The control: commenting the call out has to read as missing, or
        # this scan passes on the line that is no longer running.
        self.assertNotIn("register_ui_prefs", "\n".join(
            line for line in "# register_ui_prefs(templates)".splitlines()
            if not line.lstrip().startswith("#")))


class WhatIsPerPageLivesInsideTheSwap(unittest.TestCase):
    """WHERE the server renders each of the three, and why it differs.

    The theme is the BROWSER's choice and is the same on every page, so it
    belongs on `<html>`; a navigation that leaves `<html>` alone is right
    about it. The other two are the PAGE's: which cookie this page's
    thumbnail size is saved under, and whether this chapter list opens
    filtered. They sit on `<main>`, which is inside what a whole-`<body>`
    swap replaces.

    Measured before the move, with `hx-boost:inherited="true"` on `<body>`,
    boosting home -> /series/1: the series grid rendered at the home page's
    **140px** under the key `thumbnail-size`, against **300px** and
    `thumbnail-size:series` on a full load of the same URL. After: 300px
    and the right key either way, and the novel page's filter 9 rows of 12
    both ways -- still at the FIRST animation frame, which is the whole
    reason these are server-rendered at all.
    """

    def html_tag(self):
        markup = _markup_of(BASE_HTML)
        return markup[markup.index("<html"):markup.index(">", markup.index("<html")) + 1]

    def main_tag(self):
        # Nothing inside the open tag contains a `>` -- the Jinja in there
        # is `{% ... %}` -- so the first one closes it.
        markup = _markup_of(BASE_HTML)
        start = markup.index("<main")
        return markup[start:markup.index(">", start) + 1]

    def test_the_html_tag_carries_the_browsers_choice_and_nothing_else(self):
        tag = self.html_tag()
        self.assertIn("ui_theme(request)", tag, "the theme left <html>")
        for per_page in ("thumb", "unread", "main_state"):
            self.assertNotIn(per_page, tag,
                             f"<html> carries {per_page!r}, which a body swap would not update")

    def test_main_carries_what_belongs_to_the_page(self):
        tag = self.main_tag()
        self.assertIn('data-thumb-size-key="{{ thumb_key }}"', tag)
        self.assertIn("ui_thumb_size(request, thumb_key)", tag)
        self.assertIn("--thumb-size:", tag)
        self.assertIn("block main_state", tag,
                      "a page has no way to add state of its own inside the swap")

    def test_the_novel_page_puts_its_filter_there(self):
        source = NOVEL_HTML.read_text()
        self.assertIn("{% block main_state %}", source)
        self.assertNotIn("{% block html_attrs %}", source,
                         "back on <html>, which a body swap leaves behind")

    def test_the_rule_matches_where_the_attribute_is_rendered(self):
        # The pre-paint half. A selector still saying `html[...]` would
        # match nothing at all, and the failure is a screenful of read
        # chapters for one frame -- not an error.
        css = re.sub(r"/\*.*?\*/", "", THEME_CSS.read_text(), flags=re.S)
        self.assertIn("main[data-unread-only] li[data-chapter-search]", css)
        self.assertNotIn("html[data-unread-only]", css)

    def test_both_readers_of_the_key_read_it_off_main(self):
        # thumbnail-size.js decides the key and applies the size;
        # ui-prefs.js needs the same key for its one-time migration. Either
        # one still asking <html> would quietly use the home page's key.
        for name in ("thumbnail-size.js", "ui-prefs.js"):
            code = _markup_of(JS_DIR / name)
            self.assertIn('document.querySelector("main")', code,
                          f"{name} does not look at <main>")
            self.assertNotIn("documentElement.dataset.thumbSizeKey", code,
                             f"{name} still reads the key off <html>")

    def test_the_size_is_applied_where_the_grids_can_inherit_it(self):
        code = _markup_of(JS_DIR / "thumbnail-size.js")
        self.assertIn('page.style.setProperty("--thumb-size"', code)
        self.assertNotIn('documentElement.style.setProperty("--thumb-size"', code)


class NoScriptRacesThePaintAnyMore(unittest.TestCase):
    """What this change was for, stated so it cannot quietly come back."""

    def test_nothing_in_head_reads_localstorage(self):
        # The three inline blocks are the whole point. Everything before
        # <body> that reads a preference is a preference the server could
        # have rendered instead.
        for path in (BASE_HTML, NOVEL_HTML):
            head = _markup_of(path).split("<body", 1)[0]
            self.assertNotIn("localStorage", head,
                             f"{path.name} still reads localStorage before <body>")

    def test_the_comment_stripper_does_not_pass_by_seeing_nothing(self):
        # The negative control for the scan above. base.html's comments
        # explain the rule by naming the thing it forbids, so the stripper
        # is load-bearing -- and a stripper that removes too much would let
        # a real localStorage read through unnoticed.
        self.assertIn("localStorage", BASE_HTML.read_text(),
                      "the comment this stripper exists for is gone; simplify the test")
        self.assertNotIn("localStorage", _markup_of(BASE_HTML))
        self.assertIn("ui_theme(request)", _markup_of(BASE_HTML))
        self.assertIn("<html", _markup_of(BASE_HTML))

    def test_the_three_keys_are_gone_from_localstorage_entirely(self):
        # Not just out of <head>: an owner still writing to localStorage
        # would leave two stores disagreeing, and the server reads only one.
        for name in ("thumbnail-size.js", "novel-chapters.js"):
            source = (JS_DIR / name).read_text()
            self.assertNotIn("localStorage", source,
                             f"{name} still uses localStorage for a server-rendered preference")

    def test_the_migration_is_there_and_says_when_it_can_go(self):
        # Dropping it silently resets everyone's theme and cover size, and
        # nothing would say why.
        source = JS.read_text()
        self.assertIn("localStorage.getItem(name)", source)
        self.assertIn("function migrate()", source)


if __name__ == "__main__":
    unittest.main()
