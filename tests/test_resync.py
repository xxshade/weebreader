"""A page that fell behind is re-synced in place, not reloaded.

When a page is behind -- its stream reconnected after events it never saw,
the server restarted, a history snapshot the library has outgrown -- there
is nothing to patch it with (events are not replayed), and
library-events.js used to `location.reload()`. It now fetches the same URL
and MORPHS it into the page (`resync`), so the nodes on screen stay the
same nodes: scroll position, focus, a half-typed form and an open dialog
survive, and only what changed is changed.

Three things make that safe, each pinned here:

* the page's OWN state is re-applied afterwards. The morph puts back the
  server's card order, `hidden` classes and unpressed tabs, and an
  identical <script> is kept rather than run again, so each page re-applies
  what it saved (the home grid's `resync`, the series sort on
  `htmx:after:swap`, the novel page's chapter filter over live rows);
* `open` stays the reader's on <details> and <dialog>. That guard has to be
  an htmx EXTENSION -- the morph's attribute hook is not raised as a DOM
  event -- so `resync` is on base.html's extension whitelist;
* an event that lands between the fetched page's render and the morph
  makes it fetch again: the render's own `data-rendered-at` against the
  stream's last id.

Measured in both browsers against the real build: the server killed with
SIGKILL (no flush, so a real gap), a series renamed in the database while
it was down, then restarted. The home page showed the new name after one
`GET /`, in the SAME document, with scroll, focus in the search box, the
saved sort, the selected library tab and a filtered-out card all as they
were. A dialog left open with text typed in it stayed open with the text;
a series page sorted descending stayed descending; a novel's volumes stayed
opened and closed as the reader had left them.
"""
import re
import unittest
from pathlib import Path

from tests.test_boost import _config
from tests.test_restore_catch_up import _code_of

ROOT = Path(__file__).resolve().parents[1]
EVENTS_JS = ROOT / "app/static/js/library-events.js"
HOME_JS = ROOT / "app/static/js/home.js"
SERIES_SORT_JS = ROOT / "app/static/js/series-sort.js"
NOVEL_CHAPTERS_JS = ROOT / "app/static/js/novel-chapters.js"
BASE = ROOT / "app/templates/base.html"


def _function(js, name, indent="        "):
    """One function's body, up to its own closing brace at `indent`."""
    return js.split(f"function {name}(", 1)[1].split(f"\n{indent}}}\n", 1)[0]


class TheResync(unittest.TestCase):
    def setUp(self):
        self.js = _code_of(EVENTS_JS)
        self.resync = _function(self.js, "resync")

    def test_it_fetches_this_page_fresh(self):
        self.assertIn("const url = window.location.href;", self.resync)
        self.assertIn("fetch(url", self.resync)
        self.assertIn('cache: "no-store"', self.resync)

    def test_it_morphs_the_body_rather_than_replacing_it(self):
        self.assertIn('swap: "innerMorph"', self.resync)
        self.assertIn("target: document.body", self.resync)
        self.assertIn("htmx.swap(", self.resync)

    def test_a_redirect_navigates_but_transient_errors_retry_in_place(self):
        # A redirect is the server saying something else -- most likely
        # that the session is gone -- and morphing the login page into
        # this one would be a strange way to say so.
        self.assertIn("window.location.assign(response.url)", self.resync)
        self.assertIn("if (!response.ok) throw", self.resync)
        self.assertIn("resyncRetry = setTimeout", self.resync)
        self.assertNotIn("window.location.reload()", self.resync)

    def test_one_at_a_time(self):
        # A gap and a failed catch-up can both land at once; two morphs
        # racing over one body would leave whichever finished last.
        self.assertIn("if (resyncing) return resyncing;", self.resync)
        self.assertIn("resyncing = null", self.resync)

    def test_each_page_reapplies_its_own_state_after_it(self):
        after = self.resync.split("htmx.swap(", 1)[1]
        self.assertIn("window.libraryGrid?.resync?.()", after)
        self.assertIn("window.novelChaptersApply?.()", after)

    def test_what_landed_after_the_render_is_fetched_again(self):
        after = self.resync.split("htmx.swap(", 1)[1]
        self.assertIn("again = streamIsPast(renderedAtIn(text));", after)
        # Once this one is done, never inside it: `resyncing` would make
        # the second call return the first.
        finished = after.split(".finally(", 1)[1]
        self.assertLess(finished.index("resyncing = null;"),
                        finished.index("if (again) resync();"))
        past = _function(self.js, "streamIsPast")
        self.assertIn("return here.number > rendered.number;", past)
        self.assertIn("here.epoch !== rendered.epoch) return false;", past)

    def test_the_render_stamp_is_found_where_base_html_puts_it(self):
        # The same pattern renderedAtIn uses, against the real element.
        pattern = re.search(r"/(id=\"library-events\".*?)/\.exec\(text\)",
                            _function(self.js, "renderedAtIn")).group(1)
        found = re.search(pattern.replace("\\/", "/"), BASE.read_text())
        self.assertIsNotNone(found)
        self.assertIn("{{ rendered_at }}", found.group(1))

    def test_every_catch_up_path_resyncs_instead_of_reloading(self):
        self.assertIn("events_gap: () => window.libraryResync()", self.js)
        self.assertIn("window.libraryResync = resync;", self.js)
        self.assertIn("() => resync(), { once: true }", self.js)
        self.assertIn("const fallback = setTimeout(resync, RESUME_GRACE_MS);", self.js)

    def test_only_the_explicit_reload_button_reloads(self):
        # Automatic catch-up must preserve the document, including when
        # the network is down. The banner still offers a manual reload.
        self.assertEqual(self.js.count("window.location.reload()"), 1)
        self.assertIn('bannerReload.addEventListener("click", () => window.location.reload())',
                      self.js)


