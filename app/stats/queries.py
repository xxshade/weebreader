"""Aggregations behind the statistics page.

Kept apart from the router so every number on that page is a plain function
over a database connection -- which is what makes them testable without a
running server, the same reasoning as app/library/search.py.

One rule runs through all of it: **counted time is always
`MIN(seconds, cap_seconds)`**, never the raw `seconds`. A page left open
while the reader went to do something else would otherwise dominate every
average on the page. See schema.sql's comment on `page_reads` for why the
cap is stored per row rather than read from the user's current setting --
in short, so that changing the setting never silently rewrites history.

Dates are grouped in LOCAL time (`date(started_at, 'localtime')`) while the
values are stored in UTC, like every other timestamp in this schema. That
matters for a daily chart specifically: a page read at 21:00 local in a
UTC-3 zone is stored as the next day in UTC, so grouping on the raw value
would file a whole evening's reading under tomorrow. `localtime` resolves
against the CONTAINER's timezone, i.e. whatever `TZ` is set to in
docker-compose -- the same thing that already decides what `0 5 * * *`
means for the nightly scan.
"""

from datetime import date, timedelta

from app.library.series_scope import has_items

# The daily charts' default window. A month is long enough to show a habit
# and short enough that every bar is still wide enough to see on a phone.
DAILY_WINDOW_DAYS = 30

# The windows the chart's own control offers, longest last. 90 is where a
# bar stops being a bar on a phone and the chart starts reading as a
# density plot -- still useful for "when did I read", which is what a
# quarter-long window is asked for, but it is the last size that is.
DAILY_WINDOW_PRESETS = (30, 60, 90)

# A custom range is bounded because the chart draws one column per day and
# nothing has to stop the reader typing 1970 in the first box. A year of
# columns is already past legibility; ten thousand would be a page that
# takes seconds to lay out and says nothing.
MAX_WINDOW_DAYS = 366

# `min(seconds, cap_seconds)` per row, summed. Written once here because
# getting it wrong in one query and right in another would produce a page
# whose totals disagree with its own averages.
COUNTED = "SUM(MIN(page_reads.seconds, page_reads.cap_seconds))"
VISITS = "COUNT(DISTINCT COALESCE('visit:' || page_reads.visit_id, 'legacy:' || page_reads.id))"


async def text_pace(db, user_id):
    """One time-weighted reading pace for measured text in either format."""
    item = dict((await _rows(db, """SELECT COALESCE(SUM(words),0) AS words,
        COALESCE(SUM(pace_words),0) AS sample_words,
        COALESCE(SUM(pace_seconds),0) AS sample_seconds,
        COALESCE(SUM(pace_samples),0) AS samples, MIN(started_at) AS since
        FROM page_reads WHERE user_id=? AND visit_id IS NOT NULL
        AND kind IN ('epub','novel')""", (user_id,)))[0])
    enough = item['sample_words'] >= 1000 and item['sample_seconds'] >= 300 and item['samples'] >= 3
    item['wpm'] = 60 * item['sample_words'] / item['sample_seconds'] if enough else None
    return item

# "Most recent" answers "what have I been reading lately", and opening
# something to glance at it -- or tapping the wrong cover -- is not
# reading it. Anything under a minute of counted time is one of those, so
# it is left out rather than sitting at the top of the list purely because
# it happened last. The same floor MIN_SAMPLE_SECONDS uses in
# stats/estimates.py, and for the same reason: below it, the number says
# more about the tap than about the book.
MIN_RECENT_SECONDS = 60

# How many entries "Most recent" shows. Named rather than inlined because
# the filter below it has to honour the SAME number after switching: a
# filtered view showing three of the eight rows the unfiltered list held
# would be answering a different question ("of the last eight things,
# which were comics") than the one the control asks ("the last eight
# comics").
RECENT_LIMIT = 8

# The buckets "Most recent" can be filtered by, in the order the control
# offers them. An item's bucket is its own file_format rather than its
# library's media_type: a 'mixed' library holds both, so the library
# answers "either" for a row that is definitely one or the other.
RECENT_KINDS = ("cbz", "epub", "novel")


async def _rows(db, sql, params=()):
    cursor = await db.execute(sql, params)
    return await cursor.fetchall()


# ---------- Library composition ----------

