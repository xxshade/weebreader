"""A rejected edit answers 422, and the panel still swaps.

The two edit panels -- a series' name/cover/description, a novel's
title/description -- answer an htmx request with THEMSELVES, re-rendered
from the database, so the panel does not close and two things can be
fixed in one sitting. A refusal is the same panel carrying a message.

That body used to arrive with a **200**, which was the shape htmx 2
forced: it did not swap a 4xx at all, so an error had to come back
looking like a success and the success had to be signalled some other
way (the HX-Trigger header the "Saved" flash listens on). htmx 4 swaps
whatever the element opts into, so the status can say what happened.

Both halves are needed and both are here. Without `hx-status:422` on the
form, base.html's `noSwap: [..., "4xx", ...]` -- which is the right
default for a server whose other error bodies are JSON -- would drop the
panel on the floor and the refusal would be silent. Without the 422, the
opt-in is decoration.

The HX-Trigger stays, and is not a second way of saying the same thing:
it is NARROWER than "the status was 200". The blur-cover checkbox posts
from inside the series panel and answers 200 too, and nobody should see
"Saved" flash for ticking it.
"""
import re
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import rename_series
from app.novels.router import edit_novel_title

ROOT = Path(__file__).resolve().parents[1]
PANELS = {
    "app/templates/library/_series_edit.html": "#series-edit",
    "app/templates/novels/_novel_edit.html": "#novel-edit",
}


def _request(path="/series/1/name", *, htmx=True):
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"hx-request", b"true")] if htmx else [],
        "client": ("test", 1), "server": ("test", 80),
    })


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
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x')")
        await self.db.execute("INSERT INTO libraries (id, name, root_path, media_type)"
            " VALUES (1, 'L', '/l', 'comic')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path)"
            " VALUES (1, 1, 'S', 'S', 's')")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url, sync_status, active)"
            " VALUES (1, 'N', 'https://x/1', 'idle', 1)")
        await self.db.commit()

    async def _close(self):
        await self.db.close()


class ARefusedEditSaysSoInTheStatus(_Case):
    async def test_an_empty_series_name_is_a_422(self):
        response = await rename_series(1, _request(), name="   ",
                                       user=self.user, db=self.db)
        self.assertEqual(response.status_code, 422)
        self.assertIn("empty", response.context["edit_error"].lower())
        # And nothing was written: a 422 that saved anyway is worse than
        # the 200 it replaced.
        row = await (await self.db.execute("SELECT name FROM series WHERE id = 1")).fetchone()
        self.assertEqual(row["name"], "S")

    async def test_an_empty_novel_title_is_a_422(self):
        response = await edit_novel_title(1, _request("/novels/1/title"), "",
                                          user=self.user, db=self.db)
        self.assertEqual(response.status_code, 422)
        self.assertIn("cannot be empty", response.context["edit_error"])

    async def test_a_save_is_still_a_200(self):
        # The control. A handler that answered 422 for everything would
        # pass both tests above, and every save would stop flashing.
        response = await rename_series(1, _request(), name="The Shorter Name",
                                       user=self.user, db=self.db)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["HX-Trigger"], "series-edit-saved")

    async def test_a_save_is_still_a_200_on_the_novel_page(self):
        response = await edit_novel_title(1, _request("/novels/1/title"), "Shorter",
                                          user=self.user, db=self.db)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["HX-Trigger"], "novel-edit-saved")

    async def test_without_htmx_a_refusal_is_still_a_redirect(self):
        # A browser posting a form follows the 302 and re-renders the
        # whole page; there is no panel to swap and no 422 to read. The
        # error rides in the query string, as it always did.
        response = await rename_series(1, _request(htmx=False), name="",
                                       user=self.user, db=self.db)
        self.assertEqual(response.status_code, 302)
        self.assertIn("edit_error=empty-name", response.headers["location"])


class EveryFormInThosePanelsOptsIn(unittest.TestCase):
    """The per-element half. `noSwap` covers "4xx" for the whole app, and
    a form that forgot this attribute would post, be refused, and show
    nothing at all."""

    def test_every_form_that_the_panel_answers_carries_the_opt_in(self):
        for name, target in PANELS.items():
            markup = (ROOT / name).read_text()
            forms = re.findall(r"<form\b[^>]*>", markup, flags=re.S)
            answered = [f for f in forms if f'hx-target="{target}"' in f]
            self.assertTrue(answered, name)
            for form in answered:
                self.assertIn("hx-status:422", form, f"{name}: {form}")

    def test_the_opt_in_carries_a_swap_to_merge(self):
        # A BARE `hx-status:422` reads as the obvious way to write this
        # and does nothing: htmx looks the attribute up, gets "" back for
        # a valueless attribute, and its `if (value)` is false -- so the
        # response falls through to noSwap and is dropped. The value is an
        # attribute spec, merged into the request context.
        for name in PANELS:
            markup = (ROOT / name).read_text()
            self.assertNotIn("hx-status:422>", markup, name)
            self.assertNotIn('hx-status:422 ', markup, name)
            self.assertIn('hx-status:422="swap:outerHTML"', markup, name)

    def test_the_checkbox_that_is_not_a_save_stays_out_of_it(self):
        # Control, and the reason the HX-Trigger event still exists: the
        # blur-cover form posts from inside the series panel, answers 200
        # and swaps nothing. It is not an edit and must not opt in.
        markup = (ROOT / "app/templates/library/_series_edit.html").read_text()
        blur = [f for f in re.findall(r"<form\b[^>]*>", markup, flags=re.S)
                if "blur-cover" in f]
        self.assertEqual(len(blur), 1)
        self.assertNotIn("hx-status", blur[0])
