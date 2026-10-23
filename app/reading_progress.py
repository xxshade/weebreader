"""Shared reading_progress INSERT ... ON CONFLICT statements.

The single-item versions (reader/router.py's mark-read/unread, used from the
reader itself) and the whole-series versions (library/router.py's
mark-all-read/unread, used from the series page) previously duplicated the
same ON CONFLICT UPDATE clauses inline in both router modules. Centralized
here since both are really the same two operations ("mark completed" /
"mark unread") just applied to a different row selection.
"""


async def mark_item_read(db, user_id: int, item_id: int, last_page: int | None) -> None:
    """Marking read is a DECLARATION, and `completed_from_percent` is what
    records it as one: how far the reader had actually got at the moment
    they said "done". Zero for a book never opened, whatever the bar said
    for one abandoned partway, ~100 for one read to the end.

    Without it the three are indistinguishable a second later -- this
    statement sets progress_percent to 100 in all three cases -- and the
    statistics that measure TIME cannot tell "this volume took four hours"
    from "I gave up on this volume after ten minutes and ticked it off".
    See stats/queries.time_to_finish and the rate in stats/estimates.py.
    """
    await db.execute(
        """
        INSERT INTO reading_progress
            (user_id, item_id, status, current_page, current_location, progress_percent,
             last_read_at, completed_at, completed_from_percent)
        VALUES (?, ?, 'completed', ?, NULL, 100, datetime('now'), datetime('now'), 0)
        ON CONFLICT(user_id, item_id) DO UPDATE SET
            status = 'completed',
            current_page = excluded.current_page,
            -- Cleared WITH the page. An EPUB marked read gets no page (its
            -- position is a spine index plus a paragraph), and the old
            -- paragraph kept on its own was applied to spine 0: a blank
            -- page reading "41 / 3" on the next open.
            current_location = excluded.current_location,
            progress_percent = 100,
            last_read_at = datetime('now'),
            -- Kept on a row already completed, like the percentage below:
            -- it is the date the trackers are sent as the finish.
            completed_at = CASE
                WHEN reading_progress.status = 'completed' THEN reading_progress.completed_at
                ELSE datetime('now') END,
            -- The row's OWN percentage, read before this statement
            -- overwrites it with 100. An already-completed row keeps what
            -- it recorded the first time: re-marking something read is not
            -- a claim to have re-read it.
            completed_from_percent = CASE
                WHEN reading_progress.status = 'completed'
                    THEN reading_progress.completed_from_percent
                ELSE reading_progress.progress_percent END
        """,
        (user_id, item_id, last_page),
    )


async def mark_item_unread(db, user_id: int, item_id: int) -> None:
    """Puts an item back to Unread -- which is the item with no row at all.

    There is one unread, not two (see GLOSSARY.md). An 'unread' row used
    to be written here, and it was indistinguishable from no row everywhere
    except where it was wrong: the card's "clear" button appeared on every
    volume of a series marked unread, never opened included, and the row's
    `last_read_at` kept the series sorted as recently read on the home page.
    Deleting the row is what makes "marked unread" and "never opened" the
    same state in fact and not only in the interface.

    Marking read and then unread used to be the way to put something down,
    and it is still worth knowing what that cost: mark-read writes
    `completed_at` and status 'completed', and the statistics page averages
    how long a FINISHED item took over exactly those rows -- one glance
    entered that average as a whole volume.

    The `page_reads` rows are left alone -- that time really was spent, and
    "time read" should say so. What they stop doing is setting the reading
    rate: it pairs each item's seconds with the bytes its saved percentage
    implies, and with no row there is no percentage, so the item falls
    below MIN_SAMPLE_FRACTION and drops out of the sample (see
    stats/estimates.py). A one-minute glance at a 400-page book was never
    a fair measure of how fast anyone reads.
    """
    await db.execute(
        "DELETE FROM reading_progress WHERE user_id = ? AND item_id = ?", (user_id, item_id)
    )


