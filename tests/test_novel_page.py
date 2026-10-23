"""A novel's own page: what it counts, and what it refuses to load.

The counts are what the page gained -- it used to say nothing at all about
how far in you are -- and the column list is what it stopped paying for.
Both are asserted against the context the real handler builds rather than
against rendered HTML, so a template rewrite does not break them and a
query rewrite does.
"""
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.http_utils import safe_redirect_target
from app.novels.router import (
    edit_novel_synopsis, edit_novel_title, novel_detail, reset_novel_synopsis,
    reset_novel_title, templates, toggle_novel_active,
)
from app.novels.sync import reconcile_editable_metadata


def _request(path="/novels/1", *, htmx=False):
    """The minimum a TemplateResponse needs. These handlers never call
    url_for, so nothing here has to know about the app's routing table.

    `htmx` is the header the edit endpoints branch on: over htmx they
    answer with the panel, without it they redirect."""
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"hx-request", b"true")] if htmx else [],
        "client": ("test", 1), "server": ("test", 80),
    })


class NovelPageCase(unittest.IsolatedAsyncioTestCase):
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
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url, sync_status, active)"
            " VALUES (1, 'N', 'https://x/1', 'idle', 1)")
        await self.db.commit()
        self._next_chapter = 1

    async def _close(self):
        await self.db.close()

    async def volume(self, volume_id, number):
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (?, 1, ?)",
                              (volume_id, number))
        await self.db.commit()
        return volume_id

    async def chapter(self, volume_id, *, read=False, downloaded=True):
        n = self._next_chapter
        self._next_chapter += 1
        # content_size alongside the text, the way every write in the app
        # does it -- a fixture that skipped it would model a chapter the
        # app cannot produce, and the manifest would report it as weighing
        # nothing.
        body = "<p>text</p>" if downloaded else None
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url, content_html,"
            " content_size, order_index) VALUES (?, 1, ?, ?, ?, ?, ?, LENGTH(?), ?)",
            (n, volume_id, float(n), f"C{n}", f"https://x/c{n}", body, body, n))
        if read:
            await self.db.execute(
                "INSERT INTO novel_read_chapters (user_id, chapter_id) VALUES (1, ?)", (n,))
        await self.db.commit()
        return n

    async def context(self):
        response = await novel_detail(1, _request(), None, self.user, self.db)
        return response.context


class Counts(NovelPageCase):
    async def test_it_counts_read_chapters_over_the_whole_novel(self):
        await self.volume(1, 1)
        await self.volume(2, 2)
        for _ in range(3):
            await self.chapter(1, read=True)
        await self.chapter(1)
        await self.chapter(2, read=True)
        ctx = await self.context()
        self.assertEqual((ctx["chapters_read"], ctx["chapters_total"]), (4, 5))

    async def test_each_volume_counts_only_its_own(self):
        # What lets a COLLAPSED volume say how far in it you are. A count
        # taken over the novel would read the same on every row.
        await self.volume(1, 1)
        await self.volume(2, 2)
        await self.chapter(1, read=True)
        await self.chapter(1, read=True)
        await self.chapter(2, read=True)
        await self.chapter(2)
        await self.chapter(2)
        ctx = await self.context()
        self.assertEqual(ctx["volume_read_counts"], {1: 2, 2: 1})

    async def test_a_volume_is_all_read_only_when_every_chapter_is(self):
        await self.volume(1, 1)
        await self.volume(2, 2)
        await self.chapter(1, read=True)
        await self.chapter(2, read=True)
        await self.chapter(2)
        ctx = await self.context()
        self.assertEqual(ctx["volume_all_read"], {1: True, 2: False})

    async def test_an_empty_volume_is_not_all_read(self):
        # `all()` over nothing is True, which would have put "Mark all as
        # unread" on a volume holding no chapters at all.
        await self.volume(1, 1)
        ctx = await self.context()
        self.assertEqual(ctx["volume_all_read"], {1: False})
        self.assertEqual(ctx["volume_read_counts"], {1: 0})

    async def test_a_chapter_read_in_another_novel_is_not_counted(self):
        # Guards the READ-IDS QUERY's join, which is where the scoping
        # actually happens -- the count downstream is a plain len().
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url) VALUES (2, 'Other', 'https://x/2')")
        await self.db.execute("INSERT INTO volumes (id, novel_id, number) VALUES (99, 2, 1)")
        await self.db.execute(
            "INSERT INTO chapters (id, novel_id, volume_id, number, title, url, order_index)"
            " VALUES (999, 2, 99, 1.0, 'C', 'https://x/o', 1)")
        await self.db.execute(
            "INSERT INTO novel_read_chapters (user_id, chapter_id) VALUES (1, 999)")
        await self.db.commit()
        await self.volume(1, 1)
        await self.chapter(1)
        ctx = await self.context()
        self.assertEqual((ctx["chapters_read"], ctx["chapters_total"]), (0, 1))


