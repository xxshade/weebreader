"""The plain dialogs: "New library" and "Edit name and cover".

Both used to unfold into the page -- a <details> in the sidebar, a hidden
panel under the series title -- and are native <dialog>s now, opened by
dialogs.js. Three things hold them together, and each fails silently:

* the scripts run ONCE per document. The pages are boosted, a whole-<body>
  swap re-executes a page's own scripts, and a document listener added per
  visit would pile up. So both live in base.html's <head> and guard
  themselves, the arrangement test_swapped_scripts.py pins for the others;
* the edit dialog is NOT what the endpoints swap -- they swap #series-edit
  inside it, so a save leaves it open with its heading and "Saved";
* "New library" refuses in words. A duplicate folder used to reach SQLite
  as a UNIQUE failure and answer 500, which htmx does not swap: the button
  did nothing at all. Now it is a 422 carrying the form, which only swaps
  because the form opts in with `hx-status:422`.

Measured in both browsers against the real build: each dialog opens modal
with the caret in its first box, Escape and ✕ and Cancel close it, a click
on the backdrop does not; a refusal leaves the dialog open with the message
and what was typed, and the address bar on "/"; a success lands on the home
page listing the new library. Choosing a cover paints it in the slot with
NO request, an unreadable file says so, and Upload is the only POST.
"""
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import LIBRARY_CREATE_ERRORS, create_library

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app/templates"
SCRIPTS = ROOT / "app/static/js"


def _template(name):
    # Comments blanked, so a scan cannot be satisfied by a sentence about
    # the markup instead of the markup.
    return re.sub(r"\{#.*?#\}", "", (TEMPLATES / name).read_text(), flags=re.S)


def _element(markup, tag, element_id):
    """The whole element with this id, nested tags of the same name
    included -- bounded, since a dialog that never closes should fail
    rather than match the rest of the file."""
    start = re.search(rf"<{tag}\b[^>]*\bid=\"{element_id}\"", markup)
    if not start:
        return None
    depth, pos = 0, start.start()
    for match in re.finditer(rf"<(/?){tag}\b", markup[pos:pos + 20000]):
        depth += -1 if match.group(1) else 1
        if depth == 0:
            end = markup.index(">", pos + match.end())
            return markup[pos:end + 1]
    return None


class TheScanCanSeeADialog(unittest.TestCase):
    """The helper above, against inline samples: a matcher that quietly
    stopped matching would leave every test below green."""

    def test_it_finds_the_element_and_stops_at_its_own_end(self):
        sample = '<dialog id="a"><dialog id="b"></dialog><p>in</p></dialog><p>out</p>'
        self.assertEqual(_element(sample, "dialog", "a"),
                         '<dialog id="a"><dialog id="b"></dialog><p>in</p></dialog>')

    def test_it_finds_nothing_when_nothing_is_there(self):
        self.assertIsNone(_element('<div id="a"></div>', "dialog", "a"))