async def library_totals(db) -> list[dict]:
    """Per library: how many series and how many items it holds.

    Two grouped queries rather than one join: a single query counting both
    with COUNT(DISTINCT ...) has to materialise series x items per library
    (115 x 2400 on this project's own library) to produce two numbers that
    do not depend on each other at all.
    """
    libraries = await _rows(
        db, "SELECT id, name, media_type FROM libraries ORDER BY name"
    )
    series_counts = {
        r["library_id"]: r["n"]
        for r in await _rows(db, f"SELECT library_id, COUNT(*) AS n FROM series WHERE {has_items()} GROUP BY library_id")
    }
    item_counts = {
        r["library_id"]: r["n"]
        for r in await _rows(
            db, "SELECT library_id, COUNT(*) AS n FROM items GROUP BY library_id"
        )
    }
    return [
        {
            "id": lib["id"],
            "name": lib["name"],
            "media_type": lib["media_type"],
            "series_count": series_counts.get(lib["id"], 0),
            "item_count": item_counts.get(lib["id"], 0),
        }
        for lib in libraries
    ]


def totals_by_media_type(per_library: list[dict]) -> list[dict]:
    """library_totals' counts rolled up by media_type, which is the division
    the statistics page leads with -- several libraries can share a type.
    Takes what library_totals returned rather than asking again: the page
    needs both, and asked twice for the same numbers."""
    rolled: dict[str, dict] = {}
    for lib in per_library:
        bucket = rolled.setdefault(
            lib["media_type"],
            {"media_type": lib["media_type"], "libraries": 0, "series_count": 0, "item_count": 0},
        )
        bucket["libraries"] += 1
        bucket["series_count"] += lib["series_count"]
        bucket["item_count"] += lib["item_count"]
    return sorted(rolled.values(), key=lambda b: b["media_type"])


async def series_by_status(db, user_id: int) -> list[dict]:
    """Series counted by the user's manual tracking label, per media type.

    A series with no label at all is reported as "none" rather than dropped:
    on a library that has never been curated that is every row, and a chart
    that silently showed nothing would read as a bug.
    """
    return [
        dict(r)
        for r in await _rows(
            db,
            f"""
            SELECT libraries.media_type,
                   COALESCE(series_status.status, 'none') AS status,
                   COUNT(*) AS n
              FROM series
              JOIN libraries ON libraries.id = series.library_id
         LEFT JOIN series_status
                ON series_status.series_id = series.id AND series_status.user_id = ?
             WHERE {has_items()}
          GROUP BY libraries.media_type, COALESCE(series_status.status, 'none')
            """,
            (user_id,),
        )
    ]


async def items_by_status(db, user_id: int) -> list[dict]:
    """Items counted by per-item reading status, per media type.

    No progress row means unread -- that is the same convention the library
    grid renders by, so the two agree.
    """
    return [
        dict(r)
        for r in await _rows(
            db,
            """
            SELECT libraries.media_type,
                   COALESCE(reading_progress.status, 'unread') AS status,
                   COUNT(*) AS n
              FROM items
              JOIN libraries ON libraries.id = items.library_id
         LEFT JOIN reading_progress
                ON reading_progress.item_id = items.id AND reading_progress.user_id = ?
          GROUP BY libraries.media_type, COALESCE(reading_progress.status, 'unread')
            """,
            (user_id,),
        )
    ]


async def novel_totals(db, user_id: int) -> dict:
    """Web novels counted the way the libraries are, for the same section.

    They are their own pipeline and their own tables, so the counts come
    from here rather than from library_totals -- but they are drawn beside
    the libraries, in the same shape: novels by tracking status, and
    CHAPTERS by reading status, a chapter being what a web novel's reading
    status is kept for (see Chapter in GLOSSARY.md). Read is a chapter
    marked read; Reading, the one each novel's saved position is in, if it
    is not marked; Unread, the rest."""
    counts = (
        await _rows(
            db,
            """
            SELECT (SELECT COUNT(*) FROM novels) AS novels,
                   (SELECT COUNT(*) FROM novels WHERE active = 1) AS active_novels,
                   (SELECT COUNT(*) FROM chapters) AS chapters,
                   (SELECT COUNT(*) FROM chapters WHERE content_html IS NOT NULL) AS downloaded,
                   (SELECT COUNT(*) FROM novel_read_chapters WHERE user_id = ?) AS read_chapters
            """,
            (user_id,),
        )
    )[0]
    by_status = [
        dict(r)
        for r in await _rows(
            db,
            """
            SELECT COALESCE(novel_status.status, 'none') AS status, COUNT(*) AS n
              FROM novels
         LEFT JOIN novel_status
                ON novel_status.novel_id = novels.id AND novel_status.user_id = ?
          GROUP BY COALESCE(novel_status.status, 'none')
            """,
            (user_id,),
        )
    ]
    chapter_status = (
        await _rows(
            db,
            """
            SELECT (SELECT COUNT(*) FROM novel_read_chapters
                     WHERE user_id = ?) AS completed,
                   (SELECT COUNT(*) FROM novel_reading_progress
                     WHERE user_id = ?
                       AND chapter_id NOT IN (SELECT chapter_id FROM novel_read_chapters
                                               WHERE user_id = ?)) AS reading
            """,
            (user_id, user_id, user_id),
        )
    )[0]
    by_chapter_status = [
        {"status": "completed", "n": chapter_status["completed"]},
        {"status": "reading", "n": chapter_status["reading"]},
        {"status": "unread",
         "n": counts["chapters"] - chapter_status["completed"] - chapter_status["reading"]},
    ]
    return {**dict(counts), "by_status": by_status, "by_chapter_status": by_chapter_status}


