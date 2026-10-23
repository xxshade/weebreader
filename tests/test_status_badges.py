"""The two statuses, worn on a page as the badge a cover already wears.

They used to be two identical grey selects in the toolbar over the grid.
The same two facts are drawn on the home page as coloured chips, and a
reader should not have to learn that the chip and the control are one
thing -- so on a series' and a novel's own page the control IS the chip.

Three things about that are load-bearing, and each fails silently:

- The COLOUR is half of what the badge says, and markup keeps the class it
  was rendered with. So the endpoints answer an htmx request with the
  re-rendered badge rather than the 204 they used to; revert that and the
  chip goes on showing the old colour until a reload.
- The chip is a <span> with a transparent <select> OVER it, never the
  select itself: a select sizes to its widest OPTION, so "Reading" rendered
  as wide as "Stopped reading" and the chip stopped hugging its word.
- The plain form post still redirects. Every form on these pages works
  with no JavaScript, and that is the half htmx never exercises.
"""
import re
import tempfile
import unittest
from pathlib import Path

from starlette.datastructures import FormData
from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.tracking import service as tracking_service
from app.library.router import set_series_publication_status, set_series_status
from app.novels.router import set_novel_publication_status, set_novel_status
from app.status_labels import PUBLICATION_STATUS_LABELS, STATUS_LABELS

ROOT = Path(__file__).resolve().parents[1]
BADGES = ROOT / "app/templates/_status_badges.html"
SERIES_HTML = ROOT / "app/templates/library/series.html"
NOVEL_HTML = ROOT / "app/templates/novels/detail.html"
HOME_HTML = ROOT / "app/templates/library/index.html"


def _request(form, *, htmx=True):
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
        "headers": [(b"hx-request", b"true")] if htmx else [],
        "client": ("test", 1), "server": ("test", 80),
    }
    request = Request(scope)
    request._form = FormData(form)
    return request


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        # Setting any status now queues a tracker push (nothing linked
        # here, so it ends at once), and it must end before this test's
        # database is deleted.
        self.addAsyncCleanup(tracking_service.drain)
        self.user = {"id": 1}
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'M','/m','comic')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'Serie', 'serie', '/f')")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url, status)"
            " VALUES (1, 'N', 'https://x/1', 'Ongoing')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()


class TheEndpointAnswersWithTheBadge(_Case):
    async def test_a_series_tracking_status_comes_back_wearing_its_colour(self):
        response = await set_series_status(1, _request({"status": "reading"}), self.user, self.db)
        body = response.body.decode()
        self.assertIn('id="tracking-status-badge"', body)
        self.assertIn("bg-indigo-600", body)
        self.assertIn(">Reading<", body)

    async def test_a_series_publication_status_comes_back_wearing_its_colour(self):
        response = await set_series_publication_status(
            1, _request({"publication_status": "hiatus"}), self.user, self.db)
        body = response.body.decode()
        self.assertIn('id="publication-status-badge"', body)
        self.assertIn("bg-yellow-300", body)

    async def test_clearing_a_tracking_status_comes_back_quiet(self):
        # Unset is the ordinary state and has no colour of its own: one of
        # the six saying "No status" would read as a status.
        response = await set_series_status(1, _request({"status": ""}), self.user, self.db)
        body = response.body.decode()
        self.assertIn("bg-neutral-800", body)
        for colour in ("bg-indigo-600", "bg-green-600", "bg-blue-600"):
            self.assertNotIn(colour, body)

    async def test_a_novel_keeps_the_sources_word_in_the_empty_option(self):
        # What makes "follow the source again" a choice rather than a guess.
        response = await set_novel_publication_status(
            1, _request({"publication_status": "canceled"}), self.user, self.db)
        body = response.body.decode()
        self.assertIn("From the source (Ongoing)", body)

    async def test_an_override_brings_the_sources_own_wording_with_it(self):
        # Inside the badge, so the swap carries it: outside, choosing an
        # override left it absent until the next reload, and choosing the
        # source again left it on screen contradicting the badge.
        overridden = (await set_novel_publication_status(
            1, _request({"publication_status": "canceled"}), self.user, self.db)).body.decode()
        self.assertIn("Ongoing at source", overridden)

        back = (await set_novel_publication_status(
            1, _request({"publication_status": ""}), self.user, self.db)).body.decode()
        self.assertNotIn("at source", back)

    async def test_a_novel_tracking_status_comes_back_wearing_its_colour(self):
        response = await set_novel_status(1, _request({"status": "on_hold"}), self.user, self.db)
        self.assertIn("bg-neutral-600", response.body.decode())


