"""The tracking tables, and the one piece of real domain logic here:
what number a reader has actually reached.

No HTTP. Everything in this module is a query, so the rule that decides
what gets sent to a service can be tested against a real database with no
network at all -- which matters, because that rule is the only part of
tracking that can be wrong in a way nobody notices.
"""

import secrets
from dataclasses import dataclass

MANGAUPDATES = "mangaupdates"
MYANIMELIST = "myanimelist"


@dataclass(frozen=True)
class LocalProgress:
    """How far the reader has got, per FIELD.

    Both trackers keep chapters and volumes as separate counts, and the
    three things this app holds sit differently against that:

      * a scanned comic or light-novel series moves in ONE of the two, and
        which one is `series.number_kind` (decided for the whole series by
        resolve_series_numbers) -- a light novel is volumes, a chapter-per-
        file manga is chapters;
      * a web novel moves in BOTH: its chapters carry their own numbers and
        the source groups them into volumes, which is how such a novel is
        cited ("vol 3 ch 41"), so reporting only the chapter throws away
        half of what the entry records.

    Each is a MAX over what is marked read, never a count. A reader who
    marked volumes 1, 2 and 5 read is at 5 -- counting would say 3 and
    quietly walk their tracker backwards. It is also why an unnumbered item
    contributes nothing rather than zero.
    """

    chapter: float | None = None
    volume: float | None = None
    # What the shelf holds in total, in the unit it is mainly counted in --
    # shown next to the number so "12" reads as "12 of 40" rather than as a
    # bare figure.
    total: float | None = None
    # WHICH of the two units this shelf counts in ('chapter' or 'volume'),
    # or None for a web novel, which counts in both.
    #
    # Not inferable from the fields above, and that is the whole reason it
    # is carried: both are None until something is marked read, so a
    # reader deciding the unit by which field holds a number gets the
    # wrong answer on exactly the shelves where nothing has been read yet.
    # That was a real bug -- see service._adopt.
    kind: str | None = None

    @property
    def anywhere(self) -> bool:
        """Whether the reader has got anywhere at all. Nothing marked read
        is not the same as being at zero, and only one of the two is worth
        telling a service about."""
        return self.chapter is not None or self.volume is not None


async def series_progress(db, user_id: int, series_id: int) -> LocalProgress:
    cursor = await db.execute(
        """
        SELECT MAX(CASE WHEN reading_progress.status = 'completed'
                        THEN items.number_in_series END) AS reached,
               MAX(items.number_in_series)               AS total
          FROM items
          LEFT JOIN reading_progress
                 ON reading_progress.item_id = items.id
                AND reading_progress.user_id = ?
         WHERE items.series_id = ?
        """,
        (user_id, series_id),
    )
    row = await cursor.fetchone()
    cursor = await db.execute("SELECT number_kind FROM series WHERE id = ?", (series_id,))
    kind_row = await cursor.fetchone()
    reached = row["reached"] if row else None
    # A series numbered by volume reports volumes and says nothing about
    # chapters, and the other way round. Sending 0 for the field it does
    # not use would tell the service the reader had read none of them.
    kind = "volume" if kind_row and kind_row["number_kind"] == "volume" else "chapter"
    volume = kind == "volume"
    return LocalProgress(
        chapter=None if volume else reached,
        volume=reached if volume else None,
        total=row["total"] if row else None,
        kind=kind,
    )