# ---------- Reading habits ----------

async def reading_totals(db, user_id: int) -> dict:
    """Headline numbers: how much time, over how many pages, since when."""
    row = (
        await _rows(
            db,
            f"""
            SELECT COALESCE({COUNTED}, 0) AS seconds,
                   {VISITS} AS pages,
                   COUNT(DISTINCT CASE WHEN MIN(seconds,cap_seconds)>0
                         THEN date(started_at, 'localtime') END) AS days,
                   MIN(started_at) AS first_at
              FROM page_reads
             WHERE user_id = ?
            """,
            (user_id,),
        )
    )[0]
    seconds = row["seconds"] or 0
    pages = row["pages"] or 0
    return {
        "seconds": seconds,
        "pages": pages,
        "days": row["days"] or 0,
        "first_at": row["first_at"],
        # The single number the whole feature exists to produce.
        "seconds_per_page": (seconds / pages) if pages else 0,
    }


async def seconds_per_page_by_kind(db, user_id: int) -> list[dict]:
    """Time per page split by what was being read.

    Worth splitting because the units are not comparable: a comic "page" is
    an image and a text "page" is a paragraph position, so averaging them
    together produces a number that describes neither.
    """
    return [
        dict(r)
        for r in await _rows(
            db,
            f"""
            -- page_reads.kind, not a join: a row outlives its item (see
            -- schema.sql) and this chart is where time spent on something
            -- since deleted can still be seen for what it was.
            SELECT page_reads.kind AS kind,
                   COALESCE({COUNTED}, 0) AS seconds,
                   {VISITS} AS pages
              FROM page_reads
             WHERE page_reads.user_id = ?
          GROUP BY kind
          ORDER BY kind
            """,
            (user_id,),
        )
    ]


# How far in a reader has to have actually got for a completion to count
# as one HERE. "Mark as read" is a declaration and can be made from
# anywhere -- see reading_progress.completed_from_percent -- and a book
# declared done at 12% took the time of 12% of a book, which is not an
# answer to "how long does one take". 90 rather than 100 because finishing
# normally lands a percentage point or two short: a reader who reaches the
# last page and taps the button has read the book.
FINISHED_FROM_PERCENT = 90


