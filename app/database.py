"""
SQLite access layer.

Decisions (see the architecture document):
- WAL + synchronous=NORMAL: readers don't block during writes.
- SQLite only allows one writer at a time regardless of journal mode; that's
  why we serialize writes (scanner/watcher) with an asyncio.Lock instead of
  letting multiple tasks collide on SQLITE_BUSY.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite
from starlette.exceptions import HTTPException

from app import request_timing
from app.config import settings
from app.stats.recording import rebuild_read_totals
from app.stats.pace_totals import PACE_TRIGGER, ensure_pace_totals

_write_lock = asyncio.Lock()
_schema_path = Path(__file__).parent / "schema.sql"


async def init_db() -> None:
    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript(_schema_path.read_text())
        await db.commit()
        # WAL is a database-level setting, persisted in the file itself
        # once set -- unlike synchronous/busy_timeout/foreign_keys below,
        # it doesn't need to be reissued on every new connection. Setting
        # it here, once, at startup, means get_connection() (called on
        # every single request) doesn't pay for a redundant pragma each time.
        await db.execute("PRAGMA journal_mode = WAL")
        await db.commit()
        await _add_missing_columns(db)
        # Collections/reading lists feature removed -- drops the tables on
        # any existing database that still has them from before. Safe to
        # run on every start: DROP TABLE IF EXISTS is already a no-op once
        # they're gone.
        await db.execute("DROP TABLE IF EXISTS collection_items")
        await db.execute("DROP TABLE IF EXISTS collections")
        # Replaced by the wider idx_page_reads_user_reads and
        # idx_page_reads_user_chapter_reads (schema.sql).
        await db.execute("DROP INDEX IF EXISTS idx_page_reads_user_item")
        await db.execute("DROP INDEX IF EXISTS idx_page_reads_user_chapter")
        await db.commit()

        await _run_one_time_migrations(db)
        # Migration 5 rebuilds page_reads using its historical schema. Re-add
        # later additive columns before creating indexes that reference them.
        await _add_missing_columns(db)

        # Again, and deliberately. Every statement in schema.sql is
        # CREATE ... IF NOT EXISTS, so a second run creates only what is
        # missing -- and after a migration, something can be: DROP TABLE
        # takes the table's TRIGGERS with it, which is exactly what the
        # rebuild in migration 5 does to page_reads and the rollup trigger
        # on it. Recreating the index by hand there (as that migration
        # already had to) does not scale to every future migration; running
        # the file the schema is actually defined in does. Measured at
        # 0.27 ms on a database that already has all of it.
        await db.executescript(_schema_path.read_text())
        await db.executescript(AFTER_MIGRATIONS)
        await db.executescript(PACE_TRIGGER)
        await ensure_pace_totals(db)
        await db.commit()


# Schema that names a column a migration adds, so it cannot be in
# schema.sql's first pass (which runs before the migrations, and stops at
# the first statement an older table cannot take). See the note on
# page_reads' indexes in schema.sql.
AFTER_MIGRATIONS = """
CREATE INDEX IF NOT EXISTS idx_page_reads_user_reads
    ON page_reads(user_id, item_id, seconds, cap_seconds, started_at, kind);
CREATE INDEX IF NOT EXISTS idx_page_reads_user_activity
    ON page_reads(user_id, item_id, seconds, cap_seconds, started_at, kind, visit_id,
                  words, pace_words, pace_seconds, pace_samples);
CREATE INDEX IF NOT EXISTS idx_page_reads_user_chapter_activity
    ON page_reads(user_id, chapter_id, seconds, cap_seconds, started_at, visit_id);
