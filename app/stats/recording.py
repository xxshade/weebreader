"""Recording legacy page-visit spans; modern visits use stats/activity.py.

The shape of the data is described on `page_reads` in schema.sql; this is
the write side of it.

Two things are deliberately decided HERE and not by the client:

- **The cap.** The browser posts only what it measured; the server stamps
  the cap that was in force when the row landed. A client that could name
  its own cap could claim any amount of reading time, and more practically:
  the cap is a user setting, and the reader page may have been open since
  before it was last changed.
- **When the span happened.** The client sends `ago` -- how long ago the
  span ENDED, by its own monotonic clock -- rather than a wall-clock
  timestamp, so a device whose clock is wrong (or in another timezone)
  cannot file reading under the wrong day. The only thing taken from the
  browser is an elapsed duration, which is the one thing it can measure
  reliably.
"""

import logging

logger = logging.getLogger("stats")

# A span shorter than this is scrolling past, not reading. It matters
# mostly for the text reader's continuous mode, where the position changes
# once per paragraph as the page moves: without a floor, one flick through
# a chapter files dozens of rows worth a few tenths of a second each. The
# time given up is bounded by this value per page passed through, i.e.
# noise. reading-time.js applies the same floor before sending, so those
# spans never reach the network; this is the authority for the rule.
MIN_SPAN_SECONDS = 1.0

# A span longer than this is a clock jump or a suspended laptop, not a
# reading session. Clamped rather than dropped: the counted time is
# MIN(seconds, cap) and the cap is far below this either way, so the
# clamp changes no statistic -- it only keeps the RAW column meaningful,
# which is the column that makes the whole history recomputable under a
# different cap later.
MAX_SPAN_SECONDS = 6 * 60 * 60

# How long ago a span may claim to have ended and still be believed.
#
# Unlike the duration above, this one is REJECTED rather than clamped, and
# the difference matters: `ago` decides which day the row is filed under,
# so clamping an implausible value does not neutralise it -- it silently
# moves a reading session to a day it did not happen on, which is exactly
# what the daily charts would then be wrong about. Dropping the row loses
# one span; clamping it corrupts a chart.
#
# The bound is generous next to what the reader can actually produce: it
# flushes every 30 seconds and again when the page goes away, and a batch
# that fails to send is dropped rather than retried later (see
# reading-time.js), so `ago` is realistically seconds.
MAX_AGO_SECONDS = 60 * 60

# Bounds one request. The reader flushes far smaller batches than this;
# the limit is here so a malformed or hostile payload cannot turn one
# request into an unbounded write.
MAX_SPANS_PER_REQUEST = 200


def _clean(span: dict) -> dict | None:
    """One span from the wire, or None if it should not be stored."""
    try:
        seconds = float(span.get("seconds") or 0)
        ago = float(span.get("ago") or 0)
    except (TypeError, ValueError):
        return None
    if not (seconds >= MIN_SPAN_SECONDS) or not (0 <= ago <= MAX_AGO_SECONDS):
        return None
    seconds = min(seconds, MAX_SPAN_SECONDS)

    def _int(key):
        value = span.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    item_id = _int("item_id")
    chapter_id = _int("chapter_id")
    # Exactly one kind, matching the CHECK constraint on the table. Sending
    # both, or neither, is a bug in the caller rather than something to
    # guess at.
    if (item_id is None) == (chapter_id is None):
        return None
    return {
        "item_id": item_id,
        "chapter_id": chapter_id,
        "section_index": _int("section_index"),
        "page_index": _int("page_index"),
        "seconds": seconds,
        "ago": ago,
    }