async def novel_progress(db, user_id: int, novel_id: int) -> LocalProgress:
    """A web novel moves in BOTH units, and they are counted differently.

    The chapter is a MAX over the chapters marked read, the same rule
    everything else here follows. The VOLUME is not: a volume is reached
    only when every chapter in it has been read, so it is the highest
    volume with no unread chapter left. Taking a MAX over the volumes that
    merely CONTAIN a read chapter would report volume 3 finished after one
    chapter of it -- which is exactly the claim "mark as read" already
    taught this app not to make (see reading_progress.py).

    That is also why the two can disagree: a reader partway through volume
    3 is at "vol 2, ch 41", and both halves of that are true.
    """
    cursor = await db.execute(
        """
        SELECT MAX(CASE WHEN novel_read_chapters.chapter_id IS NOT NULL
                        THEN chapters.number END) AS reached,
               MAX(chapters.number)               AS total
          FROM chapters
          LEFT JOIN novel_read_chapters
                 ON novel_read_chapters.chapter_id = chapters.id
                AND novel_read_chapters.user_id = ?
         WHERE chapters.novel_id = ?
        """,
        (user_id, novel_id),
    )
    row = await cursor.fetchone()

    cursor = await db.execute(
        """
        SELECT MAX(volumes.number) AS reached
          FROM volumes
         WHERE volumes.novel_id = ?
           AND NOT EXISTS (
               SELECT 1 FROM chapters
                WHERE chapters.volume_id = volumes.id
                  AND chapters.id NOT IN (
                      SELECT chapter_id FROM novel_read_chapters WHERE user_id = ?
                  )
           )
           -- A volume with no chapters yet is not a volume anybody
           -- finished; without this it would satisfy "nothing unread".
           AND EXISTS (SELECT 1 FROM chapters WHERE chapters.volume_id = volumes.id)
        """,
        (novel_id, user_id),
    )
    volume_row = await cursor.fetchone()
    return LocalProgress(
        chapter=row["reached"] if row else None,
        volume=volume_row["reached"] if volume_row else None,
        total=row["total"] if row else None,
    )


# The value both `series_status` and `novel_status` store for "Finished"
# (see app/status_labels.py, where it is LABELLED "Finished" -- the word
# on screen and the word in the column are deliberately different, and
# this is the column's).
FINISHED = "completed"


def _shelf(series_id, novel_id):
    return (("series_status", "series_id", series_id) if series_id is not None
            else ("novel_status", "novel_id", novel_id))


async def local_status(db, user_id: int, *, series_id=None, novel_id=None) -> str | None:
    """The reader's own status for this shelf, in this app's vocabulary,
    or None when they have not given one.

    Only ever set by a person (the badge on the shelf's page): nothing in
    the app files a shelf by itself. That is what makes it safe to report
    to a catalogue -- it is the reader saying where the thing stands.
    """
    table, column, value = _shelf(series_id, novel_id)
    row = await (await db.execute(
        f"SELECT status FROM {table} WHERE user_id = ? AND {column} = ?",
        (user_id, value))).fetchone()
    return row["status"] if row else None


@dataclass(frozen=True)
class LocalDates:
    """When the reader started and finished a shelf HERE, as YYYY-MM-DD in
    the server's local time -- the same `date(..., 'localtime')` the
    statistics group days by."""

    start: str | None = None
    finish: str | None = None


async def local_dates(db, user_id: int, *, series_id=None, novel_id=None) -> LocalDates:
    """The start is the first sign of reading anywhere in the shelf: the
    earliest page read, or the earliest progress row (a volume marked read
    without being opened has no page reads, only a row). The finish exists
    only while the shelf is marked Finished, and is when its last item
    was completed -- or, with nothing completed here (read elsewhere,
    filed here), when it was marked Finished.
    """
    if series_id is not None:
        start_row = await (await db.execute(
            """
            SELECT MIN(first) AS first FROM (
                SELECT MIN(pr.started_at) AS first FROM page_reads pr
                  JOIN items i ON i.id = pr.item_id
                 WHERE pr.user_id = ? AND i.series_id = ?
                UNION ALL
                SELECT MIN(COALESCE(rp.last_read_at, rp.completed_at)) FROM reading_progress rp
                  JOIN items i ON i.id = rp.item_id
                 WHERE rp.user_id = ? AND i.series_id = ?
                UNION ALL
                SELECT MIN(rp.completed_at) FROM reading_progress rp
                  JOIN items i ON i.id = rp.item_id
                 WHERE rp.user_id = ? AND i.series_id = ?
            )
            """, (user_id, series_id) * 3)).fetchone()
        finish_row = await (await db.execute(
            """
            SELECT MAX(rp.completed_at) AS last FROM reading_progress rp
              JOIN items i ON i.id = rp.item_id
             WHERE rp.user_id = ? AND i.series_id = ? AND rp.status = 'completed'
            """, (user_id, series_id))).fetchone()
    else:
        start_row = await (await db.execute(
            """
            SELECT MIN(first) AS first FROM (
                SELECT MIN(started_at) AS first FROM page_reads
                 WHERE user_id = ? AND novel_id = ?
                UNION ALL
                SELECT MIN(nrc.read_at) FROM novel_read_chapters nrc
                  JOIN chapters c ON c.id = nrc.chapter_id
                 WHERE nrc.user_id = ? AND c.novel_id = ?
            )
            """, (user_id, novel_id) * 2)).fetchone()
        finish_row = await (await db.execute(
            """
            SELECT MAX(nrc.read_at) AS last FROM novel_read_chapters nrc
              JOIN chapters c ON c.id = nrc.chapter_id
             WHERE nrc.user_id = ? AND c.novel_id = ?
            """, (user_id, novel_id))).fetchone()

    finish = None
    table, column, value = _shelf(series_id, novel_id)
    status_row = await (await db.execute(
        f"SELECT status, updated_at FROM {table} WHERE user_id = ? AND {column} = ?",
        (user_id, value))).fetchone()
    if status_row and status_row["status"] == FINISHED:
        finish = (finish_row["last"] if finish_row else None) or status_row["updated_at"]
    start = start_row["first"] if start_row else None
    return LocalDates(start=await _day(db, start), finish=await _day(db, finish))