class ChapterColumns(NovelPageCase):
    async def test_it_never_loads_chapter_text(self):
        # The page renders numbers, titles and dates. Pulling content_html
        # too measured 16.3 ms and 6.5 MB per request on a 900-chapter
        # novel against 5.6 ms and 0.21 MB, and both figures grow with the
        # novel. A `SELECT *` put back here would pass every other test.
        await self.volume(1, 1)
        await self.chapter(1)
        row = (await self.context())["chapters_by_volume"][1][0]
        self.assertNotIn("content_html", row.keys())

    async def test_whether_a_chapter_has_text_still_comes_back(self):
        # As a flag, not as the text: a chapter the sync has not fetched
        # yet is the one that opens empty, and its own row says so.
        await self.volume(1, 1)
        await self.chapter(1, downloaded=True)
        await self.chapter(1, downloaded=False)
        ctx = await self.context()
        self.assertEqual([bool(c["downloaded"]) for c in ctx["chapters_by_volume"][1]],
                         [True, False])


class TheChapterBodyIsCacheable(NovelPageCase):
    """A chapter body is the largest thing this app serves per request, and
    it changes only when a sync rewrites it -- but it carried no validator
    at all, so every revisit downloaded the whole chapter again. The EPUB
    half of the same reader engine has answered 304 all along."""

    async def _resource(self, chapter_id, if_none_match=None):
        from app.novels.router import novel_resource
        headers = {"if-none-match": if_none_match} if if_none_match else {}
        request = Request({
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "client": ("test", 1), "server": ("test", 80),
        })
        return await novel_resource(1, chapter_id, request, self.db, self.user)

    async def test_a_chapter_comes_back_with_a_validator(self):
        await self.volume(1, 1)
        chapter_id = await self.chapter(1)
        response = await self._resource(chapter_id)
        self.assertTrue(response.headers["etag"])
        self.assertIn("max-age", response.headers["cache-control"])

    async def test_asking_again_with_it_is_a_304(self):
        await self.volume(1, 1)
        chapter_id = await self.chapter(1)
        etag = (await self._resource(chapter_id)).headers["etag"]
        again = await self._resource(chapter_id, if_none_match=etag)
        self.assertEqual(again.status_code, 304)

    async def test_a_rewritten_chapter_gets_a_new_one(self):
        # The whole point: the validator has to MOVE when a sync replaces
        # the text, or the reader keeps the old chapter.
        await self.volume(1, 1)
        chapter_id = await self.chapter(1)
        before = (await self._resource(chapter_id)).headers["etag"]
        await self.db.execute(
            "UPDATE chapters SET content_html = ?, content_size = LENGTH(?),"
            " content_fetched_at = '2026-02-02 02:02' WHERE id = ?",
            ("<p>rewritten, and longer than it was</p>",
             "<p>rewritten, and longer than it was</p>", chapter_id))
        await self.db.commit()
        self.assertNotEqual((await self._resource(chapter_id)).headers["etag"], before)