"""


# Columns added to a table after it first shipped. `CREATE TABLE IF NOT
# EXISTS` in schema.sql only shapes a brand-new database; an existing table
# keeps the columns it was created with, so each of these is added to one
# that lacks it. Read from the table rather than attempted and caught:
# every start used to run all of these ALTERs and swallow the "duplicate
# column" error of each one.
ADDED_COLUMNS = (
    ("users", "hidden_statuses", "TEXT"),
    # Manual drag-and-drop ordering (see schema.sql's comments on these).
    ("series", "manual_sort", "INTEGER NOT NULL DEFAULT 0"),
    ("series", "manual_sort_editing", "INTEGER NOT NULL DEFAULT 0"),
    ("items", "manual_order", "INTEGER"),
    ("novels", "manual_sort", "INTEGER NOT NULL DEFAULT 0"),
    ("novels", "manual_sort_editing", "INTEGER NOT NULL DEFAULT 0"),
    ("novels", "cover_image_hash", "TEXT"),
    ("series", "cover_overridden", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "avatar_path", "TEXT"),
    ("users", "page_time_cap_seconds", "INTEGER NOT NULL DEFAULT 180"),
    ("users", "text_time_cap_seconds", "INTEGER NOT NULL DEFAULT 180"),
    ("page_reads", "visit_id", "TEXT"),
    ("page_reads", "words", "INTEGER NOT NULL DEFAULT 0"),
    ("page_reads", "pace_words", "INTEGER NOT NULL DEFAULT 0"),
    ("page_reads", "pace_seconds", "REAL NOT NULL DEFAULT 0"),
    ("page_reads", "pace_samples", "INTEGER NOT NULL DEFAULT 0"),
    ("items", "text_size", "INTEGER"),
    ("novel_reading_progress", "character_offset", "INTEGER NOT NULL DEFAULT 0"),
    ("novel_reading_progress", "chapter_percent", "REAL NOT NULL DEFAULT 0"),
    ("novels", "source_title", "TEXT"),
    ("novels", "source_synopsis", "TEXT"),
    ("series", "publication_status", "TEXT NOT NULL DEFAULT 'ongoing'"),
    ("reading_progress", "completed_from_percent", "REAL"),
    ("novel_read_chapters", "read_from_percent", "REAL"),
    ("series", "number_kind", "TEXT"),
    ("tracker_links", "last_pushed_chapter", "REAL"),
    ("tracker_links", "last_pushed_volume", "REAL"),
    ("series", "blur_cover", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "cover_blur_px", "INTEGER NOT NULL DEFAULT 12"),
    ("series", "source_summary", "TEXT"),
    ("novels", "publication_status", "TEXT"),
    ("chapters", "content_size", "INTEGER"),
    ("tracker_links", "last_pushed_status", "TEXT"),
    ("items", "file_mtime_ns", "INTEGER"),
    ("totp_credentials", "last_used_step", "INTEGER"),
    ("items", "metadata_version", "INTEGER"),
)


async def _add_missing_columns(db) -> None:
    present: dict[str, set[str]] = {}
    for table, column, definition in ADDED_COLUMNS:
        if table not in present:
            rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
            present[table] = {row[1] for row in rows}
        if column not in present[table]:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            present[table].add(column)
    await db.commit()


# Bumped whenever a one-time migration is added below. Stored in the
# database file itself via SQLite's built-in `PRAGMA user_version` (an
# integer slot in the file header, 0 on any database that has never set
# it) -- so this needs no migrations table of its own, unlike the
# ALTER TABLE / DROP TABLE statements above, which get away with being
# re-run on every start because they're inherently idempotent. The
# migrations here are NOT: they delete data, so running one twice would
# throw away work done since the first run.
SCHEMA_VERSION = 8


async def _run_one_time_migrations(db) -> None:
    cursor = await db.execute("PRAGMA user_version")
    current = (await cursor.fetchone())[0]
    if current >= SCHEMA_VERSION:
        return

    if current < 1:
        # CBZ page ordering changed from plain lexicographic to natural
        # (see parsers/cbz.py's page_names_from). For an archive whose
        # pages aren't zero-padded, that genuinely renumbers the pages --
        # and page_dimensions is a CACHE keyed by (item_id, page_index)
        # that was filled under the OLD numbering. Left in place, every
        # cached row would hand the reader the wrong page's width/height,
        # which in continuous-scroll mode means every placeholder is
        # reserved at the wrong aspect ratio.
        #
        # Cheapest correct fix: drop the cache. reader/router.py's
        # /dimensions endpoint already recomputes and re-stores it on
        # demand the next time each comic is opened (it only trusts the
        # cache when the row count matches page_count), so this costs one
        # recompute per comic and nothing else -- no user-visible loss.
        #
        # reading_progress.current_page is deliberately NOT reset here: for
        # zero-padded archives (the majority) the old and new ordering are
        # identical, so every saved position stays exactly right, and
        # clearing them would throw away real reading progress across the
        # whole library to fix an off-by-a-few on the minority that shift.
        await db.execute("DELETE FROM page_dimensions")

    if current < 2:
        # The CBR->CBZ conversion step is gone (see
        # _check_readable_archive in app/library/scanner.py). Items it
        # imported were recorded against the converted copy in
        # `<data_dir>/converted/`, not the real file in the library --
        # and because that copy lives outside the library, the scan's
        # reconciliation pass could never remove them: it only deletes an
        # item whose recorded path is both un-walked AND gone from disk.
        #
        # So they are deleted here instead. Whatever is still in the
        # library gets re-imported under its true path on the next scan
        # (a genuine ZIP), and whatever isn't was already an orphan
        # pointing at a copy of a file the user removed. This is what
        # clears the duplicate rows -- the same volume appearing twice on
        # a series page, once per path.
        #
        # Matched on the path prefix rather than a stored flag because
        # the conversion never recorded one; `converted/` under the data
        # directory was only ever written by that step.
        #
        # foreign_keys is enabled explicitly for this statement: the
        # connection init_db runs on is a bare aiosqlite.connect(), not
        # get_connection(), so it does NOT have the pragma that every
        # request connection sets. Without it SQLite ignores the
        # ON DELETE CASCADE on reading_progress/page_dimensions and the
        # deleted items leave orphan rows behind. Issued outside any
        # transaction (hence the commit first) -- it is a no-op inside one.
        #
        # Series covers are left alone here: a deleted item's cover may
        # still be what its series points at, and the reconciliation pass
        # at the end of every scan recomputes each series' cover anyway.
        await db.commit()
        await db.execute("PRAGMA foreign_keys = ON")
        converted_prefix = str(settings.data_dir / "converted") + "/"
        await db.execute(
            "DELETE FROM items WHERE file_path LIKE ? ESCAPE '\\'",
            (converted_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        )

    if current < 3:
        # series.updated_at had a DEFAULT and nothing ever wrote to it, so
        # it equalled created_at on every row and "Recently modified"
        # ordered identically to "Recently added" (see the sort control in
        # library/index.html). The scanner now touches it whenever the
        # series' items change; this seeds the existing rows from what the
        # items themselves already record, so the ordering means something
        # before anything is rescanned. Not idempotent in the useful sense
        # -- re-running it after the scanner starts maintaining the column
        # would push values BACK to their items' timestamps -- hence a
        # one-time migration rather than a startup backfill.
        await db.execute(
            """
            UPDATE series SET updated_at = COALESCE(
                (SELECT MAX(MAX(items.updated_at), MAX(items.added_at))
                   FROM items WHERE items.series_id = series.id),
                updated_at
            )
            """
        )

    if current < 4:
        # tracker_links.last_pushed_number held whichever unit the series
        # happened to be numbered in, which stopped being answerable once a
        # web novel began reporting a volume AND a chapter. Moves each row's
        # value into the field it actually described, read from the series'
        # own number_kind; a novel's rows were always chapters.
        #
        # A one-time migration rather than a startup backfill because it
        # cannot be re-run: once the new columns are being maintained,
        # copying the old one over them again would push a link back to
        # whatever it had when this ran.
        columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(tracker_links)")).fetchall()
        }
        if "last_pushed_number" in columns:
            await db.execute(
                """
                UPDATE tracker_links SET
                    last_pushed_volume = CASE WHEN series_id IS NOT NULL AND (
                        SELECT number_kind FROM series WHERE id = tracker_links.series_id
                    ) = 'volume' THEN last_pushed_number END,
                    last_pushed_chapter = CASE WHEN novel_id IS NOT NULL OR (
                        SELECT number_kind FROM series WHERE id = tracker_links.series_id
                    ) IS NOT 'volume' THEN last_pushed_number END
                WHERE last_pushed_number IS NOT NULL
                """
            )

    if current < 5:
        # page_reads stopped cascading with its content and learned to say
        # what it was (see the note on the table in schema.sql). SQLite can
        # change neither a foreign key's action nor a CHECK by ALTER TABLE,
        # so the table is rebuilt -- the standard twelve-step dance, minus
        # the steps that only matter when something REFERENCES the table
        # being rebuilt, and nothing references this one.
        #
        # `kind` is derived from what each row still points at, which is
        # the only moment that information is still available for rows
        # already recorded. A row whose item is somehow already gone (a
        # delete run on a connection without the foreign-key pragma, or by
        # hand in sqlite3) becomes 'unknown' rather than failing the
        # migration -- the number of seconds it holds is true either way.
        columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(page_reads)")).fetchall()
        }
        if "kind" not in columns:
            # Foreign keys must be off around a rebuild, and the pragma is
            # a no-op inside a transaction -- init_db's connection is a
            # bare aiosqlite.connect() with them off already, so this only
            # documents the requirement rather than changing anything.
            await db.commit()
            await db.execute("PRAGMA foreign_keys = OFF")
            await db.execute("""
                CREATE TABLE page_reads_new (
                    id              INTEGER PRIMARY KEY,
                    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    item_id         INTEGER REFERENCES items(id) ON DELETE SET NULL,
                    novel_id        INTEGER REFERENCES novels(id) ON DELETE SET NULL,
                    chapter_id      INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
                    kind            TEXT NOT NULL CHECK(kind IN ('cbz','epub','novel','unknown')),
                    section_index   INTEGER,
                    page_index      INTEGER,
                    started_at      TEXT NOT NULL,
                    seconds         REAL NOT NULL,
                    cap_seconds     INTEGER NOT NULL,
                    CHECK (NOT (item_id IS NOT NULL AND chapter_id IS NOT NULL))
                )
            """)
            await db.execute("""
                INSERT INTO page_reads_new
                    (id, user_id, item_id, novel_id, chapter_id, kind,
                     section_index, page_index, started_at, seconds, cap_seconds)
                SELECT page_reads.id, page_reads.user_id, page_reads.item_id,
                       page_reads.novel_id, page_reads.chapter_id,
                       CASE WHEN page_reads.chapter_id IS NOT NULL THEN 'novel'
                            ELSE COALESCE(items.file_format, 'unknown') END,
                       page_reads.section_index, page_reads.page_index,
                       page_reads.started_at, page_reads.seconds, page_reads.cap_seconds
                  FROM page_reads
             LEFT JOIN items ON items.id = page_reads.item_id
            """)
            await db.execute("DROP TABLE page_reads")
            await db.execute("ALTER TABLE page_reads_new RENAME TO page_reads")
            # The index went with the old table.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_page_reads_user_time ON page_reads(user_id, started_at)"
            )
            await db.commit()
            await db.execute("PRAGMA foreign_keys = ON")

    if current < 6:
        # item_read_totals / chapter_read_totals are new (see schema.sql):
        # a trigger keeps them current from here on, but it only sees spans
        # recorded from here on. This is what puts the reading already in
        # the database into them, so the estimates do not go blank on
        # everything the user has read so far.
        #
        # A one-time migration rather than a startup backfill because it is
        # a DELETE-then-INSERT: cheap and correct to run on an empty pair of
        # tables, and pointless work on every start afterwards. If the two
        # ever need resynchronising by hand, this is the function to call.
        await rebuild_read_totals(db)

    if current < 7:
        # Unread is an item with no reading_progress row (see
        # reading_progress.mark_item_unread); "Mark all unread" used to
        # write an 'unread' row for every item instead. Those rows put the
        # card's "clear" button on volumes never opened and kept their
        # series sorted as recently read on the home page -- deleting them
        # is what makes the libraries that already have them agree.
        await db.execute("DELETE FROM reading_progress WHERE status = 'unread'")
        await db.commit()

    if current < 8:
        # items.missing: written by nothing since removing a vanished file
        # became automatic, but still filtered on by some twenty queries --
        # and a row an old version had set to 1 stayed hidden for good even
        # with its file back. Dropped; such a row is an ordinary item now,
        # and the next scan removes it if its file is really gone.
        columns = {row[1] for row in await (await db.execute("PRAGMA table_info(items)")).fetchall()}
        if "missing" in columns:
            await db.execute("ALTER TABLE items DROP COLUMN missing")
        # libraries.scan_settings: written once at creation, never read.
        columns = {row[1] for row in await (await db.execute("PRAGMA table_info(libraries)")).fetchall()}
        if "scan_settings" in columns:
            await db.execute("ALTER TABLE libraries DROP COLUMN scan_settings")
        # scan_events: only errors are ever read (/library/issues); the
        # added/modified/removed rows grew by one per import or removal,
        # forever, and nothing could clear them.
        await db.execute("DELETE FROM scan_events WHERE event_type != 'error'")
        await db.commit()

    await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    await db.commit()


async def get_connection() -> aiosqlite.Connection:
    pending = aiosqlite.connect(settings.db_path)
    # aiosqlite's worker thread is not a daemon, so a connection still open
    # when the interpreter exits -- a pooled one, the writer, one a
    # cancelled task never closed -- held the process open forever in
    # threading._shutdown (measured: the test suite stopped exiting). The
    # lifespan closes what it owns cleanly; this is for everything else.
    thread = getattr(pending, "_thread", None)
    if thread is not None:
        thread.daemon = True
    try:
        db = await pending
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA synchronous = NORMAL")
        await db.execute("PRAGMA busy_timeout = 5000")
        await db.execute("PRAGMA foreign_keys = ON")
        return db
    except BaseException:
        with suppress(Exception):
            await pending.close()
        raise


# A small pool of READ connections, handed out by get_db and returned at
# the end of the request.
#
# Opening one per request meant `aiosqlite.connect` (which starts a THREAD)
# plus three PRAGMAs plus a close on every single request, including the
# ones a reader fires in bursts. Measured: 0.95 ms median and 1.95 ms p90
# for that sequence, against 0.124 ms for a trivial query on a connection
# that is already open -- so most of a small request was the connection.
#
# Safe to reuse a connection only because every WRITE in this app goes
# through `write_transaction()` on its own connection. sqlite3 begins an
# implicit transaction on DML and not on a SELECT, so a pooled connection
# that only ever reads is never left holding one open -- but a single
# forgotten write would leave one open forever, and the next borrower
# would inherit it. `PRAGMA query_only` is what stops that being a matter
# of discipline: SQLite itself refuses the write, loudly, on the line that
# tried it.
#
# The size bounds threads, not throughput: SQLite in WAL mode lets readers
# run concurrently, and a request that finds the pool empty simply waits
# for the next one back, which is the same queueing a bigger pool would do
# inside SQLite anyway.
POOL_SIZE = 4

_pool: asyncio.LifoQueue | None = None
_pool_path: str | None = None
_pool_connections: list = []


async def _pool_queue() -> asyncio.LifoQueue:
    """The pool, created on first use.

    Built lazily rather than in the lifespan because the tests drive the
    handlers directly, with no app to start -- and because a pool that
    fills on demand costs nothing on an install that never serves a
    request.

    LIFO so a burst of traffic keeps reusing the same few connections and
    the rest stay idle rather than every one of them being touched in
    turn.
    """
    global _pool, _pool_path
    path = str(settings.db_path)
    if _pool is not None and _pool_path != path:
        # Another database (the tests point settings at a new directory per
        # test): connections to the old one would answer from it.
        await close_pool()
    if _pool is None:
        _pool_path = path
        _pool = asyncio.LifoQueue(maxsize=POOL_SIZE)
        for _ in range(POOL_SIZE):
            _pool.put_nowait(None)
    return _pool


async def close_pool() -> None:
    """Closes every pooled connection, and the write connection. Called
    from the lifespan's shutdown."""
    global _pool
    await _close_writer()
    for db in _pool_connections:
        with suppress(Exception):
            await db.close()
    _pool_connections.clear()
    _pool = None


