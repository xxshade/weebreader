"""Blurring a cover on the home grid.

The home grid is the one screen that shows every cover the library holds
at once, to whoever happens to be looking at the screen. This is the flag
that softens one of them there and nowhere else, and the two things worth
pinning are that it is a property of the SERIES (a column, like
publication_status, not a row per reader) and that an unchecked box turns
it back off -- a checkbox posts nothing at all when it is clear, so a
handler that only ever reads a present field can be switched on and never
off.
"""
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.datastructures import FormData
from starlette.requests import Request

from app.auth.dependencies import get_current_user
from app.auth.router import (
    MAX_COVER_BLUR, MIN_COVER_BLUR, set_cover_blur, set_page_time_cap,
)
from app.auth.service import hash_token, session_expiry
from app.config import settings
from app.database import get_connection, init_db
from app.library.router import (
    home, reading_grid, save_hidden_statuses, search_items_partial, series_card,
    series_items_grid, set_series_blur_cover, settings_page,
)


def _request(path="/", *, htmx=False, form=None):
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"hx-request", b"true")] if htmx else [],
        "client": ("test", 1), "server": ("test", 80),
    }
    request = Request(scope)
    if form is not None:
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
        self.user = {"id": 1}
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'M','/m','comic')")
        # Exactly how the scanner writes one: it has never heard of this
        # column, which is why the default has to live in the column.
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'Serie', 'serie', '/f')")
        # A series is only on the shelf while it holds something (see
        # library/series_scope.py), so the card fixtures need one item.
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size) VALUES"
            " (1, 1, 1, 'V1', 'v1', '/f/v1.cbz', 'cbz', 'h1', 1)")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url) VALUES (1, 'Novela', 'https://x/1')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def _stored(self):
        row = await (await self.db.execute("SELECT blur_cover FROM series WHERE id = 1")).fetchone()
        return row["blur_cover"]

    async def _user_from_session(self):
        """The real dependency, through a real session row."""
        token = "tok-for-tests"
        await self.db.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (1, ?, ?)",
            (hash_token(token), session_expiry()))
        await self.db.commit()
        return await get_current_user(session=token, db=self.db)

    async def _cards(self):
        response = await home(_request(), self.user, self.db)
        return {c["name"]: c for c in response.context["cards"]}


class HowHardItBlurs(_Case):
    """The FLAG is per series; the RADIUS is per reader. What is on a cover
    is a fact about the book, but how hard to hide it is about the room you
    are in.
    """

    async def _stored_blur(self):
        row = await (await self.db.execute("SELECT cover_blur_px FROM users WHERE id = 1")).fetchone()
        return row["cover_blur_px"]

    async def test_a_new_reader_gets_a_working_default(self):
        # In the COLUMN, because nothing that creates a user knows this
        # field exists -- and because a 0 here would silently be a feature
        # that does nothing while claiming to be on.
        self.assertEqual(await self._stored_blur(), 12)
        self.assertGreaterEqual(await self._stored_blur(), MIN_COVER_BLUR)

    async def test_it_can_be_changed(self):
        for value in (MIN_COVER_BLUR, 20, MAX_COVER_BLUR):
            await set_cover_blur(_request(htmx=True), value, self.user)
            self.assertEqual(await self._stored_blur(), value)

    async def test_a_value_off_the_slider_is_refused_and_changes_nothing(self):
        await set_cover_blur(_request(htmx=True), 20, self.user)
        for value in (MIN_COVER_BLUR - 1, MAX_COVER_BLUR + 1, 0, -8, 9999):
            answer = await set_cover_blur(_request(htmx=True), value, self.user)
            self.assertIn("bad-blur", answer.headers["location"])
            self.assertEqual(await self._stored_blur(), 20)

    async def test_the_home_grid_publishes_it_as_a_real_length(self):
        # The whole point of putting it in the markup: a cover somebody
        # asked to blur must never be drawn sharp, and anything a script
        # applies after the first paint is one frame of exactly that.
        #
        # Rendered rather than read out of the context, because the failure
        # this guards against is silent in Jinja: a key the template asks
        # for and the context does not carry renders as NOTHING, and
        # "--cover-blur: px" is a declaration the browser drops without a
        # word, taking the blur with it. Measured -- that is what happened.
        await set_cover_blur(_request(htmx=True), 30, self.user)
        response = await home(_request(), await self._user_from_session(), self.db)
        html = response.template.render(response.context)
        self.assertIn("--cover-blur: 30px", html)

    async def test_the_session_query_carries_it(self):
        # home() gets its user from get_current_user, so a column missing
        # THERE is what empties the declaration above. A test that passed
        # its own dict would never see it.
        user = await self._user_from_session()
        self.assertEqual(user["cover_blur_px"], 12)

    async def test_the_slider_offers_exactly_what_the_endpoint_accepts(self):
        # Two files: the bounds are checked in auth/router.py and drawn in
        # settings.html. A control that offers a value its own save refuses
        # is a dead end the reader can reach by dragging.
        response = await settings_page(
            _request("/settings"), await self._user_from_session(), self.db)
        html = response.template.render(response.context)
        slider = html.split('id="cover-blur"', 1)[1].split(">", 1)[0]
        self.assertIn(f'min="{MIN_COVER_BLUR}"', slider)
        self.assertIn(f'max="{MAX_COVER_BLUR}"', slider)