class TheManifestDoesNotReadTheText(NovelPageCase):
    """`size` weights the reader's progress bar, and it used to be
    LENGTH(content_html) -- which on a TEXT column reads the whole value.
    Measured on 900 chapters of ~6 KB: 4.8 ms and 5.4 MB per open of the
    reader, against 1.8 ms for the column written beside the text."""

    async def test_it_reports_the_stored_length(self):
        from app.novels.router import novel_manifest
        await self.volume(1, 1)
        chapter_id = await self.chapter(1)
        body = "<p>" + ("x" * 500) + "</p>"
        await self.db.execute(
            "UPDATE chapters SET content_html = ?, content_size = LENGTH(?) WHERE id = ?",
            (body, body, chapter_id))
        await self.db.commit()
        manifest = await novel_manifest(1, self.user, self.db)
        self.assertEqual(manifest["spine"][0]["size"], len(body))

    async def test_a_chapter_with_no_text_yet_weighs_nothing(self):
        # The reader gives those the average weight of the ones it knows,
        # so the honest answer is 0 rather than a guess.
        from app.novels.router import novel_manifest
        await self.volume(1, 1)
        await self.chapter(1, downloaded=False)
        manifest = await novel_manifest(1, self.user, self.db)
        self.assertEqual(manifest["spine"][0]["size"], 0)


class ReturnTo(NovelPageCase):
    """The sync endpoints send whoever posted to them back where they
    were. /settings is the only page carrying those forms now, but the
    parameter is still honoured for any caller that sends one."""

    async def test_it_comes_back_to_whatever_asked(self):
        response = await toggle_novel_active(1, "/novels/1", self.user)
        self.assertEqual(response.headers["location"], "/novels/1")
        row = await (await self.db.execute("SELECT active FROM novels WHERE id = 1")).fetchone()
        self.assertEqual(row["active"], 0)

    async def test_without_one_it_still_goes_to_settings(self):
        # The settings list posts these forms with no return_to, and must
        # keep landing back on the page it was submitted from.
        response = await toggle_novel_active(1, None, self.user)
        self.assertEqual(response.headers["location"], "/settings")

    async def test_a_hostile_return_to_is_dropped_at_the_endpoint(self):
        # The guard existing is not the same as the endpoint applying it.
        # This is `return_to` arriving as a form field, so it is whatever
        # the request said -- a link mailed to the reader included.
        for hostile in ("//evil.example/steal", "https://evil.example"):
            response = await toggle_novel_active(1, hostile, self.user)
            self.assertEqual(response.headers["location"], "/settings")


class RedirectGuard(unittest.TestCase):
    """The open-redirect guard, now shared by the reader's mark-read forms
    and the novel page's sync buttons."""

    def test_it_accepts_a_path_inside_this_app(self):
        self.assertEqual(safe_redirect_target("/novels/1"), "/novels/1")
        self.assertEqual(safe_redirect_target("/?library=novels"), "/?library=novels")

    def test_it_refuses_another_origin(self):
        self.assertIsNone(safe_redirect_target("https://evil.example/steal"))
        self.assertIsNone(safe_redirect_target("http://evil.example"))

    def test_it_refuses_a_protocol_relative_url(self):
        # "//evil.example" is a PATH to the eye and another origin to the
        # browser, which is the whole trick.
        self.assertIsNone(safe_redirect_target("//evil.example/steal"))

    def test_it_refuses_nothing_at_all(self):
        self.assertIsNone(safe_redirect_target(None))
        self.assertIsNone(safe_redirect_target(""))


if __name__ == "__main__":
    unittest.main()