async def time_to_finish(db, user_id: int) -> dict:
    """How long one finished thing took, on average.

    Only FINISHED things are averaged, and that is the point: "how long an
    item takes to read" is not a question a half-read item can answer, and
    including them would drag every average down by however far the reader
    happened to get. An item is finished when its reading_progress says
    completed; a chapter, when novel_read_chapters has a row for it.

    Which is exactly why a completion has to be one the reader READ their
    way to. "Mark as read" is a declaration, available from any position
    and from none, and it sets progress_percent to 100 regardless -- so an
    item is admitted here only if `completed_from_percent` says the reader
    had actually reached the end (see reading_progress.mark_item_read). A
    NULL there is a row completed before that was recorded and is taken at
    its word, since nothing else about it is known; those thin out as new
    reading accumulates.

    An item never opened and then ticked off is already invisible to this
    average by having no `page_reads` at all -- the JOIN below drops it.
    The case this guards is the other one: abandoned partway, ticked off,
    and carrying real logged time that measures a fraction of a book.
    """
    # Split by FORMAT, because a comic and a book are not the same
    # question. One is looked at and the other is read: averaged together
    # the number describes neither, and it is the number a reader would
    # use to decide whether they have an evening for the next volume.
    # (The per-page average above splits for the same reason.) A removed
    # item counts under the format it had; a span whose item went before
    # removed items were kept has no verdict left and is in neither.
    item_rows = {
        row["kind"]: dict(row)
        for row in await _rows(
            db,
            f"""
            SELECT kind, COUNT(*) AS n, COALESCE(AVG(total), 0) AS avg_seconds,
                   COALESCE(SUM(total), 0) AS sum_seconds
              FROM (
                    SELECT page_reads.kind AS kind,
                           {COUNTED} AS total
                      FROM page_reads
                      JOIN reading_progress
                        ON reading_progress.item_id = page_reads.item_id
                       AND reading_progress.user_id = page_reads.user_id
                     WHERE page_reads.user_id = ?
                       AND page_reads.item_id IS NOT NULL
                       AND reading_progress.status = 'completed'
                       AND COALESCE(reading_progress.completed_from_percent, 100) >= ?
                  GROUP BY page_reads.item_id

                 UNION ALL

                    -- Removed items keep their verdict and their time
                    -- (see removed_items in schema.sql): a volume read
                    -- and then tidied off the disk took exactly as long.
                    SELECT removed_items.file_format AS kind,
                           removed_item_reads.seconds AS total
                      FROM removed_item_reads
                      JOIN removed_items ON removed_items.id = removed_item_reads.removed_item_id
                     WHERE removed_item_reads.user_id = ?
                       AND removed_item_reads.status = 'completed'
                       AND COALESCE(removed_item_reads.completed_from_percent, 100) >= ?
                   )
          GROUP BY kind
            """,
            (user_id, FINISHED_FROM_PERCENT, user_id, FINISHED_FROM_PERCENT),
        )
    }
    empty = {"n": 0, "avg_seconds": 0, "sum_seconds": 0}
    # A web novel's unit is the VOLUME, not the chapter. A chapter is a few
    # minutes long, so "how long one takes" said nothing you could plan an
    # evening with, and it sat next to a tile measuring whole books. A
    # volume is finished when every chapter in it is marked read (the
    # per-chapter equivalent of reading_progress.status = 'completed'
    # above), and its time is the summed counted time of its chapters.
    #
    # EVERY chapter of the volume has to be timed, not just one. A volume
    # read before timing existed would otherwise enter the average as
    # zero and drag it down (the same trap the reading-rate learning in
    # stats/estimates.py documents) -- and the stronger rule is what the
    # novel half needs against the declaration problem the item half
    # solves with completed_from_percent: "Mark all as read" on a volume
    # finishes all hundred of its chapters at once, and if two of them
    # were ever open, the summed time of those two is not how long that
    # volume took. There is no per-chapter percentage to consult here (a
    # novel stores one position, for the chapter it is in), so what stands
    # in for it is the requirement that the time cover the whole volume.
    volume_row = (
        await _rows(
            db,
            f"""
            WITH finished_volumes AS (
                    SELECT chapters.volume_id AS volume_id
                      FROM chapters
                 LEFT JOIN novel_read_chapters
                        ON novel_read_chapters.chapter_id = chapters.id
                       AND novel_read_chapters.user_id = ?
                  GROUP BY chapters.volume_id
                    HAVING COUNT(novel_read_chapters.chapter_id) = COUNT(*)
                 )
            SELECT COUNT(*) AS n, COALESCE(AVG(total), 0) AS avg_seconds,
                   COALESCE(SUM(total), 0) AS sum_seconds
              FROM (
                    -- chapters on the LEFT of the join, so an untimed one
                    -- still produces a row and can be counted: RIGHT JOIN
                    -- would say the same thing but needs SQLite 3.39+,
                    -- which nothing else here assumes.
                    SELECT SUM(chapter_seconds.seconds) AS total
                      FROM chapters
                 LEFT JOIN (SELECT page_reads.chapter_id AS id, {COUNTED} AS seconds
                              FROM page_reads
                             WHERE page_reads.user_id = ?
                               AND page_reads.chapter_id IS NOT NULL
                          GROUP BY page_reads.chapter_id) AS chapter_seconds
                        ON chapter_seconds.id = chapters.id
                     WHERE chapters.volume_id IN (SELECT volume_id FROM finished_volumes)
                  GROUP BY chapters.volume_id
                    HAVING COUNT(chapter_seconds.id) = COUNT(*)
                   )
            """,
            (user_id, user_id),
        )
    )[0]
    # NOT keyed "items"/"volumes": a Jinja attribute lookup tries getattr
    # before the mapping, and `dict.items` exists -- so `finish.items.n` in
    # the template silently resolved to the bound method and rendered as
    # "nothing finished yet" however much was finished. Caught in a real
    # browser, where the tile read "-" while the query returned 2.
    return {
        "per_comic": item_rows.get("cbz", dict(empty)),
        "per_book": item_rows.get("epub", dict(empty)),
        "per_volume": dict(volume_row),
    }