async def _day(db, timestamp) -> str | None:
    if not timestamp:
        return None
    row = await (await db.execute("SELECT date(?, 'localtime') AS d", (timestamp,))).fetchone()
    return row["d"]


async def links_status_unreported(db):
    """Links whose shelf has a status the service has not been told.

    The status backfill's whole query, and it follows the startup
    backfills' two rules: cheap once it has run, and a no-op on the second
    start. `last_pushed_status IS NOT status` is what enforces both -- a
    link that has been told carries the same word, so it drops out.

    A status with no counterpart on the service ("Stopped reading") stays
    in this answer; `local_status` rides along so the caller can skip it
    without a push.

    Series and novels are one UNION rather than two calls because the
    caller treats them identically: what comes back is a link, and a link
    already knows which of the two it points at.
    """
    cursor = await db.execute(
        """
        SELECT l.*, s.status AS local_status FROM tracker_links l
          JOIN series_status s ON s.series_id = l.series_id AND s.user_id = l.user_id
         WHERE l.last_pushed_status IS NOT s.status
        UNION ALL
        SELECT l.*, n.status AS local_status FROM tracker_links l
          JOIN novel_status n ON n.novel_id = l.novel_id AND n.user_id = l.user_id
         WHERE l.last_pushed_status IS NOT n.status
        ORDER BY id
        """
    )
    return await cursor.fetchall()


# ---------------------------------------------------------------- accounts


async def get_account(db, user_id: int, tracker: str):
    cursor = await db.execute(
        "SELECT * FROM tracker_accounts WHERE user_id = ? AND tracker = ?",
        (user_id, tracker),
    )
    return await cursor.fetchone()


async def save_account(
    db, user_id: int, tracker: str, *, access_token: str, username: str | None,
    refresh_token: str | None = None, expires_at: str | None = None,
) -> None:
    """Writes the SESSION. There is deliberately no password parameter: the
    only thing that ever sees one is mangaupdates.authenticate, in the
    request that trades it for this token."""
    await db.execute(
        """
        INSERT INTO tracker_accounts
            (user_id, tracker, username, access_token, refresh_token, expires_at, last_error)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(user_id, tracker) DO UPDATE SET
            username = excluded.username,
            access_token = excluded.access_token,
            refresh_token = excluded.refresh_token,
            expires_at = excluded.expires_at,
            last_error = NULL,
            updated_at = datetime('now')
        """,
        (user_id, tracker, username, access_token, refresh_token, expires_at),
    )


async def record_account_error(db, user_id: int, tracker: str, message: str) -> None:
    await db.execute(
        "UPDATE tracker_accounts SET last_error = ?, updated_at = datetime('now') "
        "WHERE user_id = ? AND tracker = ?",
        (message, user_id, tracker),
    )