class Editing(NovelPageCase):
    """Renaming a novel, and rewriting its description, from its own page.

    Both edits go through the panel the pencil opens, which submits over
    htmx and is answered with itself -- so these assert on what came back
    as well as on what was stored: a save that does not report itself
    leaves the reader wondering, and a rejection that reports success is
    worse.
    """

    async def _novel(self):
        return await (
            await self.db.execute(
                "SELECT title, synopsis, source_title, source_synopsis FROM novels WHERE id = 1")
        ).fetchone()

    async def _source(self, title="Source Name", synopsis="Source blurb"):
        await self.db.execute(
            "UPDATE novels SET source_title = ?, source_synopsis = ? WHERE id = 1", (title, synopsis))
        await self.db.commit()

    async def test_a_new_title_is_saved_and_reported_as_saved(self):
        response = await edit_novel_title(1, _request(htmx=True), "The Shorter Name", self.user, self.db)
        self.assertEqual((await self._novel())["title"], "The Shorter Name")
        # The panel comes back, so nothing reloads and it stays open...
        self.assertEqual(response.template.name, "novels/_novel_edit.html")
        # ...with the heading swapped out of band, since it lives outside.
        self.assertTrue(response.context["oob"])
        # ...and success is the server's word, not a guess from the body.
        self.assertEqual(response.headers["HX-Trigger"], "novel-edit-saved")

    async def test_an_empty_title_is_refused_and_says_so(self):
        # `novels.title` is NOT NULL and it is what every card, the reader
        # and the statistics label this novel by. Accepting a blank one
        # would break those pages a long way from here.
        for blank in ("", "   ", "\t\n"):
            response = await edit_novel_title(1, _request(htmx=True), blank, self.user, self.db)
            self.assertEqual((await self._novel())["title"], "N")
            self.assertIn("cannot be empty", response.context["edit_error"])
            # No event: the panel that comes back is carrying a complaint,
            # and flashing "Saved" over it would be a lie.
            self.assertNotIn("HX-Trigger", response.headers)

    async def test_a_title_pasted_out_of_a_page_is_tidied(self):
        await edit_novel_title(1, _request(htmx=True), "  Lord   of\n the Mysteries ", self.user, self.db)
        self.assertEqual((await self._novel())["title"], "Lord of the Mysteries")

    async def test_the_description_is_edited_separately_from_the_title(self):
        await edit_novel_synopsis(1, _request(htmx=True), "A boy finds a diary.", self.user, self.db)
        row = await self._novel()
        self.assertEqual(row["synopsis"], "A boy finds a diary.")
        self.assertEqual(row["title"], "N")

    async def test_an_emptied_description_is_stored_as_nothing_at_all(self):
        # Unlike the title, no description is a legitimate answer -- the
        # paragraph simply disappears -- and NULL is what the column
        # already means everywhere else.
        await edit_novel_synopsis(1, _request(htmx=True), "Something", self.user, self.db)
        await edit_novel_synopsis(1, _request(htmx=True), "   ", self.user, self.db)
        self.assertIsNone((await self._novel())["synopsis"])

    async def test_the_sources_wording_can_be_taken_back(self):
        await self._source()
        await edit_novel_title(1, _request(htmx=True), "Mine", self.user, self.db)
        await edit_novel_synopsis(1, _request(htmx=True), "Mine too", self.user, self.db)

        await reset_novel_title(1, _request(htmx=True), self.user, self.db)
        await reset_novel_synopsis(1, _request(htmx=True), self.user, self.db)
        row = await self._novel()
        self.assertEqual(row["title"], "Source Name")
        self.assertEqual(row["synopsis"], "Source blurb")

    async def test_with_nothing_scraped_yet_reset_leaves_what_is_there(self):
        # A novel added but never synced has no source wording to go back
        # to. Blanking the title on the way to nowhere would be the worst
        # possible reading of the button.
        await edit_novel_title(1, _request(htmx=True), "Mine", self.user, self.db)
        await reset_novel_title(1, _request(htmx=True), self.user, self.db)
        self.assertEqual((await self._novel())["title"], "Mine")

    async def test_without_htmx_it_is_still_a_plain_form_post(self):
        # Same property every other form on this page has: the endpoints
        # answer a redirect when htmx is absent, because re-rendering the
        # page from a POST leaves a URL that re-submits on reload.
        response = await edit_novel_title(1, _request(), "Typed by hand", self.user, self.db)
        self.assertEqual(response.headers["location"], "/novels/1")
        self.assertEqual((await self._novel())["title"], "Typed by hand")

    async def test_without_htmx_a_refusal_comes_back_on_the_url(self):
        # ...and the page opens its edit panel for it, rather than
        # reporting the problem to a closed drawer.
        response = await edit_novel_title(1, _request(), "", self.user, self.db)
        self.assertEqual(response.headers["location"], "/novels/1?edit_error=empty-title")
        ctx = (await novel_detail(1, _request(), "empty-title", self.user, self.db)).context
        self.assertIn("cannot be empty", ctx["edit_error"])

    async def test_the_panel_is_shut_when_there_is_nothing_to_report(self):
        self.assertIsNone((await self.context())["edit_error"])

    async def test_editing_a_novel_that_does_not_exist_is_a_404(self):
        with self.assertRaises(HTTPException) as caught:
            await edit_novel_title(99, _request(htmx=True), "X", self.user, self.db)
        self.assertEqual(caught.exception.status_code, 404)


