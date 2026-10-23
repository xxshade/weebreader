"""What a `<script src>` in page content has to survive being RE-RUN.

Every navigation swaps `<body>` now -- `hx-boost` is on it and nothing
overrides that any more, the readers included -- and htmx re-inserts
every script in the swapped content with
`document.createElement("script")`, which the spec marks force-async. It
copies the attributes, so `defer` comes along and its ordering guarantee
does not: `defer` orders scripts while the PARSER is placing them, and
there is no parser here. Three consequences, each measured, and each
silent or nearly so:

1. **Sibling scripts execute in whichever order they finish downloading.**
   `series.html` and `novels/detail.html` used to load Sortable, then
   `reorder.js` (which defines `window.makeReorderable`), then a caller of
   it. Measured with `reorder.js` delayed 300ms: `makeReorderable is not
   defined`, 3 runs of 3. Measured with SORTABLE delayed instead: no error
   at all and the grid simply did not drag, because `arm()` returns at its
   first line without it.

2. **The file runs again in the SAME document.** A top-level `const` throws
   "already declared" on the second run, and two files declaring the same
   name collide the first time one document shows both pages. Every file
   here is an IIFE today; this pins that it stays one.

3. **What is re-run accumulates.** A vendored library re-executed by a swap
   brings a SECOND copy with its own empty instance registry, so
   `Sortable.get()` cannot see what the first copy armed. Measured on a
   second visit to the same page: both containers carried two expandos,
   `Sortable<t1>` and `Sortable<t2>` -- two live instances on one element --
   and a single drag POSTed the new order TWICE. The element's own flag
   fixed the consequence (one expando, one POST); what fixes the CAUSE is
   that a shared file is loaded from `<head>`, which a swap leaves alone.
   Measured under hx-boost, home -> series -> home -> novel -> home ->
   series: **3 copies of Sortable from the pages, 1 from `<head>`**.

   Moving the file is not enough by itself, and the same run says so: the
   MutationObserver in `reorder.js` is created per CALL, and its callers
   are per-page, so it climbed to 4 either way. Hence the registration
   guard -- with it, 9 navigations leave one observer per spec, steady.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "app/static/js"
TEMPLATES = ROOT / "app/templates"
BASE = TEMPLATES / "base.html"
REORDER = JS_DIR / "reorder.js"
CALLERS = ("series-reorder.js", "novel-reorder.js")
# The two pages that reorder, and the caller each one owns.
PAGES = {"library/series.html": "series-reorder.js",
         "novels/detail.html": "novel-reorder.js"}
SHARED = ("src/vendor/Sortable.min.js", "js/reorder.js")
# The same rule, one screen over. All three readers call into
# `window.readerResume` and `window.createReadingTimeTracker`, and the text
# engine into `window.readerBlocks`; each keeps only its own engine.
READERS = {"reader/comic.html": "reader-comic.js",
           "reader/epub.html": "reader-text.js",
           "novels/reader.html": "reader-text.js"}
READER_SHARED = ("js/reader-resume.js", "js/reading-time.js", "js/reader-blocks.js")


def _markup_of(path: Path) -> str:
    """The template with its comments blanked out.

    A scan for SCRIPT TAGS, and the comments in these three explain the
    rule by naming the files it moved. Same treatment, same reason, as
    tests/test_reader_shell.py.
    """
    source = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)
    return re.sub(r"<!--.*?-->", "", source, flags=re.S)


def _head_of(base: str) -> str:
    return base[:base.index("</head>")]


def _code_of(path: Path) -> str:
    """The script with its comments blanked out.

    These are scans for CODE, and the comments below explain each rule by
    naming exactly what it forbids. Same treatment, same reason, as
    tests/test_events.py.
    """
    source = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in source.splitlines())


class NothingDeclaresABindingInTheGlobalSCOPE(unittest.TestCase):
    """A top-level `const` is a crash the second time the file runs."""

    def test_every_script_keeps_its_declarations_to_itself(self):
        offenders = []
        for path in sorted(JS_DIR.glob("*.js")):
            for number, line in enumerate(_code_of(path).splitlines(), 1):
                if re.match(r"(const|let|class)\s", line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "these would throw on a second run in one document")

    def test_the_scan_can_actually_see_a_declaration(self):
        # Negative control: a regex anchored wrong matches nothing forever.
        sample = "const spec = {};\n    const inner = 1;\nlet other;\n"
        found = [line for line in sample.splitlines() if re.match(r"(const|let|class)\s", line)]
        self.assertEqual(found, ["const spec = {};", "let other;"])


class WhatIsSHAREDIsLoadedWhereASwapCannotReachIt(unittest.TestCase):
    """`<head>` is the mirror of `<main>`.

    The per-page state went ONTO `<main>` because a whole-`<body>` swap
    replaces it. A shared library goes into `<head>` for the same reason
    read the other way: a swap leaves `<head>` alone, so what is loaded
    there is loaded once per DOCUMENT. Measured under hx-boost across
    home -> series -> home -> novel -> home -> series: 3 copies of
    Sortable when the pages carried it, 1 from `<head>`.
    """

    def test_the_shared_two_are_in_the_head(self):
        head = _head_of(_markup_of(BASE))
        for src in SHARED:
            self.assertIn(f"asset_url('{src}')", head, f"{src} is not loaded from <head>")

    def test_neither_page_loads_them_any_more(self):
        # The actual regression: putting either line back gives that page
        # another copy of the library on every navigation into it.
        for name in PAGES:
            markup = _markup_of(TEMPLATES / name)
            for src in SHARED:
                self.assertNotIn(f"asset_url('{src}')", markup,
                                 f"{name} loads {src} again, inside what a swap re-runs")

    def test_each_page_still_carries_its_own_caller(self):
        # The other half. The callers are per-page on purpose -- which
        # containers, which items, what the endpoint wants -- and a page
        # that lost its own would simply stop reordering.
        for name, caller in PAGES.items():
            self.assertIn(f"asset_url('js/{caller}')", _markup_of(TEMPLATES / name),
                          f"{name} no longer registers its reorderable")

    def test_no_caller_moved_into_the_head_with_them(self):
        # They are not shared, and one in <head> would run on every page
        # in the app for the sake of two.
        head = _head_of(_markup_of(BASE))
        for caller in CALLERS:
            self.assertNotIn(f"js/{caller}", head, f"{caller} is loaded app-wide")

    def test_the_scan_can_tell_head_from_body(self):
        # Negative control both ways: a <head> slice that swallowed the
        # document would pass every test above, and one that ended too
        # early would pass the two NotIns for free.
        head = _head_of(_markup_of(BASE))
        self.assertIn("asset_url('src/vendor/htmax.min.js')", head)
        self.assertNotIn("js/library-events.js", head,
                         "the <head> slice runs past </head>")


class RegisteringTwICEDoesNotBuildUp(unittest.TestCase):
    """reorder.js is in `<head>` now; its callers are not.

    So a swap re-runs each caller and `makeReorderable` is called again for
    the same selector. Every call used to install another document-wide
    MutationObserver -- 4 and climbing over six navigations, and moving the
    file to `<head>` did NOT change that number, because the observer is
    created per call. With the guard: one per spec, steady over nine.
    """

    def test_a_second_registration_returns_at_the_door(self):
        code = _code_of(REORDER)
        self.assertIn("registered.has(containerSelector)", code,
                      "nothing stops a re-run registering the same spec again")
        self.assertIn("registered.add(containerSelector)", code)
        guard = code.index("registered.has(containerSelector)")
        observer = code.index("new MutationObserver")
        self.assertLess(guard, observer,
                        "the observer is installed before the guard can stop it")

    def test_the_observer_is_still_what_catches_a_swapped_in_grid(self):
        # Control for the test above: it would also pass on a file that
        # stopped watching the DOM at all, and then a grid arriving by
        # swap -- which is the only way one arrives now -- never drags.
        code = _code_of(REORDER)
        self.assertIn("new MutationObserver", code)
        self.assertIn("observe(document.documentElement", code)


class TheChapterFilterWatchesTheDocumentOnce(unittest.TestCase):
    """The other half of the same family, and the one that was left.

    novel-chapters.js installed a MutationObserver on every run, and its
    closure held THAT page's checkbox -- so a swap left the old one
    watching a control it had already detached. Measured on repeated
    boosted visits to /novels/{id}: 3, then 4, then 5, against a flat 2
    from reorder.js in the same run.

    The fix is the shape the live-update banner already uses: one
    observer, looking the current control up when it fires.
    """

    def setUp(self):
        self.code = _code_of(JS_DIR / "novel-chapters.js")

    def test_a_second_run_does_not_install_another_one(self):
        # The GUARD, not the flag: `if (true)` with the assignment still
        # inside the block leaves the name in the file and the observer
        # back on every run.
        self.assertIn("if (!window.novelChaptersWatchBound) {", self.code,
                      "nothing stops a re-run watching the document again")
        self.assertIn("window.novelChaptersWatchBound = true;", self.code)
        guard = self.code.index("if (!window.novelChaptersWatchBound)")
        observer = self.code.index("new MutationObserver")
        self.assertLess(guard, observer,
                        "the observer is installed before the guard can stop it")

    def test_it_looks_the_filter_up_when_it_fires(self):
        # The guard alone would be a REGRESSION without this: one observer
        # holding the first novel's checkbox, over every novel after it.
        body = self.code.split("new MutationObserver", 1)[1]
        self.assertIn('document.getElementById("chapter-unread-only")', body)
        self.assertIn("window.novelChaptersApply", body)

    def test_a_page_with_no_filter_clears_the_one_before_it(self):
        # Drag mode and a novel with no chapters return before apply()
        # exists. Without this the observer would go on calling the
        # PREVIOUS novel's filter, over rows that are no longer there.
        before = self.code.split("if (!box || !unreadOnly) return", 1)[0]
        self.assertIn("window.novelChaptersApply = null", before)

    def test_the_observer_is_still_what_catches_a_marked_chapter(self):
        # Control for all three: a file that stopped watching data-read
        # would pass every test above, and a chapter marked read while
        # "Unread only" is on would simply stay on the list.
        self.assertIn("new MutationObserver", self.code)
        self.assertIn('attributeFilter: ["data-read"]', self.code)


class TheReorderTrioDoesNotDependOnLoadOrder(unittest.TestCase):
    """Sortable and reorder.js sit in base.html's <head>, which a boosted
    visit does not swap and a parse runs ahead of every page's own deferred
    scripts -- so a caller can simply call. (A load-order queue used to
    stand in for that; it had nothing left to catch.)"""

    def test_the_shared_files_run_before_the_callers(self):
        head = (TEMPLATES / "base.html").read_text().split("</head>")[0]
        sortable = head.index("Sortable.min.js")
        shared = head.index("js/reorder.js")
        self.assertLess(sortable, shared, "reorder.js would arm before Sortable is there")

    def test_each_caller_calls_it_directly(self):
        for name in CALLERS:
            self.assertIn("window.makeReorderable(spec);", _code_of(JS_DIR / name), name)

    def test_sortable_is_still_what_arms_them(self):
        # Control for the test above: it would also pass on a file that
        # stopped creating Sortable instances at all.
        code = _code_of(REORDER)
        self.assertIn("window.Sortable.create(container", code)

    def test_the_armed_guard_is_on_the_element_not_in_a_registry(self):
        # Sortable.get() belongs to ONE copy of the library, and a swap
        # brings a second. The element is what both agree on.
        code = _code_of(REORDER)
        self.assertIn('container.dataset.reorderArmed === "1"', code)
        self.assertIn('container.dataset.reorderArmed = "1"', code)
        self.assertNotIn("if (window.Sortable.get(container)) continue", code,
                         "back to a guard one copy of Sortable cannot see past")

    def test_the_stripper_is_not_what_makes_those_pass(self):
        # Both ways. These comments quote the very calls they forbid.
        self.assertIn("Sortable.get()", REORDER.read_text(),
                      "the comment this stripper exists for is gone; simplify the test")
        self.assertNotIn("Sortable.get()", _code_of(REORDER))
        self.assertIn("window.Sortable.create(container", _code_of(REORDER))


class TheReadersShareTheirFilesTheSameWay(unittest.TestCase):
    """And theirs was the case where the ORDER mattered.

    The three reader templates used to load `reader-resume.js`, then
    `reading-time.js`, then the engine that calls into both -- document
    order, with a comment saying `defer` would keep it. A swap does not:
    htmx re-inserts each script with `document.createElement`, which the
    spec marks force-async, and the copied `defer` orders nothing because
    no parser is placing it.

    Measured before this, a boosted click from the home page into the
    comic reader, **nothing delayed**:

        Firefox    `createReadingTimeTracker is not defined` in 3 of 5
                   runs, and the reader never rendered -- no page, no
                   indicator
        Chromium   5 of 5 warm, and the same failure every time
                   `reading-time.js` was delayed 400ms

    `reader-resume.js` fails more quietly, because the engines call it
    through `?.`: no error, and the one thing it exists for silently not
    installed. From `<head>` all six combinations came back alive, both
    browsers, including the 400ms delays.

    Neither is per-page state, which is what makes `<head>` right rather
    than merely effective: `reading-time.js` defines a factory and binds
    nothing until a reader calls it, and `reader-resume.js` installs
    itself once per DOCUMENT already.
    """

    def test_every_shared_file_is_in_the_head(self):
        head = _head_of(_markup_of(BASE))
        for src in READER_SHARED:
            self.assertIn(f"asset_url('{src}')", head, f"{src} is not loaded from <head>")

    def test_no_reader_loads_them_any_more(self):
        # The regression in one line: either tag back in page content is
        # a reader that fails to start, on whichever load happens to be
        # slow.
        for name in READERS:
            markup = _markup_of(TEMPLATES / name)
            for src in READER_SHARED:
                self.assertNotIn(f"asset_url('{src}')", markup,
                                 f"{name} loads {src} again, inside what a swap re-runs")

    def test_each_reader_still_carries_its_own_engine(self):
        # The per-page half: the engine builds THIS reader, and a
        # template that lost its own would render an empty shell.
        for name, engine in READERS.items():
            self.assertIn(f"asset_url('js/{engine}')", _markup_of(TEMPLATES / name),
                          f"{name} no longer loads its engine")

    def test_no_engine_moved_into_the_head_with_them(self):
        # An engine in <head> would run once per document and never
        # again -- the opposite of what it is for.
        head = _head_of(_markup_of(BASE))
        for engine in set(READERS.values()):
            self.assertNotIn(f"js/{engine}", head, f"{engine} is loaded app-wide")


if __name__ == "__main__":
    unittest.main()