async def delete_account(db, user_id: int, tracker: str) -> None:
    """Signing out drops the LINKS too.

    A link is only meaningful against an account: keeping the rows would
    leave a series page saying it is tracked while nothing can be sent, and
    signing in as somebody else would silently adopt the previous account's
    matches.
    """
    await db.execute(
        "DELETE FROM tracker_links WHERE user_id = ? AND tracker = ?", (user_id, tracker)
    )
    await db.execute(
        "DELETE FROM tracker_accounts WHERE user_id = ? AND tracker = ?", (user_id, tracker)
    )


# --------------------------------------------------- oauth, mid-handshake


async def save_pending_oauth(db, user_id: int, tracker: str, *, state: str, verifier: str) -> None:
    await db.execute(
        """
        INSERT INTO tracker_oauth_pending (user_id, tracker, state, code_verifier)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, tracker) DO UPDATE SET
            state = excluded.state,
            code_verifier = excluded.code_verifier,
            created_at = datetime('now')
        """,
        (user_id, tracker, state, verifier),
    )


async def take_pending_oauth(db, user_id: int, tracker: str, state: str) -> str | None:
    """The verifier for a callback carrying this `state`, consumed.

    Deleted whether or not the state matched: a handshake gets exactly one
    callback, so leaving the row would let a replayed redirect be redeemed
    a second time. A mismatched state returns None and the caller refuses
    -- that is the check that says this callback belongs to the handshake
    this app started.
    """
    row = await (await db.execute(
        "SELECT state, code_verifier FROM tracker_oauth_pending WHERE user_id = ? AND tracker = ?",
        (user_id, tracker),
    )).fetchone()
    await db.execute(
        "DELETE FROM tracker_oauth_pending WHERE user_id = ? AND tracker = ?", (user_id, tracker))
    if row is None or not secrets.compare_digest(row["state"], state):
        return None
    return row["code_verifier"]


# ------------------------------------------------------------------- links


# The column name in the two queries below is chosen between two
# literals by which argument was passed, never taken from a request --
# `series_id XOR novel_id` is the table's own shape (see schema.sql), and
# a WHERE that named both would need one of them to be NULL-safe.
async def get_link(db, user_id: int, tracker: str, *, series_id=None, novel_id=None):
    cursor = await db.execute(
        f"""SELECT * FROM tracker_links
             WHERE user_id = ? AND tracker = ?
               AND {"series_id" if series_id is not None else "novel_id"} = ?""",
        (user_id, tracker, series_id if series_id is not None else novel_id),
    )
    return await cursor.fetchone()


async def links_for(db, user_id: int, *, series_id=None, novel_id=None):
    """Every tracker this series or novel is linked to.

    A reader can keep an entry on both services, and the two are
    independent: one signed out or refusing must not stop the other being
    told, which is why the push loops over these rather than picking one.
    """
    cursor = await db.execute(
        f"""SELECT * FROM tracker_links
             WHERE user_id = ?
               AND {"series_id" if series_id is not None else "novel_id"} = ?
             ORDER BY tracker""",
        (user_id, series_id if series_id is not None else novel_id),
    )
    return await cursor.fetchall()


async def link_counts(db, user_id: int) -> tuple[dict, dict]:
    """How many services each series and each novel is tied to.

    One grouped query for the whole home grid rather than a count per
    card, the same batching every other per-card number there uses
    (statuses, item counts, last-read times).

    DISTINCT tracker because the count is answering "how many SERVICES",
    which is what the colour on screen means; a duplicate row for one
    service would read as both.
    """
    cursor = await db.execute(
        """SELECT series_id, novel_id, COUNT(DISTINCT tracker) AS n
             FROM tracker_links WHERE user_id = ?
            GROUP BY series_id, novel_id""",
        (user_id,),
    )
    series, novels = {}, {}
    for row in await cursor.fetchall():
        if row["series_id"] is not None:
            series[row["series_id"]] = row["n"]
        elif row["novel_id"] is not None:
            novels[row["novel_id"]] = row["n"]
    return series, novels