async def daily_activity(db, user_id: int, *, days: int = DAILY_WINDOW_DAYS,
                         today: date | None = None,
                         start: date | None = None,
                         end: date | None = None) -> list[dict]:
    """One entry per day over the window, oldest first.

    The window is a date RANGE, given either as an explicit `start`/`end`
    pair or as the last `days` days ending `today` -- the second is the
    first with the arithmetic done here, so both go down the same query.

    Days with no reading are filled in with zeros here rather than left out:
    a bar chart with the empty days missing silently compresses a gap into
    nothing, so a week off looks identical to a week of steady reading.

    Each day also carries `kinds`, the same two numbers split by
    `page_reads.kind` -- what the stacked columns draw, one segment per
    kind. Only kinds read that day are in it, and `seconds` and `pages`
    are their sums, so the column and its segments cannot disagree.
    `page_reads.kind` rather than a join, for the reason
    `seconds_per_page_by_kind` gives: a row outlives its item.

    The window is bounded twice on purpose -- once on the raw `started_at`,
    which is what the (user_id, started_at) index can actually use, and once
    on the local date, which is what the grouping means. The raw bound is
    deliberately a day wider at each end than needed so no timezone can push
    a wanted row outside it.
    """
    end = end or today or date.today()
    start = start or (end - timedelta(days=days - 1))
    rows = await _rows(
        db,
        f"""
        SELECT date(started_at, 'localtime') AS day,
               kind,
               COALESCE({COUNTED}, 0) AS seconds,
               {VISITS} AS pages
          FROM page_reads
         WHERE user_id = ?
           AND started_at >= ? AND started_at < ?
           AND date(started_at, 'localtime') BETWEEN ? AND ?
      GROUP BY day, kind
        """,
        (user_id,
         (start - timedelta(days=1)).isoformat(),
         (end + timedelta(days=2)).isoformat(),
         start.isoformat(), end.isoformat()),
    )
    by_day: dict[str, dict] = {}
    for r in rows:
        by_day.setdefault(r["day"], {})[r["kind"]] = {
            "seconds": r["seconds"] or 0, "pages": r["pages"] or 0}
    out = []
    for offset in range((end - start).days, -1, -1):
        key = (end - timedelta(days=offset)).isoformat()
        kinds = by_day.get(key, {})
        out.append({
            "day": key,
            "seconds": sum(k["seconds"] for k in kinds.values()),
            "pages": sum(k["pages"] for k in kinds.values()),
            "kinds": kinds,
        })
    return out