async def record_spans(db, user_id: int, cap_seconds: int, spans: list) -> int:
    """Writes the spans worth keeping; returns how many were stored.

    A span whose item or chapter no longer exists is skipped, not an error:
    one removed while the reader was open used to fail the foreign key and
    take the whole batch -- every valid span with it -- down with it. A
    chapter's novel is read from `chapters`, never taken from the client.

    The caller owns the transaction and the commit, following the same
    convention as the rest of this app's write paths (see write_transaction
    in database.py -- it does not auto-commit).
    """
    cleaned = [c for c in (_clean(s) for s in spans[:MAX_SPANS_PER_REQUEST]) if c]
    if not cleaned:
        return 0

    def started(c):
        # The span STARTED (ago + seconds) ago: `ago` measures from its
        # end. Stored in UTC like every other timestamp here; the daily
        # grouping converts with 'localtime'.
        return f"-{c['ago'] + c['seconds']:.3f} seconds"

    # `kind` is resolved HERE, in the insert, rather than joined at read
    # time: the row outlives its item (ON DELETE SET NULL, see schema.sql)
    # and has to keep saying what it was.
    items = await db.executemany(
        """
        INSERT INTO page_reads
            (user_id, item_id, novel_id, chapter_id, kind, section_index, page_index,
             started_at, seconds, cap_seconds)
        SELECT ?, items.id, NULL, NULL, items.file_format, ?, ?, datetime('now', ?), ?, ?
          FROM items WHERE items.id = ?
        """,
        [(user_id, c["section_index"], c["page_index"], started(c), c["seconds"], cap_seconds,
          c["item_id"]) for c in cleaned if c["item_id"] is not None],
    )
    chapters = await db.executemany(
        """
        INSERT INTO page_reads
            (user_id, item_id, novel_id, chapter_id, kind, section_index, page_index,
             started_at, seconds, cap_seconds)
        SELECT ?, NULL, chapters.novel_id, chapters.id, 'novel', ?, ?, datetime('now', ?), ?, ?
          FROM chapters WHERE chapters.id = ?
        """,
        [(user_id, c["section_index"], c["page_index"], started(c), c["seconds"], cap_seconds,
          c["chapter_id"]) for c in cleaned if c["chapter_id"] is not None],
    )
    return max(items.rowcount, 0) + max(chapters.rowcount, 0)


async def rebuild_read_totals(db) -> None:
    """Recomputes item_read_totals / chapter_read_totals from page_reads.

    Those two tables are a cache of one query each, kept current by a
    trigger so they cannot drift (see schema.sql). This is the other half
    of that promise: the trigger accumulates one span at a time, this does
    the whole history in one pass, and the two have to agree. Where they
    ever did not, THIS is right -- page_reads is the history and the totals
    are derived from it, never the other way round.

    Called once by the migration that introduced the tables, to fill them
    on a database that already had reading behind it; and by the tests,
    which drive the real write paths and then compare.

    Only totals for content that still EXISTS are written, matching the
    ON DELETE CASCADE on both tables. A span outlives its item (page_reads
    uses SET NULL, deliberately -- deleting a comic does not un-read it),
    so the join is what keeps a rebuild producing exactly what the cascade
    left behind, rather than resurrecting totals for books that are gone.

    The caller owns the transaction and the commit, like record_spans.
    """
    await db.execute("DELETE FROM item_read_totals")
    await db.execute(
        """
        INSERT INTO item_read_totals (user_id, item_id, seconds)
        SELECT page_reads.user_id, page_reads.item_id,
               SUM(MIN(page_reads.seconds, page_reads.cap_seconds))
          FROM page_reads
          JOIN items ON items.id = page_reads.item_id
      GROUP BY page_reads.user_id, page_reads.item_id
        """
    )
    await db.execute("DELETE FROM chapter_read_totals")
    await db.execute(
        """
        INSERT INTO chapter_read_totals (user_id, chapter_id, seconds)
        SELECT page_reads.user_id, page_reads.chapter_id,
               SUM(MIN(page_reads.seconds, page_reads.cap_seconds))
          FROM page_reads
          JOIN chapters ON chapters.id = page_reads.chapter_id
      GROUP BY page_reads.user_id, page_reads.chapter_id
        """
    )
