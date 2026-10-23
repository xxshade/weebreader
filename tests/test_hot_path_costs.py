"""Things the app must not read just to throw away.

Every case here is the same mistake in a different place: a query that
carries a chapter's whole text, or a table's widest columns, across the
connection to compute one number or render one card. They are invisible in
a test suite that only checks answers -- the answer is right, it just cost
a hundred times what it needed to -- so what is pinned is the SHAPE of the
query, and each one names the measurement that made it worth pinning.

The novel detail page found this first (16.3 ms and 6.5 MB per request
against 5.6 ms and 0.21 MB, on a 900-chapter novel) and its own test lives
in test_novel_page.py. These are the places that had the same bug and were
missed.
"""
import re
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import close_pool, get_connection, get_db, init_db

APP = Path(__file__).resolve().parent.parent / "app"


class NoChapterTextIsReadForANumber(unittest.TestCase):
    """`LENGTH(content_html)` is not free.

    SQLite can answer LENGTH() from the stored size only for a BLOB; for
    TEXT it counts characters, which means reading the whole value out of
    its overflow pages. The manifest asked for it once per chapter on every
    open of the reader, and the reading-rate learning asked for it again
    over every chapter it had ever timed. Measured on 900 chapters of ~6 KB:
    4.8 ms and 5.4 MB, against 1.8 ms for the column kept beside the text.
    """

    def sources(self):
        """Every module, with comment lines blanked out.

        These scans are looking for CODE. Leaving the prose in means a note
        explaining why the column exists reads as the mistake it describes
        -- which it did, on main.py's own comment about the backfill.
        """
        out = {}
        for path in APP.rglob("*.py"):
            lines = path.read_text().splitlines()
            out[path] = "\n".join("" if line.lstrip().startswith("#") else line for line in lines)
        return out

    def test_nothing_measures_a_chapter_at_query_time(self):
        offenders = []
        for path, source in self.sources().items():
            for match in re.finditer(r"LENGTH\((?:chapters\.)?content_html\)", source):
                line = source[: match.start()].count("\n") + 1
                # The backfill is the one place allowed to: it is what
                # fills the column in, once, for chapters scraped before it
                # existed.
                if path.name == "sync.py" and "content_size = LENGTH(content_html)" in source[
                        match.start() - 40: match.end() + 10]:
                    continue
                offenders.append(f"{path.relative_to(APP.parent)}:{line}")
        self.assertEqual(offenders, [], f"content_html measured at query time in {offenders}")

    def test_every_write_of_the_text_writes_the_length_with_it(self):
        # The column is only trustworthy if it cannot drift. A write that
        # sets content_html and forgets content_size leaves a chapter
        # weighing zero -- which the reader spreads over the others as the
        # average, and the rate learning silently drops.
        offenders = []
        for path, source in self.sources().items():
            for match in re.finditer(r"SET content_html = \?[^\"']*", source):
                if "content_size" not in match.group(0):
                    line = source[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(APP.parent)}:{line}")
        self.assertEqual(offenders, [], f"content_html written without its length in {offenders}")

    def test_the_sync_asks_whether_there_is_text_not_for_the_text(self):
        # It only needs to know which chapters still need downloading.
        # Selecting the column read every chapter of the novel into memory
        # on every sync -- 5.4 MB for the 900-chapter case above.
        source = (APP / "novels/sync.py").read_text()
        self.assertIn("content_html IS NOT NULL AS has_content", source)
        self.assertNotIn("SELECT id, url, content_html FROM chapters", source)


class NoCardReadsWhatItDoesNotRender(unittest.TestCase):
    """`SELECT *` on the home grid also carried `summary` and
    `source_summary` -- the description a tracker fills in, a paragraph or
    several -- for every series, to be dropped on the floor. Measured on
    400 series with 1.4 KB descriptions: 5.7 ms and ~1.1 MB read, against
    3.5 ms."""

    def test_the_grid_names_its_columns(self):
        source = (APP / "library/router.py").read_text()
        self.assertIn("SERIES_CARD_COLUMNS", source)
        self.assertIn("NOVEL_CARD_COLUMNS", source)
        # The GRID specifically -- one row's worth of extra columns, which
        # is what `SELECT *` costs on a single series page, is nothing.
        # Four hundred of them on one screen is the whole point.
        self.assertNotIn("SELECT * FROM series WHERE {has_items()}", source)
        self.assertNotIn('f"SELECT * FROM series WHERE', source)

    def test_the_columns_are_the_ones_the_card_actually_reads(self):
        # The list is written out by hand, so this is what keeps it honest:
        # every `row["..."]` inside the two card builders has to be in it.
        source = (APP / "library/router.py").read_text()
        for builder, constant in (("_series_card", "SERIES_CARD_COLUMNS"),
                                  ("_novel_card", "NOVEL_CARD_COLUMNS")):
            # Up to the NEXT definition, not to the next route: the two
            # builders sit side by side, so stopping at `@router` put the
            # novel card's body inside the series card's.
            body = re.split(r"\n(?:@router|def |async def )",
                            source.split(f"def {builder}(")[1])[0]
            declared = re.search(rf"{constant} = \(\s*\n?\s*\"([^\"]+)\"", source).group(1)
            columns = {c.strip() for c in declared.split(",")}
            for used in set(re.findall(r'row\["(\w+)"\]', body)):
                with self.subTest(builder=builder, column=used):
                    self.assertIn(used, columns)