class TheBlurFlag(_Case):
    async def test_a_series_the_scanner_made_is_not_blurred(self):
        self.assertEqual(await self._stored(), 0)
        self.assertFalse((await self._cards())["Serie"]["blur_cover"])

    async def test_ticking_the_box_sets_it_and_the_card_says_so(self):
        await set_series_blur_cover(1, _request(htmx=True, form={"blur_cover": "1"}),
                                    self.user, self.db)
        self.assertEqual(await self._stored(), 1)
        self.assertTrue((await self._cards())["Serie"]["blur_cover"])

    async def test_clearing_the_box_turns_it_back_off(self):
        # The one that a naive handler gets wrong: an unchecked checkbox is
        # ABSENT from the form, so "update only what was sent" makes this a
        # one-way switch.
        await set_series_blur_cover(1, _request(htmx=True, form={"blur_cover": "1"}),
                                    self.user, self.db)
        await set_series_blur_cover(1, _request(htmx=True, form={}), self.user, self.db)
        self.assertEqual(await self._stored(), 0)
        self.assertFalse((await self._cards())["Serie"]["blur_cover"])

    async def test_the_live_card_carries_it_too(self):
        # A card inserted or replaced by library-events.js is built from
        # this payload, and a card that arrives live must not look
        # different from one the server rendered.
        await set_series_blur_cover(1, _request(htmx=True, form={"blur_cover": "1"}),
                                    self.user, self.db)
        self.assertTrue((await series_card(1, self.user, self.db))["blur_cover"])

    async def test_it_belongs_to_the_series_and_not_to_the_reader(self):
        # Same reasoning as publication_status: what is on a cover is a
        # fact about the book. A second reader sees the same blur, and
        # there is no per-user row that could disagree.
        await set_series_blur_cover(1, _request(htmx=True, form={"blur_cover": "1"}),
                                    self.user, self.db)
        response = await home(_request(), {"id": 2}, self.db)
        card = {c["name"]: c for c in response.context["cards"]}["Serie"]
        self.assertTrue(card["blur_cover"])

    async def test_a_novel_card_answers_the_flag_rather_than_omitting_it(self):
        # The grid holds both kinds and reads card.blur_cover on every one
        # of them. A missing key is not an error in Jinja -- it renders as
        # nothing -- so the check that matters is that the key is there.
        self.assertIn("blur_cover", (await self._cards())["Novela"])
        self.assertFalse((await self._cards())["Novela"]["blur_cover"])

    async def test_an_unknown_series_is_a_404(self):
        with self.assertRaises(HTTPException) as caught:
            await set_series_blur_cover(9, _request(htmx=True, form={"blur_cover": "1"}),
                                        self.user, self.db)
        self.assertEqual(caught.exception.status_code, 404)

    async def test_it_does_not_pretend_the_series_changed_on_disk(self):
        # "Date modified" on the home page promises the series' CONTENTS
        # changed; a display preference is not that, the same way a rename
        # is not.
        before = await (await self.db.execute("SELECT updated_at FROM series WHERE id = 1")).fetchone()
        await set_series_blur_cover(1, _request(htmx=True, form={"blur_cover": "1"}),
                                    self.user, self.db)
        after = await (await self.db.execute("SELECT updated_at FROM series WHERE id = 1")).fetchone()
        self.assertEqual(before["updated_at"], after["updated_at"])