async def mark_series_read(db, user_id: int, series_id: int) -> None:
    await db.execute(
        """
        INSERT INTO reading_progress
            (user_id, item_id, status, current_page, current_location, progress_percent,
             last_read_at, completed_at, completed_from_percent)
        SELECT ?, id, 'completed',
               CASE WHEN page_count IS NOT NULL THEN page_count - 1 ELSE NULL END,
               NULL, 100, datetime('now'), datetime('now'), 0
        FROM items
        WHERE series_id = ?
        ON CONFLICT(user_id, item_id) DO UPDATE SET
            status = 'completed',
            current_page = excluded.current_page,
            current_location = NULL,
            progress_percent = 100,
            last_read_at = datetime('now'),
            completed_at = CASE
                WHEN reading_progress.status = 'completed' THEN reading_progress.completed_at
                ELSE datetime('now') END,
            -- Same rule as mark_item_read, and this is where it matters
            -- most: one click here declares a whole shelf finished, and
            -- most of those volumes were never opened.
            completed_from_percent = CASE
                WHEN reading_progress.status = 'completed'
                    THEN reading_progress.completed_from_percent
                ELSE reading_progress.progress_percent END
        """,
        (user_id, series_id),
    )


async def mark_series_unread(db, user_id: int, series_id: int) -> None:
    """mark_item_unread for every item on the series page -- the ones whose
    file is missing are not on it, and keep what they had, as they always
    have."""
    await db.execute(
        "DELETE FROM reading_progress WHERE user_id = ? "
        "AND item_id IN (SELECT id FROM items WHERE series_id = ?)",
        (user_id, series_id),
    )


async def mark_series_read_up_to(db, user_id: int, series_id: int, number: float) -> int:
    """Mark every numbered item at or below `number` as read.

    What a tracker's own count means when it is ahead of this shelf: the
    reader read those volumes, somewhere else or before this app existed.
    Adopting it is the only way the two ever agree -- the push refuses to
    send a number lower than the service's, so a shelf that stays behind
    stays silent forever.

    It only ever ADDS. A row already 'completed' is left exactly as it is,
    which matters twice: `completed_at` and `completed_from_percent` are
    what the statistics read (re-stamping them would say a book finished
    in April was finished today), and `last_read_at` orders the home
    page's "Reading" strip, which would otherwise be reshuffled by a
    tracker link. An unnumbered item contributes nothing rather than
    being swept up by a `<=` it cannot answer.

    Returns how many items it actually marked, so the panel can say what
    it did rather than claiming something happened when nothing did.
    """
    cursor = await db.execute(
        """
        INSERT INTO reading_progress
            (user_id, item_id, status, current_page, current_location, progress_percent,
             last_read_at, completed_at, completed_from_percent)
        SELECT ?, id, 'completed',
               CASE WHEN page_count IS NOT NULL THEN page_count - 1 ELSE NULL END,
               NULL, 100, datetime('now'), datetime('now'), 0
        FROM items
        WHERE series_id = ?
          AND number_in_series IS NOT NULL AND number_in_series <= ?
        ON CONFLICT(user_id, item_id) DO UPDATE SET
            status = 'completed',
            current_page = excluded.current_page,
            current_location = NULL,
            progress_percent = 100,
            last_read_at = datetime('now'),
            completed_at = datetime('now'),
            completed_from_percent = reading_progress.progress_percent
          WHERE reading_progress.status IS NOT 'completed'
        """,
        (user_id, series_id, number),
    )
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


async def mark_novel_read_up_to(db, user_id: int, novel_id: int, *,
                                chapter: float | None = None,
                                volume: float | None = None) -> int:
    """The web-novel half of the same idea, in both of its units.

    A novel is cited as "vol 3 ch 41" and a tracker can be ahead on either
    number, so both are adopted: every chapter at or below `chapter`, and
    every chapter of every volume at or below `volume` -- because "volume
    3 read" on the site means its chapters were read, which is exactly
    what store.novel_progress reads back out to decide the volume it
    reports.

    INSERT OR IGNORE, so a chapter already marked keeps the
    `read_from_percent` it first recorded: re-marking is not a claim to
    have re-read, and that column is what the reading-rate learning pairs
    seconds against. The same rule (and the same statement) as
    novels/router.py's _mark_chapter_read, which is the per-chapter path.
    """
    marked = 0
    for column, value in (("chapters.number", chapter), ("volumes.number", volume)):
        if value is None:
            continue
        cursor = await db.execute(
            f"""
            INSERT OR IGNORE INTO novel_read_chapters (user_id, chapter_id, read_from_percent)
            SELECT ?, chapters.id,
                   COALESCE((SELECT chapter_percent FROM novel_reading_progress
                              WHERE user_id = ? AND chapter_id = chapters.id), 0)
              FROM chapters
              JOIN volumes ON volumes.id = chapters.volume_id
             WHERE chapters.novel_id = ? AND {column} IS NOT NULL AND {column} <= ?
            """,
            (user_id, user_id, novel_id, value),
        )
        marked += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    return marked
