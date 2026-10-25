-- ===== Authentication =====

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    -- JSON array of series-status values (see SERIES_STATUS_LABELS) that
    -- this user has chosen to hide from browsing app-wide. NULL/empty =
    -- hide nothing (the default). Same treatment as the status-filter
    -- dropdown on the home page: search intentionally ignores this too,
    -- so a hidden series is still findable by typing its name.
    hidden_statuses TEXT,
    -- Relative path under covers_dir of an uploaded avatar, or NULL for
    -- none (the header then shows the username, as it always did). Same
    -- content-addressed naming as every other image this app stores, so
    -- the /covers mount's `immutable` caching applies to it too.
    avatar_path     TEXT,
    -- Per-page reading-time cap, in seconds (see page_reads below). A page
    -- left open longer than this counts as this much, which is what keeps
    -- "went to make coffee" from being recorded as reading. User-editable
    -- on the settings page; 180s is the default asked for.
    page_time_cap_seconds INTEGER NOT NULL DEFAULT 180,
    text_time_cap_seconds INTEGER NOT NULL DEFAULT 180,
    -- How hard the home grid blurs a cover whose series asked for it (see
    -- series.blur_cover). The FLAG is a fact about the book and lives on
    -- the series; how hard to blur it is about the room you are in, so it
    -- is one number per reader rather than one per cover. It reaches the
    -- page as a CSS variable on the grid, which is what keeps a cover from
    -- being drawn sharp for one frame while a script reads a preference.
    cover_blur_px   INTEGER NOT NULL DEFAULT 12,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS totp_credentials (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    secret          TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 0,
    -- The time step of the last code accepted. A code at or before it is
    -- refused, so one code signs in once (see auth.service.totp_step).
    last_used_step  INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id   TEXT NOT NULL UNIQUE,
    public_key      TEXT NOT NULL,
    sign_count      INTEGER NOT NULL DEFAULT 0,
    transports      TEXT,
    nickname        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT
);