class TheDialogsAreDialogs(unittest.TestCase):
    def test_new_library_is_a_dialog_holding_the_form(self):
        home = _template("library/index.html")
        self.assertNotIn('id="new-library"', home, "the old <details> is back")
        dialog = _element(home, "dialog", "new-library-dialog")
        self.assertIsNotNone(dialog)
        self.assertIn('{% include "library/_new_library_form.html" %}', dialog)
        self.assertIn('data-dialog-open="new-library-dialog"', home)

    def test_edit_name_and_cover_is_a_dialog_around_the_swapped_panel(self):
        page = _template("library/series.html")
        self.assertNotIn('id="series-edit-panel"', page, "the old panel is back")
        dialog = _element(page, "dialog", "series-edit-dialog")
        self.assertIsNotNone(dialog)
        self.assertIn('{% include "library/_series_edit.html" %}', dialog)
        # "Saved" is driven by an event and is mid-fade when a swap lands,
        # so it lives in the dialog and OUTSIDE what is swapped.
        self.assertIn('id="edit-saved"', dialog)
        self.assertNotIn('id="edit-saved"', _template("library/_series_edit.html"))
        self.assertIn('data-dialog-open="series-edit-dialog"', page)

    def test_edit_title_and_description_is_a_dialog_too(self):
        # The novel page's panel, the series one's twin: same shape, same
        # rule that the dialog is not what the endpoints swap.
        page = _template("novels/detail.html")
        self.assertNotIn('id="novel-edit-panel"', page, "the old panel is back")
        dialog = _element(page, "dialog", "novel-edit-dialog")
        self.assertIsNotNone(dialog)
        self.assertIn('{% include "novels/_novel_edit.html" %}', dialog)
        self.assertIn('id="novel-edit-saved"', dialog)
        self.assertNotIn('id="novel-edit-saved"', _template("novels/_novel_edit.html"))
        self.assertIn('data-dialog-open="novel-edit-dialog"', page)
        self.assertIn("data-dialog-focus", _template("novels/_novel_edit.html"))

    def test_every_dialog_has_a_way_out_for_a_pointer(self):
        # The backdrop does not close them (no `closedby`), so the ✕ is
        # the only way a pointer has; Escape is the keyboard's.
        for name, dialog_id in (("library/index.html", "new-library-dialog"),
                                ("library/series.html", "series-edit-dialog"),
                                ("novels/detail.html", "novel-edit-dialog")):
            with self.subTest(dialog_id):
                dialog = _element(_template(name), "dialog", dialog_id)
                self.assertIn("data-dialog-close", dialog)
                self.assertNotIn("closedby", dialog)

    def test_a_refusal_without_htmx_renders_the_dialog_open(self):
        for name, dialog_id, flag in (("library/index.html", "new-library-dialog", "library_form.error"),
                                      ("library/series.html", "series-edit-dialog", "edit_error"),
                                      ("novels/detail.html", "novel-edit-dialog", "edit_error")):
            with self.subTest(dialog_id):
                opening = re.search(rf'<dialog\b[^>]*id="{dialog_id}"[^>]*>', _template(name), re.S)
                self.assertIn(f"'open' if {flag}", opening.group(0))


class TheScriptsRunOncePerDocument(unittest.TestCase):
    SCRIPTS = ("dialogs.js", "cover-preview.js")

    def test_they_are_loaded_from_the_head(self):
        base = _template("base.html")
        head = base[:base.index("</head>")]
        for script in self.SCRIPTS:
            with self.subTest(script):
                self.assertIn(f"asset_url('js/{script}')", head)

    def test_no_page_loads_them_again(self):
        for path in TEMPLATES.rglob("*.html"):
            if path.name == "base.html":
                continue
            text = _template(str(path.relative_to(TEMPLATES)))
            for script in self.SCRIPTS:
                with self.subTest(page=path.name, script=script):
                    self.assertNotIn(f"js/{script}", text)

    def test_each_guards_itself(self):
        for script, flag in (("dialogs.js", "appDialogs"), ("cover-preview.js", "coverPreview")):
            with self.subTest(script):
                text = (SCRIPTS / script).read_text()
                self.assertIn(f"if (window.{flag}) return;", text)
                self.assertIn(f"window.{flag} = true;", text)

    def test_the_series_page_no_longer_binds_the_file_input_itself(self):
        # It used to add a document "change" listener from its own inline
        # script, once per boosted visit.
        self.assertNotIn('addEventListener("change"', _template("library/series.html"))


class TheCoverIsPreviewedBeforeItIsSent(unittest.TestCase):
    def test_the_file_input_names_the_slot_and_the_slot_has_both_images(self):
        panel = _template("library/_series_edit.html")
        self.assertRegex(panel, r'id="series-cover"[^>]*data-cover-preview="series-cover-slot"')
        slot = _element(panel, "div", "series-cover-slot")
        self.assertIsNotNone(slot)
        self.assertEqual(slot.count("data-cover-current"), 2,
                         "one current-cover element per branch (image, placeholder)")
        self.assertRegex(slot, r"<img data-cover-new hidden\b")
        self.assertIn("data-cover-name", panel)

    def test_the_preview_never_uploads(self):
        # An object URL of the file, and nothing that sends it: the Upload
        # button stays the only way the file leaves the browser.
        script = (SCRIPTS / "cover-preview.js").read_text()
        code = re.sub(r"//[^\n]*", "", script)
        self.assertIn("URL.createObjectURL(file)", code)
        self.assertIn("URL.revokeObjectURL(shown)", code)
        for sender in ("fetch(", "XMLHttpRequest", "htmx.ajax", ".submit(", "requestSubmit"):
            self.assertNotIn(sender, code)