class AReaderIsLeftAlone(unittest.TestCase):
    """A reader's DOM is its script's. Measured in both browsers: a resync
    after a server restart morphed /reader/2's server HTML over the live
    comic reader (black page, blank indicator), and deleting comic 1's file
    removed the open reader of NOVEL 1, whose root carries the novel's id
    in data-item-id."""

    def setUp(self):
        self.js = _code_of(EVENTS_JS)

    def test_the_resync_does_not_morph_over_a_reader(self):
        body = _function(self.js, "resync")
        self.assertIn('if (document.getElementById("reader-root")) return', body)
        self.assertLess(body.index("reader-root"), body.index("fetch("))

    def test_item_events_never_select_a_reader(self):
        # One way to select an item's elements, and it excludes the reader.
        self.assertEqual(self.js.count('[data-item-id="${'), 1)
        self.assertIn('[data-item-id="${itemId}"]:not(#reader-root)', self.js)


class OpenStaysTheReaders(unittest.TestCase):
    def setUp(self):
        self.js = _code_of(EVENTS_JS)
        self.hook = self.js.split('htmx.registerExtension("resync"', 1)[1].split("});", 1)[0]

    def test_it_is_an_extension_hook_not_a_dom_listener(self):
        self.assertIn("htmx_before_morph_attr:", self.hook)
        self.assertNotIn('addEventListener("htmx:before:morph:attr"', self.js)

    def test_only_open_only_those_two_and_only_during_a_resync(self):
        self.assertIn("if (!resyncing || detail?.attrName !== \"open\") return;", self.hook)
        self.assertIn('element.tagName === "DETAILS" || element.tagName === "DIALOG"', self.hook)
        self.assertIn("return false", self.hook)

    def test_the_whitelist_lets_it_register(self):
        # registerExtension refuses a name the whitelist does not carry,
        # silently -- the dialog would simply close again.
        self.assertIn("resync", re.split(r",\s*", _config()["extensions"]))


class EachPageReappliesItsState(unittest.TestCase):
    def test_the_home_grid_rereads_its_cards_and_every_saved_setting(self):
        body = _code_of(HOME_JS).split("resync() {", 1)[1].split("\n        },\n", 1)[0]
        self.assertIn("if (!grid.isConnected) return;", body)
        self.assertIn('cards = Array.from(grid.querySelectorAll(".series-card"))', body)
        for call in ("syncHiddenStatusOptions()", "renderSortDirection()",
                     "setLibraryButtonStates()", "applySort()", "applyFilters()"):
            with self.subTest(call=call):
                self.assertIn(call, body)
        # Sorting and filtering read `cards`, so it is re-read first.
        self.assertLess(body.index("cards = "), body.index("applySort()"))

    def test_the_series_sort_redraws_its_arrows_on_a_swap(self):
        js = _code_of(SERIES_SORT_JS)
        listener = js.split('addEventListener("htmx:after:swap"', 1)[1].split("});", 1)[0]
        self.assertIn("renderDirection()", listener)
        self.assertIn("applySort()", listener)

    def test_the_novel_filter_reads_its_rows_when_it_runs(self):
        js = _code_of(NOVEL_CHAPTERS_JS)
        self.assertIn('const rows = () => document.querySelectorAll("[data-chapter-search]");', js)
        self.assertIn("for (const row of rows())", js)


if __name__ == "__main__":
    unittest.main()
