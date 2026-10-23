"""A series that runs out of files keeps what the reader put into it.

The last item leaving used to delete the series row, because an empty
series would otherwise sit on the home page forever. That is a real
problem, but deleting the row is a much bigger hammer: `series_status`,
`tracker_links` and `text_replacements` all cascade off it. Reading a
series to the end and clearing the files therefore took the tracking link
with them -- and with it `last_pushed_chapter`/`last_pushed_volume`, so
the next push would start asking the service from scratch -- along with
the name, the uploaded cover, the description, the blur, the publication
status and any manual order.

The rule now: keep the row when somebody invested something in it, delete
it when it holds nothing but what the scanner itself created. What makes
that safe is that an empty series is listed nowhere (the grids, the search
and the statistics all filter on `series_scope.has_items`), and what makes
it worth doing is that `UNIQUE(library_id, folder_path)` means one chapter
landing back in the folder finds all of it again.
"""
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.config import settings
from app.database import get_connection, init_db
from app.library import scanner
from app.library.series_scope import has_items
from app.stats import queries

ROOT = Path(__file__).resolve().parent.parent


class EmptySeriesCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        await self.db.execute("INSERT INTO users (id,username,password_hash) VALUES (1,'u','x')")
        await self.db.execute(
            "INSERT INTO libraries (id,name,root_path,media_type) VALUES (1,'L','/m','comic')")
        await self.db.execute(
            "INSERT INTO series (id,library_id,name,sort_name,folder_path)"
            " VALUES (1,1,'Serie','serie','Serie')")
        await self.db.execute(
            "INSERT INTO items (id,library_id,series_id,title,sort_title,file_path,file_format,"
            "file_hash,file_size) VALUES (1,1,1,'V1','v1','/m/Serie/v1.cbz','cbz','h1',1)")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def empty_it(self):
        await scanner._delete_item(self.db, 1)
        await self.db.commit()

    def library_with_another_series(self) -> Path:
        """A library root holding one other series, so emptying "Serie" is
        a folder emptied -- not the whole library gone, which a scan refuses
        to reconcile (see test_scan_root_guard.py)."""
        library_root = Path(self._tmp.name) / "lib"
        (library_root / "Other").mkdir(parents=True)
        with zipfile.ZipFile(library_root / "Other" / "o1.cbz", "w") as z:
            z.writestr("001.txt", "x")
        return library_root

    async def series_row(self):
        return await (await self.db.execute("SELECT * FROM series WHERE id = 1")).fetchone()

    async def link(self):
        await self.db.execute(
            "INSERT INTO tracker_links (user_id,tracker,series_id,remote_id,remote_title,remote_url,"
            "last_pushed_chapter) VALUES (1,'mangaupdates',1,'9','T','http://x',42)")
        await self.db.commit()

    # ---------- kept ----------

    async def test_a_tracking_link_keeps_the_series_and_what_was_pushed(self):
        await self.link()
        await self.empty_it()
        self.assertIsNotNone(await self.series_row())
        row = await (await self.db.execute(
            "SELECT last_pushed_chapter FROM tracker_links WHERE series_id = 1")).fetchone()
        # The number the service was last told. Losing it is not cosmetic:
        # the next push re-asks the service about a series it already knows.
        self.assertEqual(row["last_pushed_chapter"], 42)

    async def test_a_reading_status_keeps_it(self):
        await self.db.execute(
            "INSERT INTO series_status (user_id,series_id,status) VALUES (1,1,'reading')")
        await self.db.commit()
        await self.empty_it()
        self.assertIsNotNone(await self.series_row())

    async def test_text_replacements_keep_it(self):
        await self.db.execute(
            "INSERT INTO text_replacements (user_id,series_id,find_text,replace_text)"
            " VALUES (1,1,'a','b')")
        await self.db.commit()
        await self.empty_it()
        self.assertIsNotNone(await self.series_row())

    async def test_a_typed_name_keeps_it(self):
        # No "overridden" flag exists for the name and none is needed: the
        # automatic value is the folder's last component, so anything else
        # was typed.
        await self.db.execute("UPDATE series SET name = 'O nome certo' WHERE id = 1")
        await self.db.commit()
        await self.empty_it()
        self.assertIsNotNone(await self.series_row())

    async def test_every_edit_on_the_series_page_keeps_it(self):
        # One series per column, all emptied in the same pass: re-running
        # setUp per case would leave a connection per case open on a
        # database each one replaces.
        columns = (("summary", "'uma descricao'"), ("cover_overridden", "1"),
                   ("blur_cover", "1"), ("manual_sort", "1"),
                   ("publication_status", "'completed'"))
        for n, (column, value) in enumerate(columns, start=2):
            await self.db.execute(
                "INSERT INTO series (id,library_id,name,sort_name,folder_path)"
                " VALUES (?,1,?,?,?)", (n, f"S{n}", f"s{n}", f"S{n}"))
            await self.db.execute(f"UPDATE series SET {column} = {value} WHERE id = ?", (n,))
            await self.db.execute(
                "INSERT INTO items (id,library_id,series_id,title,sort_title,file_path,"
                "file_format,file_hash,file_size) VALUES (?,1,?,?,?,?,'cbz',?,1)",
                (n, n, f"V{n}", f"v{n}", f"/m/S{n}/v.cbz", f"h{n}"))
        await self.db.commit()
        for n, (column, _) in enumerate(columns, start=2):
            await scanner._delete_item(self.db, n)
        await self.db.commit()
        left = {r["id"] for r in await (await self.db.execute("SELECT id FROM series")).fetchall()}
        missing = [c for n, (c, _) in enumerate(columns, start=2) if n not in left]
        self.assertEqual(missing, [], "an edited series was deleted when it emptied")

    # ---------- deleted ----------

    async def test_a_series_nobody_touched_is_still_deleted(self):
        # Nothing but what the scanner made: keeping it would be hoarding.
        await self.empty_it()
        self.assertIsNone(await self.series_row())

    # ---------- not on the shelf while empty ----------

    async def test_an_empty_series_is_off_the_grid_and_the_stats(self):
        await self.link()
        await self.empty_it()
        rows = await (await self.db.execute(
            f"SELECT id FROM series WHERE {has_items()}")).fetchall()
        self.assertEqual(rows, [])
        self.assertEqual(await queries.series_by_status(self.db, 1), [])

    async def test_the_automatic_cover_goes_with_the_last_item(self):
        # It was the departing item's thumbnail, and that file is unlinked
        # -- so keeping the pointer would name something that is not there.
        await self.link()
        await self.db.execute("UPDATE series SET cover_path = '1-abc.webp' WHERE id = 1")
        await self.db.commit()
        await self.empty_it()
        self.assertIsNone((await self.series_row())["cover_path"])

    async def test_the_scan_that_finds_the_folder_empty_keeps_it_too(self):
        """The path a reader actually takes, and the one a unit test on
        _delete_item alone does not reach.

        Emptying a folder and rescanning is how a series gets cleared out
        -- a downloader removing chapters it manages, or a tidy-up by hand
        -- and scan_library's reconciliation had its OWN unconditional
        `DELETE FROM series` after removing the items, which threw away
        the link `_delete_item` had just preserved. Found end to end on a
        running server, with the tracker link gone after one rescan.
        """
        await self.link()
        library_root = self.library_with_another_series()
        (library_root / "Serie").mkdir(parents=True)
        await self.db.execute("UPDATE libraries SET root_path = ? WHERE id = 1", (str(library_root),))
        await self.db.commit()

        await scanner.scan_library(1, str(library_root))

        self.assertIsNotNone(await self.series_row(), "the rescan deleted a tracked series")
        row = await (await self.db.execute(
            "SELECT last_pushed_chapter FROM tracker_links WHERE series_id = 1")).fetchone()
        self.assertEqual(row["last_pushed_chapter"], 42)
        self.assertEqual(
            (await (await self.db.execute(
                "SELECT COUNT(*) AS n FROM items WHERE series_id = 1")).fetchone())["n"], 0)

    async def test_that_scan_still_removes_a_series_nobody_touched(self):
        library_root = self.library_with_another_series()
        (library_root / "Serie").mkdir(parents=True)
        await self.db.execute("UPDATE libraries SET root_path = ? WHERE id = 1", (str(library_root),))
        await self.db.commit()
        await scanner.scan_library(1, str(library_root))
        self.assertIsNone(await self.series_row())

    # ---------- and back again ----------

    async def test_a_chapter_landing_back_in_the_folder_finds_it_all(self):
        await self.link()
        await self.db.execute(
            "INSERT INTO series_status (user_id,series_id,status) VALUES (1,1,'reading')")
        await self.db.commit()
        await self.empty_it()

        root = Path("/m")
        series_id, appeared = await scanner._get_or_create_series(
            self.db, 1, root, root / "Serie" / "v2.cbz")
        self.assertEqual(series_id, 1)
        # `appeared` is what publishes series_added: the row already
        # existed, so keying that event on the INSERT would leave the card
        # missing from every open grid until a reload.
        self.assertTrue(appeared)
        status = await (await self.db.execute(
            "SELECT status FROM series_status WHERE series_id = 1")).fetchone()
        self.assertEqual(status["status"], "reading")

    async def test_a_series_that_still_has_items_does_not_claim_to_have_appeared(self):
        root = Path("/m")
        _, appeared = await scanner._get_or_create_series(
            self.db, 1, root, root / "Serie" / "v2.cbz")
        self.assertFalse(appeared)


class TheClauseIsUsedEverywhere(unittest.TestCase):
    """Two places list or count series, and a third would be a bug.

    They have to agree about what is on the shelf, so they read the clause
    from one place. This is what says so out loud -- a new listing that
    writes its own `FROM series` is exactly how an empty series starts
    showing up on one screen and not another.
    """

    def test_every_series_listing_filters_on_the_shared_clause(self):
        for path in ("app/library/router.py", "app/stats/queries.py"):
            source = (ROOT / path).read_text()
            with self.subTest(path=path):
                self.assertRegex(source, r"series_scope import [\w, ]*\bhas_items\b")
                for match in re.finditer(r"FROM series\b", source):
                    tail = source[match.end():match.end() + 300]
                    # Fetching ONE series by id is not a listing: a series
                    # page has to keep working while the series is empty,
                    # since that is where the tracker link lives.
                    if re.match(r"(\s+\w+)?\s+WHERE id = \?", tail):
                        continue
                    self.assertTrue(
                        "has_items(" in tail or "JOIN items" in tail,
                        f"{path}: a series listing that does not filter on has_items():\n{tail[:200]}")


if __name__ == "__main__":
    unittest.main()