CREATE TABLE IF NOT EXISTS webauthn_challenges (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    challenge       TEXT NOT NULL,
    purpose         TEXT NOT NULL CHECK(purpose IN ('registration','authentication')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    user_agent      TEXT,
    ip_address      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    totp_verified   INTEGER NOT NULL DEFAULT 0
);

-- ===== Library =====

CREATE TABLE IF NOT EXISTS libraries (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    root_path       TEXT NOT NULL UNIQUE,
    media_type      TEXT NOT NULL CHECK(media_type IN ('comic','ebook','mixed')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS series (
    id              INTEGER PRIMARY KEY,
    library_id      INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    sort_name       TEXT NOT NULL,
    folder_path     TEXT NOT NULL,
    cover_path      TEXT,
    -- The description shown on the series page. Seeded from whichever
    -- tracker the series is linked to and editable by hand after that;
    -- source_summary keeps the tracker's own wording, so "use the
    -- tracker's again" is a button and not another request, and so a
    -- later fetch can tell an edited description from an untouched one.
    -- The same arrangement novels.synopsis/source_synopsis uses, and for
    -- the same reason: a refetch must not silently undo an edit.
    summary         TEXT,
    source_summary  TEXT,
    -- When 1, the series page orders items by items.manual_order instead
    -- of (number_in_series, sort_title) -- lets the user drag-and-drop fix
    -- a series whose filenames/embedded titles don't sort correctly on
    -- their own. Sticky: set once on first use and never cleared by the
    -- toggle below, so turning off drag mode doesn't revert to automatic
    -- ordering. See /series/{id}/manual-sort/toggle and /reorder.
    -- Where the WORK is in its publication -- not where the reader is in
    -- it, which is series_status below. See app/status_labels.py. Every
    -- series is 'ongoing' until somebody says otherwise: a library of
    -- files has no source to ask, and still-running is the common case.
    -- The CHECK only exists on a database created from this file (SQLite
    -- cannot add one by ALTER TABLE); what actually guards the value
    -- everywhere is the endpoint validating against
    -- PUBLICATION_STATUS_LABELS.
    -- Which KIND of marker orders this series -- 'chapter', 'volume' or
    -- 'bare' -- as decided for the series as a whole by
    -- resolve_series_numbers() (see app/library/naturalsort.py). The
    -- number itself has always been stored, on items.number_in_series;
    -- what it MEANS was computed and thrown away, which was fine while
    -- nothing but sorting consumed it. A tracker consumes it: MangaUpdates
    -- and MyAnimeList both record chapters and volumes as separate counts,
    -- so sending "7" without knowing which of the two it is puts the
    -- number in the wrong field half the time. NULL where the scanner has
    -- not run since this column existed, or where the series has no
    -- numbers at all.
    number_kind         TEXT,
    publication_status  TEXT NOT NULL DEFAULT 'ongoing'
                        CHECK(publication_status IN ('ongoing', 'completed', 'hiatus', 'canceled')),
    manual_sort         INTEGER NOT NULL DEFAULT 0,
    -- Whether the "Reorder manually" drag-and-drop UI is CURRENTLY active
    -- on the series page -- independent of manual_sort above (which
    -- controls the ORDER BY regardless of this flag). Only this one is
    -- what the toggle button actually flips.
    manual_sort_editing INTEGER NOT NULL DEFAULT 0,
    -- When 1, cover_path was uploaded by hand and _recompute_series_cover()
    -- leaves it alone -- otherwise the next scan (or any item change) would
    -- immediately put the first item's own thumbnail back. Resetting clears
    -- this flag and recomputes, which is what makes the override undoable.
    -- The NAME needs no equivalent flag: nothing ever rewrites series.name
    -- after creation, and the automatic value is always recoverable from
    -- folder_path's last component, which is where it came from.
    cover_overridden    INTEGER NOT NULL DEFAULT 0,
    -- When 1, this series' cover is rendered blurred on the home grid --
    -- the one screen that shows every cover at once, to anybody who
    -- happens to be looking at the reader's phone. It is a property of the
    -- SERIES rather than of the reader (like publication_status, and
    -- unlike series_status): what is on the cover is a fact about the
    -- book. Nothing else is affected -- the series page, the reader and
    -- the "Reading" strip all show it as it is, because those are places
    -- you went to on purpose.
    blur_cover          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(library_id, folder_path)
);

CREATE TABLE IF NOT EXISTS items (
    id                  INTEGER PRIMARY KEY,
    library_id          INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    series_id           INTEGER REFERENCES series(id) ON DELETE SET NULL,
    title               TEXT NOT NULL,
    sort_title          TEXT NOT NULL,
    file_path           TEXT NOT NULL UNIQUE,
    file_format         TEXT NOT NULL CHECK(file_format IN ('epub','cbz')),
    file_hash           TEXT NOT NULL,
    file_size           INTEGER NOT NULL,
    -- st_mtime_ns when file_hash was taken. With file_size, it is what lets
    -- a rescan skip hashing a file that has not changed (see
    -- scanner.process_file); NULL until the next scan records it.
    file_mtime_ns       INTEGER,
    -- How much BOOK there is, as opposed to how big the file is: the
    -- uncompressed byte total of the reading order (see
    -- parsers/epub.spine_text_size). NULL for a comic, which has no such
    -- notion, and for anything imported before this column existed until
    -- the startup backfill fills it in.
    --
    -- file_size cannot stand in for it. An EPUB is a compressed zip that
    -- also holds images, fonts and a cover, so an illustrated book of a
    -- few thousand words can be orders of magnitude larger on disk than a
    -- quarter-million-word novel -- measured at 25 MB against 18 KB for
    -- exactly that pair.
    text_size           INTEGER,
    page_count          INTEGER,
    number_in_series    REAL,
    -- Position within the series when series.manual_sort is enabled --
    -- NULL for an item that's never been placed by a drag (sorts after
    -- every positioned item, same "unpositioned goes last" treatment as
    -- number_in_series already gets). Unused/ignored while manual_sort is 0.
    manual_order        INTEGER,
    cover_path          TEXT,
    metadata            TEXT,
    -- Which rules produced `metadata` (parsers/epub.METADATA_VERSION).
    -- NULL = imported before there was a number; lower = older rules. Both
    -- are what the startup backfill re-reads, since a file that has not
    -- changed is never parsed again otherwise.
    metadata_version    INTEGER,
    added_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_items_series ON items(series_id);
CREATE INDEX IF NOT EXISTS idx_items_library ON items(library_id);
CREATE INDEX IF NOT EXISTS idx_items_hash ON items(file_hash);

-- ===== Page dimensions (comic) =====
-- Computed on demand on the first visit to the reader and cached here --
-- lets the frontend size each page placeholder at its correct FINAL size
-- (via aspect-ratio) already on the first render, without needing to wait
-- for the image to load to find out its real height. This eliminates
-- placeholder growth/shrinkage during scrolling (and all the scroll
-- compensation logic that required).
CREATE TABLE IF NOT EXISTS page_dimensions (
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    page_index      INTEGER NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    PRIMARY KEY (item_id, page_index)
);

-- ===== Reading progress =====

CREATE TABLE IF NOT EXISTS reading_progress (
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id             INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'unread' CHECK(status IN ('unread','reading','completed')),
    current_page        INTEGER,
    current_location    TEXT,
    progress_percent    REAL NOT NULL DEFAULT 0,
    last_read_at        TEXT,
    completed_at        TEXT,
    -- How far the reader had ACTUALLY got when this row became
    -- 'completed'. Normally ~100 (they read to the end), but "Mark as
    -- read" is a declaration and can be made from anywhere -- from a book
    -- never opened, or one abandoned at 12% -- and without this the two
    -- are indistinguishable afterwards: the mark sets progress_percent to
    -- 100 either way.
    --
    -- The statistics need the difference. "How long one volume takes"
    -- averages finished things, and a book declared done at 12% took the
    -- time of 12% of a book; the reading-rate learning in
    -- stats/estimates.py pairs those same seconds against the text, and
    -- calling 12% of a book a whole one makes the reader look several
    -- times faster than they are, on every card. NULL means a row
    -- completed before this was recorded -- treated as a real finish,
    -- since nothing else is known about it.
    completed_from_percent REAL,
    PRIMARY KEY (user_id, item_id)
);

-- ===== Reading time =====
-- One row per PAGE VISIT: a contiguous span during which one page (a comic
-- page, or a paragraph position in a text chapter) was the one on screen.
-- Leaving a page and coming back later produces two rows, which is exactly
-- what the accounting asked for: 60s on page 10, 10s on 11, 30s back on 10,
-- 60s finishing 11 totals 160s, not "the span from first opening 10 to
-- finally leaving 11".
--
-- `seconds` is the RAW measured duration and `cap_seconds` is the cap that
-- was in force when it was recorded; the counted time is always
-- MIN(seconds, cap_seconds). Storing both is what makes changing the cap
-- safe: a row measured under a 180s cap keeps being counted under 180s
-- however the setting moves later, so history never silently rewrites
-- itself. It is also strictly more information than storing the already-
-- capped number would be -- the raw duration is still there, so the whole
-- history CAN be recomputed under a different cap if that is ever wanted.
--
-- The alternative -- a table of cap changes over time, joined by date --
-- was deliberately not built: it answers the same question with a range
-- join that any clock skew, backdated edit or missing first row gets
-- subtly wrong, while the cap belongs to the measurement itself and is
-- known exactly at the moment it is taken.
--
-- item_id or (novel_id, chapter_id): a comic/ebook page belongs to an
-- item, a web-novel page to a chapter of a novel. Never both -- that is
-- what the CHECK below says -- and, since content can be deleted,
-- possibly NEITHER.
--
-- Those links are ON DELETE SET NULL and NOT cascade, which is the whole
-- point of `kind`. A row here is a measurement of the READER: at this
-- moment, for this long, they were reading. That stays true when the file
-- is deleted -- deleting a comic does not un-read it -- and cascading
-- rewrote history instead: the daily chart for a Tuesday three months ago
-- silently shrank because a volume read that day was tidied off the disk,
-- with nothing on screen saying so. Measured before the change: deleting
-- one of two items took 3 of 6 rows and half the counted seconds with it.
--
-- So the row has to survive its content, and to survive it must carry
-- what the statistics ask of it. That is `kind`, denormalized at INSERT
-- from items.file_format (or 'novel' for a chapter): the per-kind chart
-- and the seconds-per-page split both group by it, and with it stored
-- they no longer join `items` at all. 'unknown' is not written by this
-- app -- it exists for rows migrated from a database where the item was
-- already gone.
--
-- What the row itself does NOT carry is anything that is a statement
-- about the ITEM rather than about the reader: its title, its series,
-- whether it was finished, how much text it holds. Copying those into a
-- table of moments would be the wrong place -- not least because items.id
-- is a plain INTEGER PRIMARY KEY, so SQLite hands a deleted item's id to
-- the next one inserted and orphan rows would silently re-attach to a
-- different book. They are kept instead in removed_items below, written
-- once when the item goes (docs/adr/0001-remember-removed-items.md).
CREATE TABLE IF NOT EXISTS page_reads (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id         INTEGER REFERENCES items(id) ON DELETE SET NULL,
    novel_id        INTEGER REFERENCES novels(id) ON DELETE SET NULL,
    chapter_id      INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    -- What was being read, kept here so the row still knows after its
    -- content is gone. See the note above.
    kind            TEXT NOT NULL CHECK(kind IN ('cbz','epub','novel','unknown')),
    -- Which chapter WITHIN the item, for an EPUB: its spine index. NULL
    -- for a comic (an item is one flat run of pages) and for a web novel
    -- (chapter_id already names the chapter). Without it, paragraph 3 of
    -- chapter 1 and paragraph 3 of chapter 2 are the same position, and
    -- the reader would not notice it had crossed the boundary -- it would
    -- keep one span running across two chapters.
    section_index   INTEGER,
    -- Comic: the page index. Text: the index of the topmost block element
    -- in the chapter, the same unit reading position is already stored in.
    page_index      INTEGER,
    started_at      TEXT NOT NULL,
    seconds         REAL NOT NULL,
    cap_seconds     INTEGER NOT NULL,
    visit_id        TEXT,
    words           INTEGER NOT NULL DEFAULT 0,
    pace_words      INTEGER NOT NULL DEFAULT 0,
    pace_seconds    REAL NOT NULL DEFAULT 0,
    pace_samples    INTEGER NOT NULL DEFAULT 0,
    -- Never two things at once. "Neither" is legal and means the content
    -- was deleted; what it WAS is in `kind`.
    CHECK (NOT (item_id IS NOT NULL AND chapter_id IS NOT NULL))
);
-- Checkpoints append accounting deltas to page_reads. The delivery journal
-- retains cumulative state; it never rewrites the historical ledger.
CREATE TABLE IF NOT EXISTS reading_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reading_claims (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES reading_sessions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started REAL NOT NULL,
    ended REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reading_claim_open
    ON reading_claims(user_id) WHERE ended IS NULL;
CREATE INDEX IF NOT EXISTS idx_reading_claim_session ON reading_claims(session_id, started);
CREATE INDEX IF NOT EXISTS idx_reading_claim_owned ON reading_claims(session_id,started,ended)
    WHERE ended IS NULL OR ended>started;
CREATE TABLE IF NOT EXISTS reading_activity (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES reading_sessions(id) ON DELETE CASCADE,
    identity TEXT NOT NULL,
    revision INTEGER NOT NULL,
    intervals TEXT NOT NULL,
    raw_seconds REAL NOT NULL DEFAULT 0,
    counted_seconds REAL NOT NULL DEFAULT 0,
    cap_seconds REAL NOT NULL,
    word_count INTEGER NOT NULL,
    word_positions TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    novel_id INTEGER REFERENCES novels(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL
);
-- Every statistics query is "this user's rows, over a date range", and the
-- daily charts group by started_at -- so one composite index covers all of
-- them, and the table only ever grows.
CREATE INDEX IF NOT EXISTS idx_page_reads_user_time ON page_reads(user_id, started_at);
-- The statistics page asks a different question -- "this user's seconds,
-- totalled per item / per chapter" (time_to_finish and recent_items in
-- stats/queries.py) -- and the index above cannot answer it: it is ordered
-- by time, so the grouping is a sort of the whole table.
--
-- These two carry `seconds` and `cap_seconds` as payload so the sum is read
-- straight out of the index. That part is not decoration: measured on
-- 200,000 rows, the grouping went 55 ms -> 11.5 ms with the columns
-- included and 55 ms -> 120 ms with only (user_id, item_id) -- a plain
-- index made it WORSE, because SQLite then seeks back into the table for
-- every row to fetch the two numbers.
--
-- The item one also carries `started_at` and `kind`, so every /stats query
-- over the whole history reads from it alone ("Most recent" takes the last
-- time, the per-kind split and the totals scan it). Without them each one
-- went back to the table row by row -- and with one user, `user_id` filters
-- nothing, so the planner even picked the chapter index for the per-kind
-- split: /stats took 1.5 s on 200,000 spans. It replaced
-- idx_page_reads_user_item (dropped in init_db, like the chapter one's
-- predecessor). It is created in database.py (AFTER_MIGRATIONS), not here:
-- `kind` arrives with migration 5's rebuild, and this file runs once
-- before the migrations, where an older page_reads has no such column.
-- `started_at` here too: the web-novel half of "Most recent" takes each
-- volume's last time. Replaced idx_page_reads_user_chapter.
CREATE INDEX IF NOT EXISTS idx_page_reads_user_chapter_reads
    ON page_reads(user_id, chapter_id, seconds, cap_seconds, started_at);
-- What DELETE FROM items needs: the foreign keys that point at an item
-- (page_reads SET NULL, reading_progress and item_read_totals CASCADE) look
-- their rows up BY item_id, and every index above leads with user_id -- so
-- each deleted item was a full scan of all three. Measured with 200,000
-- spans: 15.7-18.8 ms per item removed, inside the write lock, against
-- 0.85 ms with these. Partial on page_reads: a novel's spans have no item.
CREATE INDEX IF NOT EXISTS idx_page_reads_item ON page_reads(item_id) WHERE item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_reading_progress_item ON reading_progress(item_id);

-- ===== Removed items =====
-- What the reader read of an item whose file has left the library (see
-- Removed item in GLOSSARY.md, and docs/adr/0001-remember-removed-items.md
-- for why this exists at all). Written once, by scanner._delete_item, just
-- before the item row goes, and only for an item somebody spent time on:
-- an item nobody read has nothing to remember.
--
-- A SNAPSHOT, not a link: the item's spans stay in page_reads with their
-- item_id gone NULL, exactly as before, and keep counting in every total
-- and every day of the chart. What is copied here is what died with the
-- item -- its name and series for "Most recent", its completion verdict
-- for "start to finish", its size for the reading rate -- plus each
-- reader's own sums of those spans, which can no longer change because
-- nothing can be read on an item that is gone.
--
-- AUTOINCREMENT, unlike items: a removed item is never re-used, and a
-- record must never be taken for a later one.
--
-- The series is remembered as its FOLDER (library_id, folder_path), which
-- is what a series is, and not by series.id: the series row goes with its
-- last item unless the reader invested something in it, and comes back
-- with a NEW id when a file lands in that folder again. series_name is
-- the name it had, for when no series lives in that folder any more.
-- folder_path NULL is an item that sat at the library root, in no series.
CREATE TABLE IF NOT EXISTS removed_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id      INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    folder_path     TEXT,
    series_name     TEXT,
    title           TEXT NOT NULL,
    file_format     TEXT NOT NULL CHECK(file_format IN ('epub','cbz')),
    text_size       INTEGER,
    page_count      INTEGER,
    removed_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per reader who spent time on the item. seconds/pages/last_at are
-- that reader's spans summed the way stats/queries.py counts them
-- (COUNTED); status, progress_percent and completed_from_percent are their
-- reading_progress row as it stood, NULL for an item never marked anything.
CREATE TABLE IF NOT EXISTS removed_item_reads (
    user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    removed_item_id         INTEGER NOT NULL REFERENCES removed_items(id) ON DELETE CASCADE,
    seconds                 REAL NOT NULL,
    pages                   INTEGER NOT NULL,
    last_at                 TEXT NOT NULL,
    status                  TEXT,
    progress_percent        REAL,
    completed_from_percent  REAL,
    PRIMARY KEY (user_id, removed_item_id)
) WITHOUT ROWID;

-- ===== Reading totals, derived from page_reads =====
-- The reading estimates (stats/estimates.py) want one number per thing the
-- reader has spent time on: seconds totalled per item, seconds totalled per
-- chapter. Nothing else -- not when it happened, not which page. Computing
-- that by grouping page_reads meant re-reading every span the user had ever
-- recorded, on the home page, on every series page and on every card
-- swapped by a read/unread toggle. Even off the covering indexes above the
-- cost is linear in a table that only grows: measured at 200,000 spans,
-- 224 ms to build the estimator, against 2 ms from these two tables -- and
-- the 2 ms no longer moves as the spans pile up, which is the real change.
--
-- These are a CACHE, and page_reads remains the truth. Nothing is pruned
-- and no granularity is given up: the daily chart, "most recent" and the
-- per-page averages all still read the raw spans, which is also what keeps
-- the history recomputable under a different cap (see the note on
-- cap_seconds above). Anything here can be thrown away and rebuilt --
-- rebuild_read_totals() in stats/recording.py is that rebuild, and the
-- tests use it to assert these tables agree with the spans they came from.
--
-- ON DELETE CASCADE, where page_reads deliberately uses SET NULL. The
-- difference is the point: the raw span survives its content because the
-- statistics are about the READER and a deleted comic was still read that
-- Tuesday, while a total exists only to estimate a book that no longer
-- exists. Cascading also closes the id-reuse hole the note above worries
-- about -- SQLite hands a deleted item's id to the next one inserted, and a
-- total left behind would silently become that book's head start.
CREATE TABLE IF NOT EXISTS item_read_totals (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    -- SUM(MIN(seconds, cap_seconds)) over this user's spans on this item,
    -- i.e. COUNTED in stats/queries.py, accumulated one span at a time.
    seconds  REAL NOT NULL,
    PRIMARY KEY (user_id, item_id)
) WITHOUT ROWID;
-- See idx_page_reads_item: a deleted item's totals are found by item_id.
CREATE INDEX IF NOT EXISTS idx_item_read_totals_item ON item_read_totals(item_id);

CREATE TABLE IF NOT EXISTS chapter_read_totals (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    seconds    REAL NOT NULL,
    PRIMARY KEY (user_id, chapter_id)
) WITHOUT ROWID;

-- Maintained by SQLite, not by the application, and that is a deliberate
-- choice rather than a flourish. A derived table kept up to date by hand
-- fails silently: the write path that forgets it produces no error, just a
-- wrong estimate on a card, and every future insert into page_reads --
-- including the ones in the test fixtures -- is another chance to forget.
-- A trigger cannot be forgotten, runs inside the same transaction as the
-- insert, and fires for a row written from sqlite3 by hand exactly as it
-- does for one written by record_spans. The write side pays 3 us per span
-- for that: a reader's 30-second flush of 200 spans measured 3.47 ms
-- without the trigger and 4.08 ms with it.
--
-- Only AFTER INSERT, because an insert is the only thing this app ever does
-- to page_reads (test_hot_path_costs.py pins that). Deletes reach these
-- tables through the foreign keys above instead, which is both cheaper and
-- exactly what a rebuild would produce.
--
-- `SELECT ... WHERE` with no FROM is how a trigger writes one row
-- conditionally: a span names an item or a chapter, never both (the CHECK
-- on page_reads), so each statement writes nothing for the other kind.
CREATE TRIGGER IF NOT EXISTS page_reads_read_totals
AFTER INSERT ON page_reads
BEGIN
    INSERT INTO item_read_totals (user_id, item_id, seconds)
    SELECT NEW.user_id, NEW.item_id, MIN(NEW.seconds, NEW.cap_seconds)
     WHERE NEW.item_id IS NOT NULL
    ON CONFLICT(user_id, item_id)
    DO UPDATE SET seconds = item_read_totals.seconds + excluded.seconds;

    INSERT INTO chapter_read_totals (user_id, chapter_id, seconds)
    SELECT NEW.user_id, NEW.chapter_id, MIN(NEW.seconds, NEW.cap_seconds)
     WHERE NEW.chapter_id IS NOT NULL
    ON CONFLICT(user_id, chapter_id)
    DO UPDATE SET seconds = chapter_read_totals.seconds + excluded.seconds;
END;

-- ===== Series tracking status =====
-- Independent from per-item progress (reading_progress, which is "where I
-- left off in each chapter") -- this is a manual per-SERIES label, like
-- MyAnimeList/AniList, for the user to organize what they're reading,
-- already finished, dropped, etc. No row here = "no status" (doesn't
-- match any specific filter, only "show all").
CREATE TABLE IF NOT EXISTS series_status (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK(status IN (
                        'reading', 'completed', 'dropped', 'stopped_reading', 'on_hold', 'plan_to_read'
                    )),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, series_id)
);

-- ===== Scan audit log =====

CREATE TABLE IF NOT EXISTS scan_events (
    id              INTEGER PRIMARY KEY,
    library_id      INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL CHECK(event_type IN ('added','removed','modified','converted','error')),
    file_path       TEXT NOT NULL,
    detail          TEXT,
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
-- /library/issues filters on event_type = 'error' ordered by occurred_at,
-- and this table only ever grows (scan runs accumulate rows indefinitely,
-- only manually cleared) -- without this index that query is a full table
-- scan on every page load once a library has been rescanned many times.
CREATE INDEX IF NOT EXISTS idx_scan_events_type_time ON scan_events(event_type, occurred_at DESC);

-- ===== Web novels (scraped content, separate pipeline from the file-based
-- library above) =====
-- Ported from a standalone companion app that scraped novel text from a
-- specific translation site via headless Chromium. That app used
-- SQLAlchemy ORM and was multi-user with an admin role; here it's
-- rewritten as raw SQL to match the rest of this schema, and there is no
-- admin/multi-user split -- adding a novel is just another action the
-- single user takes, same as creating a library.

CREATE TABLE IF NOT EXISTS novels (
    id                          INTEGER PRIMARY KEY,
    title                       TEXT NOT NULL,
    source_url                  TEXT NOT NULL UNIQUE,
    cover_url                   TEXT,      -- source page's cover URL, kept only to detect a changed cover on resync
    cover_image                 BLOB,      -- downloaded and stored locally so the browser never hotlinks the source site
    cover_image_content_type    TEXT,
    -- hash of cover_image's bytes, so the cover URL can carry a version that
    -- changes only when the cover does (see /novels/{id}/cover)
    cover_image_hash            TEXT,
    synopsis                    TEXT,
    -- What the SOURCE last said the title/synopsis were. `title` and
    -- `synopsis` above are what this app SHOWS, and the two are normally
    -- the same. They come apart when the user edits one here: a sync
    -- overwrites the displayed value only while it still matches what the
    -- source last gave us, so an edit survives the nightly sync instead of
    -- being silently reverted -- and because the source's own wording is
    -- kept rather than overwritten, "use the source's again" is a button
    -- and not another scrape.
    source_title                TEXT,
    source_synopsis             TEXT,
    status                      TEXT,      -- e.g. "Ongoing" / "Completed", as reported by the source page
    -- The reader's OWN answer to "is there more coming", overriding what
    -- the source page says. NULL is the ordinary state and means "follow
    -- the source", which is what publication_status_from_source maps for
    -- us -- a scraped novel already knows, so being asked would be noise.
    -- It exists because that mapping cannot recognise every wording, and
    -- an unrecognised one gets no badge at all rather than a guess; this
    -- is how a reader says what the site failed to. Set back to NULL to
    -- go back to following the source, the same shape as source_synopsis
    -- above. Not a CHECK: the column is guarded by the endpoint
    -- validating against PUBLICATION_STATUS_LABELS, since SQLite cannot
    -- add a CHECK to an existing table and this one arrives by ALTER.
    publication_status          TEXT,
    active                      INTEGER NOT NULL DEFAULT 1,  -- keep checking for new chapters on the periodic sync
    -- Live sync visibility: updated throughout sync_novel() (see
    -- app/novels/sync.py) so the settings page can show what's happening
    -- (or what failed) without digging through server logs.
    sync_status                 TEXT NOT NULL DEFAULT 'idle',
    sync_error                  TEXT,
    last_checked_at             TEXT,
    -- When 1, sync_novel() (see app/novels/sync.py) stops renumbering
    -- every chapter's order_index on each sync and only appends newly
    -- discovered chapters at the end -- lets the user drag-and-drop fix a
    -- novel whose site ordering doesn't match reading order, without a
    -- later sync wiping it out. Sticky: set once on first use and never
    -- cleared by the toggle below, so turning off drag mode doesn't let a
    -- later sync renumber and wipe out the dragged order. See
    -- /novels/{id}/manual-sort/toggle and /reorder.
    manual_sort                 INTEGER NOT NULL DEFAULT 0,
    -- Whether the "Reorder manually" drag-and-drop UI is CURRENTLY active
    -- on the novel's page -- independent of manual_sort above. Only this
    -- one is what the toggle button actually flips.
    manual_sort_editing         INTEGER NOT NULL DEFAULT 0,
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS volumes (
    id              INTEGER PRIMARY KEY,
    novel_id        INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    number          INTEGER NOT NULL,
    title           TEXT,
    UNIQUE(novel_id, number)
);

CREATE TABLE IF NOT EXISTS chapters (
    id                  INTEGER PRIMARY KEY,
    novel_id            INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    volume_id           INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    number              REAL,      -- float: a decimal sub-part (e.g. 45.3) marks a split/interlude chapter
    title               TEXT NOT NULL,
    url                 TEXT NOT NULL,  -- source chapter URL; also the natural de-dupe key on resync
    release_date        TEXT,
    order_index         INTEGER NOT NULL DEFAULT 0,  -- display order, recomputed after every sync (volume, number) ascending
    content_html        TEXT,      -- NULL until the chapter body has actually been downloaded
    -- LENGTH(content_html), kept beside it. The manifest weights reading
    -- progress by it and the reading-rate learning pairs seconds against
    -- it, and both used to call LENGTH() -- which, on a TEXT column, reads
    -- the whole value: SQLite can shortcut it only for a BLOB. Measured on
    -- 900 chapters of ~6 KB: 4.8 ms and 5.4 MB read per manifest, against
    -- 1.8 ms and almost nothing. Written wherever content_html is, and
    -- filled in for older rows by a startup backfill.
    content_size        INTEGER,
    content_fetched_at  TEXT,
    discovered_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(novel_id, url)
);
CREATE INDEX IF NOT EXISTS idx_chapters_novel ON chapters(novel_id);
CREATE INDEX IF NOT EXISTS idx_chapters_volume ON chapters(volume_id);

-- Single current position per (user, novel) -- same shape as reading_progress
-- above, kept as a separate table since it references chapters/novels
-- instead of items, and tracks a scroll percentage + paragraph anchor
-- instead of a page/CFI.
CREATE TABLE IF NOT EXISTS novel_reading_progress (
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    novel_id            INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    chapter_id          INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    -- Fraction of the WHOLE novel, across every volume. Not what the
    -- cards show (see chapter_percent) -- this is what the reading-rate
    -- learning in stats/estimates.py measures a novel's progress with,
    -- since a rate needs "how much of this novel has been read", not
    -- "how far into one chapter".
    scroll_percent      REAL NOT NULL DEFAULT 0,
    -- Fraction of the CURRENT chapter alone. What the percentage and the
    -- time estimate on a card are about: a chapter is the unit a reader
    -- of a web novel actually finishes, the way a volume is for a comic.
    chapter_percent     REAL NOT NULL DEFAULT 0,
    -- Index of the topmost visible <p> within the chapter (stable
    -- regardless of viewport size/line-wrapping) -- lets "continue
    -- reading" resume at the same actual spot switching between desktop
    -- and mobile, which a raw scroll percentage alone can't do reliably.
    paragraph_index     INTEGER,
    character_offset    INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, novel_id)
);

-- Explicit per-chapter "finished this" marker, independent of
-- novel_reading_progress (which only ever tracks the single current position).
CREATE TABLE IF NOT EXISTS novel_read_chapters (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chapter_id      INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    read_at         TEXT NOT NULL DEFAULT (datetime('now')),
    -- The novel twin of reading_progress.completed_from_percent: how far
    -- into THIS chapter the reader was when it was marked read. ~100 when
    -- they read out of it, 0 when it was ticked off a list -- and "Mark
    -- all as read" on a volume ticks off a hundred at once. The rate in
    -- stats/estimates.py pairs a chapter's seconds with its bytes, so
    -- calling a chapter fully read when a tenth of it was is the same
    -- error as on the item side. NULL = recorded before this column
    -- existed, taken as a real read.
    read_from_percent REAL,
    PRIMARY KEY (user_id, chapter_id)
);

-- Same manual tracking label as series_status (MyAnimeList/AniList-style),
-- for novels -- kept as its own table (rather than widening series_status
-- to allow either a series_id or a novel_id) since a novel isn't a series
-- row and the two content types have separate id spaces.
CREATE TABLE IF NOT EXISTS novel_status (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    novel_id        INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK(status IN (
                        'reading', 'completed', 'dropped', 'stopped_reading', 'on_hold', 'plan_to_read'
                    )),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, novel_id)
);

-- Per-series / per-novel find-and-replace, applied in the text readers only.
--
-- Purely a display setting: nothing here ever touches the EPUB on disk or
-- the scraped chapter in `chapters.content_html`. The reader substitutes
-- while rendering (see static/js/reader-text.js), so removing a rule puts
-- the original text back with no re-import and no re-sync.
--
-- Exactly one of series_id / novel_id is set -- a rule belongs to one
-- shelf of things, and "both null" (a global rule) is deliberately not
-- offered: a replacement that made sense for one novel's censored names
-- would fire in every book in the library.
--
-- find_text / replace_text rather than find / replace: Jinja resolves an
-- attribute with getattr BEFORE the mapping, and `str.replace` exists, so
-- `rule.replace` in a template would silently render a bound method. Same
-- trap the `Estimate` namedtuple and the finish-time keys were named
-- around.
-- ---------------------------------------------------------------------
-- Tracking (MangaUpdates, and MyAnimeList behind the same two tables)
-- ---------------------------------------------------------------------

-- One row per (reader, service) holding the SESSION, never the password.
-- MangaUpdates trades a username and password for a bearer token
-- (PUT /v1/account/login); that exchange happens once, in the request that
-- submits the form, and only the token it returns is written here.
-- MyAnimeList will fill the same row with an OAuth access/refresh pair,
-- which is why the columns are named for what they hold rather than for
-- either service's vocabulary.
CREATE TABLE IF NOT EXISTS tracker_accounts (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tracker         TEXT NOT NULL,
    -- Who the service says we are, shown on the settings page so a
    -- connection can be recognised without opening the service.
    username        TEXT,
    access_token    TEXT NOT NULL,
    refresh_token   TEXT,
    -- NULL where the service does not say (MangaUpdates does not); a
    -- 401 is then the only way to learn a session has ended, which is
    -- what `authenticated` below records.
    expires_at      TEXT,
    -- Cleared on every success. Set when the service refused, so the
    -- settings page can say "sign in again" instead of silently
    -- failing every push from then on.
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, tracker)
);

-- One in-flight OAuth handshake per (reader, service).
--
-- The PKCE verifier has to survive the round trip to the service and back,
-- and it must NOT reach the browser: the whole point is that whoever
-- intercepts the redirect cannot redeem the code without it. `state` is
-- the other half -- it does travel, and is what proves the callback
-- belongs to the handshake this app started rather than to one somebody
-- else pointed at it.
--
-- A row lives only between the redirect out and the callback back; the
-- callback deletes it, so a stale one means an abandoned attempt and is
-- replaced by the next.
CREATE TABLE IF NOT EXISTS tracker_oauth_pending (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tracker         TEXT NOT NULL,
    state           TEXT NOT NULL,
    code_verifier   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, tracker)
);

-- What a local series or novel IS on a tracker. No row means "not
-- tracked", which is the normal state and never an error: plenty of what
-- this app holds is not in either catalogue, and a scanned folder has no
-- id anyone else would recognise. So a link is always something a person
-- chose from search results, never a guess -- an automatic title match
-- would eventually push one series' progress onto another's entry, which
-- is not a mistake the reader can see happening.
--
-- series_id XOR novel_id, the same shape text_replacements uses, because
-- a link belongs to exactly one of the two and two nullable columns with
-- no constraint would eventually hold both.
CREATE TABLE IF NOT EXISTS tracker_links (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tracker             TEXT NOT NULL,
    series_id           INTEGER REFERENCES series(id) ON DELETE CASCADE,
    novel_id            INTEGER REFERENCES novels(id) ON DELETE CASCADE,
    remote_id           TEXT NOT NULL,
    remote_title        TEXT NOT NULL,
    remote_url          TEXT NOT NULL,
    -- What this app last got the service to accept, per FIELD. Both
    -- services keep chapters and volumes as separate counts, and a web
    -- novel moves in both ("vol 3 ch 41"), so one column could not say
    -- which of the two a number belonged to -- it held the chapter for a
    -- chapter-numbered series and the volume for a volume-numbered one,
    -- and had no answer at all for something that advances in both.
    --
    -- These are a cache of the service's own state, not the source of
    -- truth: a push reads the remote value first, because the reader may
    -- have moved the entry on the site itself and writing our number over
    -- a higher one would undo that. What they ARE for is telling "nothing
    -- new to send" from "we have never sent anything", with no request.
    last_pushed_chapter REAL,
    last_pushed_volume  REAL,
    -- The shelf's own status as this app last got the service to hear it,
    -- in this app's vocabulary ('completed', 'on_hold', ...; NULL for
    -- never told). It is what makes each change of status a thing that is
    -- said ONCE: without it the backfill would re-announce every shelf on
    -- every start, and an ordinary push -- which runs whenever a page is
    -- turned -- would carry a status nobody changed. A status picked by
    -- hand in the editor records the SHELF's status here too, so the next
    -- push does not put the shelf's back over it.
    last_pushed_status  TEXT,
    last_pushed_at      TEXT,
    -- The last push's failure, kept so the series page can show it. NULL
    -- once a push succeeds.
    last_error          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((series_id IS NOT NULL) <> (novel_id IS NOT NULL)),
    UNIQUE (user_id, tracker, series_id),
    UNIQUE (user_id, tracker, novel_id)
);
CREATE INDEX IF NOT EXISTS idx_tracker_links_series ON tracker_links(user_id, series_id);
CREATE INDEX IF NOT EXISTS idx_tracker_links_novel ON tracker_links(user_id, novel_id);

CREATE TABLE IF NOT EXISTS text_replacements (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_id       INTEGER REFERENCES series(id) ON DELETE CASCADE,
    novel_id        INTEGER REFERENCES novels(id) ON DELETE CASCADE,
    find_text       TEXT NOT NULL,
    replace_text    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((series_id IS NOT NULL) <> (novel_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_text_replacements_series
    ON text_replacements(user_id, series_id);
CREATE INDEX IF NOT EXISTS idx_text_replacements_novel
    ON text_replacements(user_id, novel_id);

-- Valid text evidence is independent of lifetime recorded time. Global totals
-- survive removed content; per-work totals cascade so reused IDs inherit none.
CREATE TABLE IF NOT EXISTS account_pace_totals (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 words INTEGER NOT NULL, seconds REAL NOT NULL, samples INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS item_pace_totals (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 words INTEGER NOT NULL, seconds REAL NOT NULL, samples INTEGER NOT NULL,
 PRIMARY KEY(user_id,item_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS novel_pace_totals (
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
 words INTEGER NOT NULL, seconds REAL NOT NULL, samples INTEGER NOT NULL,
 PRIMARY KEY(user_id,novel_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS derived_stats_state (
 name TEXT PRIMARY KEY, version INTEGER NOT NULL
) WITHOUT ROWID;

-- Revision tokens avoid reading chapter bodies just to validate an ETA snapshot.
CREATE TABLE IF NOT EXISTS novel_content_revisions (
    novel_id INTEGER PRIMARY KEY REFERENCES novels(id) ON DELETE CASCADE,
    revision TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS estimate_chapter_insert AFTER INSERT ON chapters BEGIN
    INSERT INTO novel_content_revisions VALUES(NEW.novel_id,lower(hex(randomblob(16))))
    ON CONFLICT(novel_id) DO UPDATE SET revision=lower(hex(randomblob(16)));
END;
CREATE TRIGGER IF NOT EXISTS estimate_chapter_delete AFTER DELETE ON chapters BEGIN
    INSERT INTO novel_content_revisions SELECT OLD.novel_id,lower(hex(randomblob(16))) WHERE EXISTS(SELECT 1 FROM novels WHERE id=OLD.novel_id)
    ON CONFLICT(novel_id) DO UPDATE SET revision=lower(hex(randomblob(16)));
END;
CREATE TRIGGER IF NOT EXISTS estimate_chapter_update
AFTER UPDATE OF content_html,volume_id,order_index,novel_id ON chapters BEGIN
    INSERT INTO novel_content_revisions VALUES(NEW.novel_id,lower(hex(randomblob(16))))
    ON CONFLICT(novel_id) DO UPDATE SET revision=lower(hex(randomblob(16)));
    UPDATE novel_content_revisions SET revision=lower(hex(randomblob(16))) WHERE novel_id=OLD.novel_id AND OLD.novel_id<>NEW.novel_id;
END;