async def recent_items(db, user_id: int, *, limit: int = RECENT_LIMIT) -> list[dict]:
    """What the time was actually spent on lately -- the concrete half of
    the page, so the averages above have something to be checked against.

    One entry per SERIES, not per item. Short items made the per-item list
    useless exactly when it mattered: an evening of comic chapters filled
    all eight rows with one series and pushed everything else off. So the
    unit is the series -- its total time, ordered by when anything in it
    was last read -- and each entry carries its `items`, newest first, for
    the card to open onto. A web novel is one entry whose items are its
    VOLUMES (a chapter is too short to be worth a line). An item at the
    library root belongs to no series and is an entry of its own, with
    nothing to open.

    The total is everything ever read in that series, not a recent slice:
    the card answers "how much of this have I read", and its open list has
    to add up to the number on it.

    A series is its FOLDER, so a removed item (see removed_items in
    schema.sql) lands in the same entry as the items still there, under
    the series' current name when one still lives in that folder. And one
    folder holding comics and EPUBs is two entries, one per kind, so the
    kind filter keeps meaning one kind.

    Anything under MIN_RECENT_SECONDS is left out, judged on the ENTRY's
    total: a glance at a cover is not something read, but a 20-second
    chapter inside a series being read is, and is listed inside it.

    The result holds the top `limit` entries **per kind**, not the top
    `limit` overall, because the section's filter is applied in the
    browser with no refetch: the entries a filter can ever need have to
    already be in the DOM. The list is still ordered by recency across
    every kind, so the unfiltered view is exactly its first `limit`
    entries -- the global top `limit` cannot hold more than `limit` of any
    one kind, so it is always a subset of what is kept.
    """
    rows = []
    # Items still in the library. items.id IS the join, so an orphan span
    # (item gone, item_id NULL) is not in here -- its item is either a
    # removed item, below, or was removed before those were kept.
    for r in await _rows(
        db,
        f"""
        SELECT items.id AS id, items.title AS title, items.file_format AS kind,
               items.library_id AS library_id, series.folder_path AS folder_path,
               series.name AS series_name,
               COALESCE({COUNTED}, 0) AS seconds, {VISITS} AS pages,
               MAX(page_reads.started_at) AS last_at
          FROM page_reads
          JOIN items ON items.id = page_reads.item_id
     LEFT JOIN series ON series.id = items.series_id
         WHERE page_reads.user_id = ? AND page_reads.item_id IS NOT NULL
      GROUP BY page_reads.item_id
        """,
        (user_id,),
    ):
        rows.append({**dict(r), "removed": False,
                     "key": ("series", r["library_id"], r["folder_path"], r["kind"])
                     if r["folder_path"] is not None else ("item", r["id"])})
    # Removed items: already summed, once, when they went.
    for r in await _rows(
        db,
        """
        SELECT removed_items.id AS id, removed_items.title AS title,
               removed_items.file_format AS kind, removed_items.library_id AS library_id,
               removed_items.folder_path AS folder_path,
               COALESCE(series.name, removed_items.series_name) AS series_name,
               removed_item_reads.seconds AS seconds, removed_item_reads.pages AS pages,
               removed_item_reads.last_at AS last_at
          FROM removed_item_reads
          JOIN removed_items ON removed_items.id = removed_item_reads.removed_item_id
     LEFT JOIN series ON series.library_id = removed_items.library_id
                     AND series.folder_path = removed_items.folder_path
         WHERE removed_item_reads.user_id = ?
        """,
        (user_id,),
    ):
        rows.append({**dict(r), "removed": True,
                     "key": ("series", r["library_id"], r["folder_path"], r["kind"])
                     if r["folder_path"] is not None else ("removed", r["id"])})
    for r in await _rows(
        db,
        f"""
        SELECT volumes.id AS id, 'Volume ' || volumes.number AS title, 'novel' AS kind,
               novels.id AS novel_id, novels.title AS series_name,
               COALESCE({COUNTED}, 0) AS seconds, {VISITS} AS pages,
               MAX(page_reads.started_at) AS last_at
          FROM page_reads
          JOIN chapters ON chapters.id = page_reads.chapter_id
          JOIN volumes ON volumes.id = chapters.volume_id
          JOIN novels ON novels.id = volumes.novel_id
         WHERE page_reads.user_id = ? AND page_reads.chapter_id IS NOT NULL
      GROUP BY volumes.id
        """,
        (user_id,),
    ):
        rows.append({**dict(r), "removed": False, "key": ("novel", r["novel_id"])})

    entries: dict[tuple, dict] = {}
    for row in rows:
        entry = entries.get(row["key"])
        if entry is None:
            entry = entries[row["key"]] = {
                "name": row["series_name"] if row["key"][0] in ("series", "novel") else row["title"],
                "kind": row["kind"],
                # Something to open onto: a series or a novel. An item in
                # no series is the whole entry already.
                "opens": row["key"][0] in ("series", "novel"),
                "seconds": 0, "pages": 0, "last_at": "", "items": [],
            }
        entry["seconds"] += row["seconds"] or 0
        entry["pages"] += row["pages"] or 0
        entry["last_at"] = max(entry["last_at"], row["last_at"] or "")
        entry["items"].append({key: row[key] for key in
                               ("title", "seconds", "pages", "last_at", "removed")})

    kept = sorted((e for e in entries.values() if e["seconds"] >= MIN_RECENT_SECONDS),
                  key=lambda e: e["last_at"], reverse=True)
    shown_per_kind: dict[str, int] = {}
    result = []
    for entry in kept:
        if shown_per_kind.get(entry["kind"], 0) >= limit:
            continue
        shown_per_kind[entry["kind"]] = shown_per_kind.get(entry["kind"], 0) + 1
        entry["items"].sort(key=lambda item: item["last_at"] or "", reverse=True)
        result.append(entry)
    return result