async def links_missing_summary(db):
    """Links to a SERIES that has never had a description fetched.

    `source_summary IS NULL` is the whole condition, and it means "never
    asked" rather than "has none": a series the catalogue had nothing for
    is written as an empty string, so it is not asked again on every
    start. That is what makes this cheap in the steady state -- one
    indexed scan returning nothing.

    Novels are not here: their synopsis comes from the site they were
    scraped from, which is a better source than a catalogue entry and is
    already backfilled by their own sync.
    """
    cursor = await db.execute(
        """SELECT l.* FROM tracker_links l
             JOIN series s ON s.id = l.series_id
            WHERE l.series_id IS NOT NULL AND s.source_summary IS NULL
            ORDER BY l.series_id, l.tracker"""
    )
    return await cursor.fetchall()


async def save_link(
    db, user_id: int, tracker: str, *, series_id=None, novel_id=None,
    remote_id: str, remote_title: str, remote_url: str,
) -> None:
    """Replaces whatever was linked before.

    Re-linking to a different entry clears `last_pushed_number` with the
    row: that number describes the OLD entry's state, and carrying it over
    would make the first push to the new one decide there was nothing to
    send.
    """
    await delete_link(db, user_id, tracker, series_id=series_id, novel_id=novel_id)
    await db.execute(
        """
        INSERT INTO tracker_links
            (user_id, tracker, series_id, novel_id, remote_id, remote_title, remote_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, tracker, series_id, novel_id, remote_id, remote_title, remote_url),
    )


async def delete_link(db, user_id: int, tracker: str, *, series_id=None, novel_id=None) -> None:
    await db.execute(
        f"""DELETE FROM tracker_links
             WHERE user_id = ? AND tracker = ?
               AND {"series_id" if series_id is not None else "novel_id"} = ?""",
        (user_id, tracker, series_id if series_id is not None else novel_id),
    )


async def record_push(db, link_id: int, *, chapter: float | None, volume: float | None,
                      status: str | None = None) -> None:
    """COALESCE, not assignment: a push that only moved the chapter must
    not blank out the volume this app already knows the service holds --
    that would make the next push ask about it again for nothing. The
    status follows the same rule for the same reason, and it is the one
    that would cost a request on every single page turn."""
    await db.execute(
        "UPDATE tracker_links SET "
        "last_pushed_chapter = COALESCE(?, last_pushed_chapter), "
        "last_pushed_volume = COALESCE(?, last_pushed_volume), "
        "last_pushed_status = COALESCE(?, last_pushed_status), "
        "last_pushed_at = datetime('now'), last_error = NULL WHERE id = ?",
        (chapter, volume, status, link_id),
    )
    # And the ACCOUNT's error with it. An auth failure sets that one too
    # (record_account_error) and nothing else cleared it, so after one 401
    # and a successful push the settings page went on saying "sign in
    # again" until somebody did -- the schema says it is cleared on every
    # success.
    await db.execute(
        "UPDATE tracker_accounts SET last_error = NULL "
        "WHERE last_error IS NOT NULL AND (user_id, tracker) = "
        "(SELECT user_id, tracker FROM tracker_links WHERE id = ?)",
        (link_id,),
    )


async def record_link_error(db, link_id: int, message: str) -> None:
    await db.execute("UPDATE tracker_links SET last_error = ? WHERE id = ?", (message, link_id))


async def save_summary(db, series_id: int, text: str) -> bool:
    """The tracker's description, kept without overwriting an edit.

    The same arrangement novels use for a scraped title and synopsis (see
    sync.reconcile_editable_metadata): `source_summary` always follows the
    service, and the SHOWN summary follows it only while the two still
    agree. Once somebody has edited the description, a later fetch updates
    what the tracker says and leaves the page alone -- otherwise linking a
    second service, or re-linking after a mistake, would silently undo the
    edit, which reads as the app forgetting.

    Returns whether the shown text changed, so the caller can say that
    something happened rather than claiming it did.
    """
    row = await (
        await db.execute("SELECT summary, source_summary FROM series WHERE id = ?", (series_id,))
    ).fetchone()
    if row is None:
        return False
    edited = bool(row["summary"]) and row["summary"] != row["source_summary"]
    if edited:
        await db.execute(
            "UPDATE series SET source_summary = ? WHERE id = ?", (text, series_id))
        return False
    await db.execute(
        "UPDATE series SET summary = ?, source_summary = ? WHERE id = ?",
        (text, text, series_id))
    return row["summary"] != text