class SyncAgainstAnEdit(unittest.TestCase):
    """What a sync is allowed to overwrite once the user has edited a
    field. The nightly sync runs unattended, so getting this wrong looks
    like the app forgetting an edit made that afternoon."""

    def reconcile(self, **kwargs):
        base = dict(title="Old", synopsis="Old blurb", source_title="Old",
                    source_synopsis="Old blurb", meta_title="New", meta_synopsis="New blurb")
        return reconcile_editable_metadata(**{**base, **kwargs})

    def test_an_untouched_novel_follows_the_source(self):
        self.assertEqual(self.reconcile(), ("New", "New blurb", "New", "New blurb"))

    def test_an_edited_title_survives_the_sync(self):
        title, synopsis, source_title, _ = self.reconcile(title="Mine")
        self.assertEqual(title, "Mine")
        # ...and the source's own wording keeps being recorded, which is
        # what "use the source's again" reads back.
        self.assertEqual(source_title, "New")
        # The description was not edited, so it still follows.
        self.assertEqual(synopsis, "New blurb")

    def test_an_edited_description_survives_the_sync(self):
        title, synopsis, _, source_synopsis = self.reconcile(synopsis="Mine")
        self.assertEqual(synopsis, "Mine")
        self.assertEqual(source_synopsis, "New blurb")
        self.assertEqual(title, "New")

    def test_a_novel_from_before_any_of_this_is_treated_as_unedited(self):
        # Which was true of every novel that existed then -- there was no
        # way to edit one. This first pass fills the source columns in.
        self.assertEqual(
            reconcile_editable_metadata(
                title="Old", synopsis="Old blurb", source_title=None, source_synopsis=None,
                meta_title="New", meta_synopsis="New blurb"),
            ("New", "New blurb", "New", "New blurb"))

    def test_a_scrape_that_found_nothing_erases_nothing(self):
        # A source page that came back without a synopsis is a failed read,
        # not the site saying the synopsis was deleted.
        self.assertEqual(
            self.reconcile(meta_title=None, meta_synopsis=None),
            ("Old", "Old blurb", "Old", "Old blurb"))

    def test_it_does_not_go_looking_for_an_edit_in_the_wrong_field(self):
        # An edited title must not freeze the description, and vice versa.
        _, synopsis, _, _ = self.reconcile(title="Mine")
        self.assertEqual(synopsis, "New blurb")
        title, _, _, _ = self.reconcile(synopsis="Mine")
        self.assertEqual(title, "New")