@asynccontextmanager
async def read_connection():
    """A read connection, borrowed from the pool for the `async with`.

    A connection that raised is closed instead of handed back -- whatever
    state it is in is not worth guessing at, and the next borrower simply
    opens a fresh one. An HTTPException is not that: it is the handler
    ANSWERING (a 401, a 404), with the connection exactly as healthy as
    before, and closing it made every such answer pay for a new one
    (measured: ten 401s opened ten connections).
    """
    pool = await _pool_queue()
    db = await pool.get()
    try:
        if db is None:
            db = await get_connection()
            await db.execute("PRAGMA query_only = ON")
            _pool_connections.append(db)
        yield db
    except HTTPException:
        raise
    except BaseException:
        if db is not None:
            with suppress(ValueError):
                _pool_connections.remove(db)
            try:
                with suppress(Exception):
                    await db.close()
            finally:
                db = None
        raise
    finally:
        pool.put_nowait(db)


async def get_db():
    """FastAPI dependency: a read connection for the whole request.

    For the whole request INCLUDING a streamed response -- FastAPI keeps a
    yield dependency open until the response is sent. That is why a
    stream must not reach this (see auth.dependencies.get_streaming_user):
    four open /library/events streams held all four connections and every
    other request waited for one forever (measured).
    """
    started = request_timing.start()
    async with read_connection() as db:
        request_timing.finish("db_acquire", started)
        yield db