class SavingWithoutALoad(_Case):
    """Every plain preference on the settings page saves in place.

    Losing your scroll position to be told a slider moved is the whole
    complaint. The rule has two halves, and both have to hold: htmx gets a
    204 with nothing to re-render, and a browser with no JavaScript still
    gets the redirect that shows it the page again.
    """

    async def test_htmx_gets_a_204_and_no_redirect(self):
        answer = await set_cover_blur(_request(htmx=True, form={}), 20, self.user)
        self.assertEqual(answer.status_code, 204)
        self.assertNotIn("location", {k.lower() for k in answer.headers})

    async def test_a_plain_post_still_gets_its_page_back(self):
        answer = await set_cover_blur(_request(form={}), 20, self.user)
        self.assertEqual(answer.status_code, 302)
        self.assertIn("/settings", answer.headers["location"])

    async def test_a_refusal_is_always_the_page_even_over_htmx(self):
        # There is nowhere to put a message in a 204, and the message is
        # the only reason a refusal is worth showing.
        answer = await set_cover_blur(_request(htmx=True, form={}), 9999, self.user)
        self.assertEqual(answer.status_code, 302)
        self.assertIn("bad-blur", answer.headers["location"])

    async def test_the_page_time_cap_follows_the_same_rule(self):
        self.assertEqual(
            (await set_page_time_cap(_request(htmx=True, form={}), 60, self.user)).status_code, 204)
        self.assertEqual(
            (await set_page_time_cap(_request(form={}), 60, self.user)).status_code, 302)

    async def test_hiding_a_status_follows_it_too(self):
        # This one changes what the HOME page shows, not this one, so
        # re-rendering the settings page teaches the reader nothing.
        answer = await save_hidden_statuses(
            _request(htmx=True, form={"hidden_statuses": "completed"}), self.user)
        self.assertEqual(answer.status_code, 204)
        row = await (await self.db.execute(
            "SELECT hidden_statuses FROM users WHERE id = 1")).fetchone()
        self.assertIn("completed", row["hidden_statuses"])


class ItsVolumesOnTheHomePage(_Case):
    """A blurred cover is the series' choice, and its volumes wear it
    wherever the HOME page shows them: the Reading strip and the item
    search sit in front of whoever is looking at the screen just as the
    grid does. The series page is opened on purpose and stays sharp."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.db.execute("UPDATE items SET cover_path = 'v1.webp' WHERE id = 1")
        await self.db.execute(
            "INSERT INTO reading_progress (user_id, item_id, status, progress_percent, last_read_at)"
            " VALUES (1, 1, 'reading', 40, datetime('now'))")
        await self.db.commit()
        self.reader = await self._user_from_session()

    async def _blur(self, on):
        await set_series_blur_cover(1, _request(htmx=True, form={"blur_cover": "1"} if on else {}),
                                    self.user, self.db)

    def _img(self, html):
        return html.split('src="/covers/v1.webp?private=1"', 1)[1].split(">", 1)[0]

    async def _strip(self):
        response = await reading_grid(_request(), self.reader, self.db)
        return response.template.render(response.context)

    async def _search(self):
        response = await search_items_partial(_request(), "V1", self.reader, self.db)
        return response.template.render(response.context)

    async def test_the_reading_strip_blurs_it(self):
        await self._blur(True)
        html = await self._strip()
        self.assertIn("cover-blur", self._img(html))
        # The radius travels with the grid a refresh swaps in; without it
        # the class falls back to the stylesheet's default, not the
        # reader's setting.
        self.assertIn(f"--cover-blur: {self.reader['cover_blur_px']}px", html)
        self.assertIn('data-series-id="1"', html)

    async def test_and_the_home_page_renders_the_strip_the_same_way(self):
        await self._blur(True)
        response = await home(_request(), self.reader, self.db)
        html = response.template.render(response.context)
        strip = html.split('id="reading-grid"', 1)[1]
        self.assertIn("cover-blur", self._img(strip))

    async def test_the_item_search_blurs_it(self):
        await self._blur(True)
        html = await self._search()
        self.assertIn("cover-blur", self._img(html))
        self.assertIn(f"--cover-blur: {self.reader['cover_blur_px']}px", html)

    async def test_a_series_not_blurred_shows_both_sharp(self):
        await self._blur(False)
        self.assertNotIn("cover-blur", self._img(await self._strip()))
        self.assertNotIn("cover-blur", self._img(await self._search()))

    async def test_the_series_page_stays_sharp(self):
        await self._blur(True)
        response = await series_items_grid(_request(), 1, self.reader, self.db)
        html = response.template.render(response.context)
        self.assertNotIn("cover-blur", html)

    def test_a_change_reaches_an_open_home_page(self):
        # series_updated is what the checkbox publishes; the strip and the
        # search re-render when they hold one of that series' volumes.
        code = (Path(__file__).resolve().parent.parent / "app/static/js/library-events.js").read_text()
        handler = code.split("function handleSeriesUpdated", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn("#reading-grid ${own}", handler)
        self.assertIn('scheduleRefresh("reading-grid", refreshReadingGrid)', handler)
        self.assertIn("#search-items ${own}", handler)


if __name__ == "__main__":
    unittest.main()