class WithNoJavaScript(_Case):
    """Every form on these pages is a plain post that works on its own."""

    async def test_each_of_the_four_redirects(self):
        for call, form in (
            (lambda r: set_series_status(1, r, self.user, self.db), {"status": "reading"}),
            (lambda r: set_series_publication_status(1, r, self.user, self.db),
             {"publication_status": "hiatus"}),
            (lambda r: set_novel_status(1, r, self.user, self.db), {"status": "reading"}),
            (lambda r: set_novel_publication_status(1, r, self.user, self.db),
             {"publication_status": "hiatus"}),
        ):
            with self.subTest(form=form):
                response = await call(_request(form, htmx=False))
                self.assertEqual(response.status_code, 302)


class TheChipIsNotTheSelect(unittest.TestCase):
    def test_the_visible_chip_is_a_span_the_select_only_covers(self):
        # A select sizes to its widest OPTION, so making it the chip made
        # "Reading" as wide as "Stopped reading" -- the chip stopped
        # hugging its word, which is the whole thing being copied from the
        # cover.
        markup = BADGES.read_text()
        for macro in ("tracking_select", "publication_select"):
            with self.subTest(macro=macro):
                body = markup.split(f"{{% macro {macro}(", 1)[1].split("{% endmacro %}", 1)[0]
                chip = re.search(r'<span class="text-xs rounded[^"]*"', body)
                self.assertIsNotNone(chip, "no chip span")
                self.assertIn('class="status-select"', body)

    def test_the_overlay_is_reachable_rather_than_hidden(self):
        # display:none and visibility:hidden take it off the keyboard and
        # out of the accessibility tree; opacity does not.
        css = (ROOT / "app/static/src/theme.css").read_text()
        rule = css.split(".status-select {", 1)[1].split("}", 1)[0]
        self.assertIn("opacity: 0", rule)
        self.assertNotIn("display: none", rule)
        self.assertNotIn("visibility: hidden", rule)
        # The element that has focus is invisible, so the CHIP wears the
        # ring -- the same arrangement the eye toggle uses.
        self.assertIn(".status-chip:has(.status-select:focus-visible)", css)

    def test_the_chip_matches_the_cover_by_carrying_the_same_utilities(self):
        # Not a screenshot: the point is that both are the same literal
        # classes, so they cannot drift into looking nearly alike.
        cover = re.search(r'<span class="\{\{ badge_bg \}\}([^"]*)"', HOME_HTML.read_text())
        self.assertIsNotNone(cover)
        for utility in cover.group(1).split():
            self.assertIn(utility, BADGES.read_text())


class WhereTheyLive(unittest.TestCase):
    def test_both_pages_wear_the_badges_rather_than_their_own_selects(self):
        for page in (SERIES_HTML, NOVEL_HTML):
            with self.subTest(page=page.name):
                text = page.read_text()
                self.assertIn("badges.tracking_select(", text)
                self.assertIn("badges.publication_select(", text)
                self.assertNotIn('name="publication_status"', text)

    def test_the_series_bulk_actions_are_icons_that_still_say_their_names(self):
        # An icon with no accessible name is the silent half of turning a
        # labelled button into a glyph.
        text = SERIES_HTML.read_text()
        buttons = re.findall(r"<button\b(.*?)</button>", text, re.S)
        found = {}
        for button in buttons:
            label = re.search(r'aria-label="(Mark all \w+)"', button)
            if label:
                found[label.group(1)] = button
        self.assertEqual(set(found), {"Mark all read", "Mark all unread"})
        for label, button in found.items():
            with self.subTest(label=label):
                self.assertIn(f'title="{label}"', button)
                self.assertIn("<svg", button)
                # No words left in the button itself, or it is not an icon.
                self.assertEqual(re.sub(r"<[^>]*>", "", button.split(">", 1)[1]).strip(), "")

    def test_the_bulk_actions_sit_with_the_title_not_in_the_grid_toolbar(self):
        # The toolbar being the sort-and-reorder panel under the title.
        text = SERIES_HTML.read_text()
        toolbar = text.index('<div id="series-order-panel"')
        self.assertLess(text.index('title="Mark all read"'), toolbar)
        self.assertLess(text.index("badges.tracking_select("), toolbar)


if __name__ == "__main__":
    unittest.main()