class TheReaderDoesNotBlockTheLoop(unittest.TestCase):
    """Zip reads and Pillow decodes are blocking calls, and an `async def`
    handler runs ON the event loop -- so a page turn froze the whole app,
    SSE and progress saves included, for as long as it took. Measured with
    four page loads in flight: an unrelated endpoint went from 3.9 ms to
    37.9 ms median (p95 49.9), and to 16.3 ms (p95 26.9) once the read was
    handed to a thread.

    The scanner and the thumbnail pipeline already did this. The reader,
    which is the hottest path in the app, was the one that did not.
    """

    def test_every_archive_read_in_a_request_goes_to_a_thread(self):
        source = (APP / "reader/router.py").read_text()
        for call in ("comic_page_bytes", "comic_all_page_dimensions",
                     "epub_resource_bytes", "list_spine_resources"):
            with self.subTest(call=call):
                self.assertRegex(source, rf"asyncio\.to_thread\(\s*\n?\s*(streaming\.)?{call}")

    def test_no_page_is_measured_on_the_way_out(self):
        # /dimensions has every page's size, computed once and cached in
        # the database, and that is where the reader reads them from.
        # Opening each page in Pillow as it was served measured the same
        # numbers again, per page turn.
        source = (APP / "reader/router.py").read_text()
        self.assertNotIn("X-Page-Dimensions", source)
        self.assertNotIn("from PIL import Image", source)


class TheSpansAreOnlyEverAppendedTo(unittest.TestCase):
    """What the reading-totals rollup rests on.

    `item_read_totals` and `chapter_read_totals` are maintained by an
    AFTER INSERT trigger on page_reads (see schema.sql), which covers every
    write this app makes to that table -- because an insert is every write
    this app makes to that table. A DELETE or an UPDATE would slip past the
    trigger silently: no error, just an estimate built from time that is no
    longer there.

    So this is the assumption, pinned. If a future change genuinely needs
    to delete or rewrite a span, the rollup needs a trigger for it too, and
    this test is where that gets noticed.
    """

    def test_nothing_deletes_or_rewrites_a_recorded_span(self):
        offenders = []
        for path in APP.rglob("*.py"):
            source = "\n".join(
                "" if line.lstrip().startswith("#") else line
                for line in path.read_text().splitlines()
            )
            for match in re.finditer(r"(DELETE FROM|UPDATE)\s+page_reads\b", source):
                line = source[: match.start()].count("\n") + 1
                # database.py's one-time migration rebuilds the table
                # wholesale, under its own DROP/RENAME, and the rollup is
                # backfilled after it by the migration that follows.
                if path.name == "database.py":
                    continue
                offenders.append(f"{path.relative_to(APP.parent)}:{line}")
        self.assertEqual(offenders, [], f"page_reads written other than by INSERT in {offenders}")

    def test_the_trigger_is_what_maintains_the_totals(self):
        # Not record_spans. A rollup maintained by the application would be
        # wrong in every test fixture in this suite -- they insert spans
        # directly -- and one forgotten call away from being wrong in
        # production, with nothing to say so.
        schema = (APP / "schema.sql").read_text()
        self.assertIn("CREATE TRIGGER IF NOT EXISTS page_reads_read_totals", schema)
        self.assertIn("AFTER INSERT ON page_reads", schema)
        recording = (APP / "stats/recording.py").read_text()
        body = recording.split("async def record_spans(")[1].split("\nasync def ")[0]
        for table in ("item_read_totals", "chapter_read_totals"):
            with self.subTest(table=table):
                self.assertNotIn(table, body)


class ThePoolHandsOutReadConnections(unittest.IsolatedAsyncioTestCase):
    """One connection per request meant `aiosqlite.connect` -- which starts
    a thread -- plus three PRAGMAs plus a close, on every request. Measured
    at 0.95 ms median and 1.95 ms p90, against 0.124 ms for a query on a
    connection already open."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await close_pool()
        self.addCleanup(close_pool)
        await init_db()

    async def _borrow(self):
        agen = get_db()
        return agen, await agen.__anext__()

    async def _give_back(self, agen):
        with self.assertRaises(StopAsyncIteration):
            await agen.__anext__()

    async def test_the_same_connection_comes_back_for_the_next_request(self):
        agen, first = await self._borrow()
        await self._give_back(agen)
        agen, second = await self._borrow()
        await self._give_back(agen)
        self.assertIs(first, second)

    async def test_a_pooled_connection_refuses_to_write(self):
        # The invariant the whole pool rests on: every write in this app
        # goes through write_transaction() on its own connection. sqlite3
        # opens an implicit transaction on DML, so one forgotten write on a
        # pooled connection would leave it open forever and the next
        # borrower would inherit it. PRAGMA query_only is what makes that
        # impossible rather than merely discouraged.
        agen, db = await self._borrow()
        try:
            with self.assertRaises(Exception) as caught:
                await db.execute(
                    "INSERT INTO users (id, username, password_hash) VALUES (99, 'x', 'y')")
            self.assertIn("readonly", str(caught.exception).lower())
        finally:
            await self._give_back(agen)

    async def test_writes_still_work_on_their_own_connection(self):
        from app.database import write_transaction

        async with write_transaction() as wdb:
            await wdb.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (7, 'u', 'h')")
            await wdb.commit()
        db = await get_connection()
        try:
            row = await (await db.execute("SELECT username FROM users WHERE id = 7")).fetchone()
        finally:
            await db.close()
        self.assertEqual(row["username"], "u")


if __name__ == "__main__":
    unittest.main()