class WhereTheControlsLive(NovelPageCase):
    """The page's own chrome, asserted against the RENDERED markup.

    Everything else in this file goes through the handler's context, on
    purpose -- a template rewrite should not break a test about what the
    page counts. These four are different: they are about where a control
    is and when it exists, which is the template's answer and nothing
    else's. The one that earns its place is drag mode. The filter now
    lives in a `header_filters` block, several screens away in the source
    from the list it acts on, and "not while dragging" is a rule that
    fails SILENTLY -- a filtered list is a list with holes, and a drop
    into one hands novel-reorder.js positions computed from what is
    visible rather than from what is there.
    """

    async def _html(self, edit_error=None):
        response = await novel_detail(1, _request(), edit_error, self.user, self.db)
        return templates.env.get_template(response.template.name).render(**dict(response.context))

    async def _chapter(self):
        await self.volume(1, 1)
        await self.chapter(1)

    async def test_the_chapter_filter_sits_in_the_header(self):
        # Where a series page keeps the search over its grid. Down in the
        # body it sat between the novel's metadata and the volumes,
        # reading as part of neither.
        await self._chapter()
        html = await self._html()
        head, _, body = html.partition("</header>")
        self.assertIn('id="chapter-filter"', head)
        self.assertIn('id="chapter-unread-only"', head)
        self.assertNotIn('id="chapter-filter"', body)

    async def test_drag_mode_renders_no_filter_at_all(self):
        await self._chapter()
        await self.db.execute("UPDATE novels SET manual_sort_editing = 1 WHERE id = 1")
        await self.db.commit()
        self.assertNotIn('id="chapter-filter"', await self._html())

    async def test_a_novel_with_no_chapters_has_nothing_to_filter(self):
        self.assertNotIn('id="chapter-filter"', await self._html())

    async def test_the_scrape_block_is_gone_from_the_page(self):
        # When it was last checked, whether it is active, how much is
        # still missing, the source link and the two sync buttons are
        # /settings' job -- that page is the list of scrapes and the place
        # to act on a failing one. Two wordings of the same state is how
        # they drift apart. Asserted here because the facts came from the
        # handler's context and the buttons from forms: a revert would put
        # them back silently.
        await self._chapter()
        await self.db.execute(
            "UPDATE novels SET active = 0, last_checked_at = '2026-01-01 00:00' WHERE id = 1")
        await self.db.commit()
        html = await self._html()
        for gone in ("checked 2026-01-01", "won't auto-sync", "not downloaded yet",
                     ">source<", "Sync now", "Deactivate",
                     "/settings/novels/1/sync", "/settings/novels/1/toggle-active"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, html)
        # The per-chapter badge is about the row it is on, not the scrape,
        # and stays.
        await self.chapter(1, downloaded=False)
        self.assertIn("not downloaded", await self._html())

    async def test_the_sources_wording_shows_only_while_an_override_hides_it(self):
        # With no override the publication select's own first option
        # already reads "From the source (Ongoing)", so a badge beside it
        # saying "Ongoing at source" is the same word twice. Once
        # overridden the select no longer says it, and the badge is the
        # only thing left that does.
        await self._chapter()
        await self.db.execute("UPDATE novels SET status = 'Ongoing' WHERE id = 1")
        await self.db.commit()
        self.assertNotIn("at source", await self._html())

        await self.db.execute("UPDATE novels SET publication_status = 'hiatus' WHERE id = 1")
        await self.db.commit()
        self.assertIn("Ongoing at source", await self._html())


class TheWayBackToTheLibrary(unittest.TestCase):
    """?library=<id> has to be read in BOTH places that decide what the
    home grid shows.

    The novel page's "back to Web Novels" link sends it, and for a long
    time nothing read it -- the link landed on whatever library had been
    chosen last, which is the one thing a link naming its destination
    must not do. It is now honoured, and that answer is computed twice:
    once in index.html's pre-paint block and once in home.js. AGENTS.md
    calls that pair out by name, because a rule added to one copy and not
    the other is visible only as cards appearing on the first frame and
    then vanishing.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def _read(self, *parts):
        return (self.ROOT.joinpath(*parts)).read_text()

    def test_the_link_still_names_a_library(self):
        self.assertIn('href="/?library=novels"',
                      self._read("app", "templates", "novels", "detail.html"))

    def test_both_copies_of_the_filter_read_the_parameter(self):
        for where in (("app", "static", "js", "home.js"),
                      ("app", "templates", "library", "index.html")):
            with self.subTest(where=where[-1]):
                text = self._read(*where)
                self.assertIn('URLSearchParams', text)
                self.assertIn('.get("library")', text)
                # Guarded on a button of that name existing, or a stale
                # link filters every card away with nothing saying why.
                self.assertIn('data-library-filter="${CSS.escape(', text)