def _request(*, htmx=True):
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/libraries", "raw_path": b"/libraries", "query_string": b"", "root_path": "",
        "headers": [(b"hx-request", b"true")] if htmx else [],
        "client": ("test", 1), "server": ("test", 80),
    })


class ANewLibraryIsRefusedInWords(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.folder = Path(self._tmp.name) / "books"
        self.folder.mkdir()
        self.other = Path(self._tmp.name) / "comics"
        self.other.mkdir()
        db = await get_connection()
        await db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x')")
        await db.execute("INSERT INTO libraries (name, root_path, media_type)"
                         " VALUES ('Taken', ?, 'comic')", (str(self.other),))
        await db.commit()
        await db.close()
        # A real create starts a scan and a watcher; neither is what these
        # tests are about, and both would outlive them.
        # Consume the coroutine passed to the disabled scheduler. Mocking
        # both functions independently leaves an unawaited AsyncMock call.
        scheduler = mock.patch("app.library.router._run_background", side_effect=lambda coro: coro.close())
        scheduler.start()
        self.addCleanup(scheduler.stop)
        for target in ("app.library.router.watcher", "app.library.router.scan_library"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def create(self, name="Books", path=None, media_type="ebook", *, htmx=True):
        return await create_library(
            _request(htmx=htmx), name=name,
            root_path=str(self.folder) if path is None else path,
            media_type=media_type, user={"id": 1})

    async def libraries(self):
        db = await get_connection()
        try:
            return [r["name"] for r in await (await db.execute(
                "SELECT name FROM libraries ORDER BY id")).fetchall()]
        finally:
            await db.close()

    async def test_each_refusal_is_a_422_carrying_the_form(self):
        for label, kwargs, code in (
            ("blank name", {"name": "  "}, "empty"),
            ("blank path", {"path": " "}, "empty"),
            ("a folder another library reads", {"path": None}, "taken"),
            ("no folder there", {"path": "/nowhere/at/all"}, "not-a-folder"),
            ("unknown contents", {"media_type": "films"}, "media-type"),
        ):
            with self.subTest(label):
                if code == "taken":
                    kwargs = {"path": str(self.other)}
                response = await self.create(**kwargs)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.template.name, "library/_new_library_form.html")
                self.assertEqual(response.context["library_form"]["error"],
                                 LIBRARY_CREATE_ERRORS[code])
        self.assertEqual(await self.libraries(), ["Taken"], "a refusal wrote a row")

    async def test_what_was_typed_comes_back(self):
        response = await self.create(name="My books", path="/nowhere", media_type="mixed")
        form = response.context["library_form"]
        self.assertEqual((form["name"], form["path"], form["media_type"]),
                         ("My books", "/nowhere", "mixed"))

    async def test_the_form_opts_in_to_swapping_the_422_in_place(self):
        # Without this, base.html's noSwap drops the 422 on the floor and
        # the refusal is silent again. The target is named because the
        # form is boosted (its default target is the body), and the push
        # is off because a boosted form pushes its action -- measured, a
        # refusal left /libraries in the address bar, which 405s a reload.
        form = _template("library/_new_library_form.html")
        self.assertIn('id="new-library-form"', form)
        self.assertIn('hx-status:422="swap:outerHTML target:#new-library-form push:false"', form)

    async def test_without_htmx_a_refusal_is_a_redirect_home_with_the_message(self):
        response = await self.create(path=str(self.other), htmx=False)
        self.assertEqual(response.status_code, 302)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(response.headers["location"]).query)
        self.assertEqual(query["library_error"], ["taken"])
        self.assertEqual(query["library_path"], [str(self.other)])

    async def test_a_good_one_is_created(self):
        # The control: a handler refusing everything passes every test above.
        response = await self.create(name="  Books  ")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(await self.libraries(), ["Taken", "Books"])

    async def test_an_unreadable_path_is_not_a_no(self):
        # "Unknown is not a no" (AGENTS.md): a permission error on the way
        # is not evidence the folder is missing.
        with mock.patch("app.library.router.Path.is_dir", side_effect=PermissionError):
            response = await self.create()
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