# ONE write connection, reused, behind the lock.
#
# A connection per write was a thread start, three PRAGMAs and a close for
# every one -- about a millisecond each (see POOL_SIZE's note), paid on
# every page turn, because that is how often the readers save progress.
# The lock already allows one writer at a time, so one connection is all
# a writer can ever use.
#
# It is re-opened when the database path changes (the tests point
# settings at a new directory per test) and after anything went wrong on
# it. The lifespan closes it; its thread is a daemon either way (see
# get_connection).
_writer: aiosqlite.Connection | None = None
_writer_path: str | None = None


async def _write_connection() -> aiosqlite.Connection:
    global _writer, _writer_path
    path = str(settings.db_path)
    if _writer is not None and _writer_path != path:
        await _close_writer()
    if _writer is None:
        _writer, _writer_path = await get_connection(), path
    return _writer


async def _close_writer() -> None:
    global _writer, _writer_path
    db, _writer, _writer_path = _writer, None, None
    if db is not None:
        with suppress(Exception):
            await db.close()


@asynccontextmanager
async def write_transaction():
    """
    Usage: `async with write_transaction() as db: ...; await db.commit()`
    Serializes writes from scanner/watcher against each other (and against
    any write coming from requests) so we never rely on retrying after
    SQLITE_BUSY.
    """
    async with _write_lock:
        db = await _write_connection()
        try:
            yield db
        except BaseException:
            # Rolled back, and the connection dropped: whatever state it is
            # in after a failure is not worth guessing at.
            with suppress(Exception):
                await db.rollback()
            await _close_writer()
            raise
        # A block that returned without committing wrote nothing -- the
        # same as when each block had a connection of its own and closed
        # it. Rolled back here so the NEXT writer does not commit it.
        if db.in_transaction:
            await db.rollback()
