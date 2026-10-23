"""The statistics page, and the endpoint the readers post page timings to."""

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.templating import templates
from app.auth.dependencies import get_current_user
from app.database import get_db, write_transaction
from app.http_utils import json_object
from app.stats import queries
from app.stats.recording import record_spans
from app.stats.activity import claim_session, record_batch
from app.status_labels import STATUS_LABELS

logger = logging.getLogger("stats")

router = APIRouter(tags=["stats"])

# The per-item reading status vocabulary, which is NOT the same set as the
# manual per-series tracking labels in status_labels.py -- three values
# fixed by reading_progress's CHECK constraint. Ordered read-first because
# that is the order the bars are stacked in.
ITEM_STATUS_LABELS = {
    "completed": "Read",
    "reading": "Reading",
    "unread": "Unread",
}

MEDIA_TYPE_LABELS = {
    "comic": "Comics",
    "ebook": "Ebooks",
    "mixed": "Mixed",
    # Not a library type: web novels are drawn in the Libraries section
    # beside the real ones, in the same shape (see _with_novels).
    "novel": "Web novels",
}

# What page_reads rows are split by in "time per page": a comic page is an
# image and a text page is a paragraph position, so averaging them together
# would describe neither.
KIND_LABELS = {
    "cbz": "Comic pages",
    "epub": "Ebook pages",
    "novel": "Web novel pages",
    "unknown": "Other",
}

# The two kinds of reading, for the tiles that would say nothing averaged
# together: a comic page is an image and a book page is prose, and a
# reader deciding whether they have an evening for the next volume needs
# them apart. Web novels sit with books because both are text -- what
# differs there is only the markup overhead per word (see
# stats/estimates.py, which learns them as separate rates for exactly that
# reason). A page_read whose item has since been deleted ('unknown') is in
# neither group and is left out of both tiles; the per-kind chart below
# still lists it, and "Time read" still counts it.
READING_GROUPS = (
    ("Comics", ("cbz",)),
    ("Books & web novels", ("epub", "novel")),
)

# The same buckets named as the "Most recent" filter offers them. Separate
# from KIND_LABELS on purpose: there the label sits beside a per-PAGE
# average and has to say what a page is, here it labels a filter over
# whole books, where "Comic pages" would read as the wrong noun.
RECENT_KIND_LABELS = {
    "cbz": "Comics",
    "epub": "Ebooks",
    "novel": "Web novels",
    "unknown": "Other",
}

# The daily charts stack one segment per kind, in this order from the
# baseline up, each in the validated palette slot beside it (.viz-N in
# theme.css). Fixed per KIND rather than handed out to whatever a window
# happens to hold, so a comic is the same blue in a week of nothing but
# comics as in a month of everything -- colour follows the thing, never
# its rank. Adjacent slots are adjacent segments, and the validator was
# run on exactly this order: 1-2-3-4, worst adjacent CVD separation 8.4
# (protan), against this app's #0a0a0a surface.
#
# "Other" is time spent on something since deleted (see
# test_deleted_item_stats.py). It is still time read that day, so it
# still has a hue: grey reads as "nothing", and fails the validator's
# lightness and chroma floors besides.
DAILY_KIND_SLOTS = {"cbz": 1, "epub": 2, "novel": 3, "unknown": 4}

# Month names spelled out rather than taken from strftime: %b is locale
# dependent and the container's locale is not something this page should
# be reading, while "%-d" (no zero padding) is glibc-only.
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _format_day(day: date) -> str:
    return f"{MONTH_ABBR[day.month - 1]} {day.day}, {day.year}"


def _resolve_window(days: int | None, start: str | None, end: str | None) -> dict:
    """What window the daily charts should cover, from the query string.

    Two shapes, and the explicit one wins: `from`/`to` are a custom range,
    anything else is the last `days` days. Returns the resolved dates, the
    preset that produced them (None for a custom range, which is what the
    control highlights), and the label the heading shows.

    A date that does not parse is a 400 rather than a silent fall back to
    the default: the chart would then be answering a different question
    than the URL asks, and there is no way to see that from the page.
    Everything else is CLAMPED instead, because those are all questions
    with a sensible nearest answer -- a range running into the future ends
    today (there is no reading yet to draw), a backwards range is the same
    range read the other way round, and one longer than MAX_WINDOW_DAYS is
    cut at its start, keeping the recent end the reader asked from.
    """
    today = date.today()
    if start or end:
        try:
            first = date.fromisoformat(start) if start else today
            last = date.fromisoformat(end) if end else today
        except ValueError:
            raise HTTPException(400, "Expected ISO dates (YYYY-MM-DD)")
        if first > last:
            first, last = last, first
        last = min(last, today)
        first = min(first, last)
        first = max(first, last - timedelta(days=queries.MAX_WINDOW_DAYS - 1))
        return {"start": first, "end": last, "preset": None,
                "label": f"{_format_day(first)} – {_format_day(last)}"}

    window = days or queries.DAILY_WINDOW_DAYS
    window = max(1, min(window, queries.MAX_WINDOW_DAYS))
    return {"start": today - timedelta(days=window - 1), "end": today,
            "preset": window if window in queries.DAILY_WINDOW_PRESETS else None,
            "label": f"Last {window} days"}


async def _daily_context(db, user_id: int, window: dict) -> dict:
    """Everything the daily-activity section needs, for the page and for
    the partial the range control swaps in -- one function so the two can
    never drift into charting different things."""
    daily = await queries.daily_activity(db, user_id,
                                         start=window["start"], end=window["end"])
    for day in daily:
        day["label"] = _format_day(date.fromisoformat(day["day"]))
        day['comic_visits'] = sum(v['pages'] for k, v in day['kinds'].items() if k == 'cbz')
        for kind, values in day['kinds'].items():
            values['comic_visits'] = values['pages'] if kind == 'cbz' else 0
    # Only the kinds this window actually holds -- a legend entry for a
    # colour that is nowhere on the chart is a question with no answer --
    # but always in DAILY_KIND_SLOTS order, so the legend and the stack
    # read the same way up.
    present = {kind for day in daily for kind in day["kinds"]}
    return {
        "daily": daily,
        "daily_kinds": [k for k in DAILY_KIND_SLOTS if k in present]
                       + sorted(present - DAILY_KIND_SLOTS.keys()),
        "daily_kind_slots": DAILY_KIND_SLOTS,
        "daily_kind_labels": RECENT_KIND_LABELS,
        "daily_window": window,
        "daily_presets": queries.DAILY_WINDOW_PRESETS,
        "today": date.today(),
        "max_window_days": queries.MAX_WINDOW_DAYS,
        # The chart's own scale. Computed here rather than in the
        # template because an empty history has to become 1 rather
        # than 0, and a division by zero inside a Jinja loop is a
        # 500 on a page whose whole job is to be reassuring.
        "daily_max_seconds": max([d["seconds"] for d in daily] or [0]) or 1,
        "daily_max_pages": max([d["comic_visits"] for d in daily] or [0]) or 1,
    }

# Which colour slot each status gets, fixed by status rather than by
# position in the chart. This is the whole point: a media type that happens
# to have no dropped series must not shift every later status onto a
# different colour, or the two charts on the page would disagree about what
# blue means. "none"/"unread" are an absence rather than a category and get
# the de-emphasis grey (see .viz-none in theme.css), which also keeps the
# real statuses inside the six validated slots.
STATUS_VIZ_SLOT = {
    "reading": 1,
    "completed": 2,
    "dropped": 3,
    "stopped_reading": 4,
    "on_hold": 5,
    "plan_to_read": 6,
    "unread": "none",
    "none": "none",
}


def _bucket(rows, labels) -> dict:
    """Turns the (media_type, status, n) rows the queries return into
    {media_type: {"total": n, "statuses": [{label, value, key}, ...]}}.

    Done here rather than in SQL because the template wants the statuses in
    a fixed display order with the zero ones dropped, and a query cannot
    produce a row for a status nothing has.
    """
    out: dict[str, dict] = {}
    for row in rows:
        out.setdefault(row["media_type"], {})[row["status"]] = row["n"]
    result = {}
    for media_type, counts in out.items():
        ordered = [
            {"key": key, "label": label, "value": counts.get(key, 0)}
            for key, label in labels.items()
            if counts.get(key, 0)
        ]
        # Anything the vocabulary does not name -- "none" for an untracked
        # series, or a value written before a label existed for it. Kept
        # visible rather than silently dropped, so the parts always add up
        # to the total shown beside them.
        leftover = sum(n for key, n in counts.items() if key not in labels)
        if leftover:
            ordered.append({"key": "none", "label": "No status", "value": leftover})
        result[media_type] = {"total": sum(counts.values()), "statuses": ordered}
    return result


def _with_novels(media_totals, series_status, item_status, novels) -> list[dict]:
    """Web novels as one more block of the Libraries section.

    They used to be a section of their own with a different look -- two
    tiles and a legend with no bar -- for no better reason than living in
    other tables. Same two bars now: novels by tracking status, and
    chapters by reading status (a chapter is what a web novel keeps its
    reading status for, see Chapter in GLOSSARY.md). Each block names its
    own nouns, since "series" and "items" are the wrong words here.
    """
    blocks = [{**total, "series_noun": "Series", "item_noun": "Items"} for total in media_totals]
    if novels["novels"]:
        series_status["novel"] = _bucket(
            [{"media_type": "novel", **row} for row in novels["by_status"]], STATUS_LABELS)["novel"]
        item_status["novel"] = _bucket(
            [{"media_type": "novel", **row} for row in novels["by_chapter_status"]],
            ITEM_STATUS_LABELS)["novel"]
        blocks.append({"media_type": "novel", "series_noun": "Novels", "item_noun": "Chapters"})
    return blocks


@router.get("/stats/daily-activity", response_class=HTMLResponse)
async def daily_activity_partial(request: Request, days: int | None = None,
                                 from_: str | None = Query(default=None, alias="from"),
                                 to: str | None = None,
                                 user=Depends(get_current_user), db=Depends(get_db)):
    """The daily charts alone, re-rendered for another window.

    Fetched by the range control so changing the window swaps a chart
    instead of reloading the page. Unlike the filters elsewhere on this
    page, this one CANNOT be client-side: a longer window is data the
    browser was never sent, and shipping a year of days to every visit so
    that the 90-day button feels instant would be paying on every load for
    a button most visits never press.

    The buttons are still ordinary links to /stats, so the section works
    with no JavaScript and a window stays shareable as a URL.
    """
    window = _resolve_window(days, from_, to)
    return templates.TemplateResponse(
        request, "_daily_activity.html", await _daily_context(db, user["id"], window))


def _grouped_per_page(per_kind: list[dict]) -> list[dict]:
    """The per-page average, rolled up into the two groups above.

    Built from the same rows the per-kind chart draws, so the tile and the
    chart cannot disagree about what was read -- one query, two readings
    of it. A group nothing was read in still gets an entry, carrying zero
    pages, so the tile can say so rather than vanishing and leaving the
    row lopsided.
    """
    by_kind = {row["kind"]: row for row in per_kind}
    groups = []
    for label, kinds in READING_GROUPS:
        seconds = sum(by_kind[k]["seconds"] for k in kinds if k in by_kind)
        pages = sum(by_kind[k]["pages"] for k in kinds if k in by_kind)
        groups.append({
            "label": label, "pages": pages,
            "seconds_per_page": (seconds / pages) if pages else 0,
        })
    return groups


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, days: int | None = None,
                     from_: str | None = Query(default=None, alias="from"),
                     to: str | None = None,
                     user=Depends(get_current_user), db=Depends(get_db)):
    daily_context = await _daily_context(db, user["id"], _resolve_window(days, from_, to))
    per_kind = await queries.seconds_per_page_by_kind(db, user["id"])
    pace = await queries.text_pace(db, user['id'])
    comic = next((k for k in per_kind if k['kind'] == 'cbz'), {'pages':0, 'seconds':0})
    recent = await queries.recent_items(db, user["id"])
    # Rows are scaled against the slowest kind, so the bars compare with
    # each other rather than each filling its own row.
    kind_max = max([(k["seconds"] / k["pages"]) for k in per_kind if k["pages"]] or [0]) or 1
    per_page_groups = _grouped_per_page(per_kind)
    series_status = _bucket(await queries.series_by_status(db, user["id"]), STATUS_LABELS)
    item_status = _bucket(await queries.items_by_status(db, user["id"]), ITEM_STATUS_LABELS)
    novels = await queries.novel_totals(db, user["id"])
    libraries = await queries.library_totals(db)
    media_blocks = _with_novels(queries.totals_by_media_type(libraries),
                                series_status, item_status, novels)
    return templates.TemplateResponse(
        request, "stats.html",
        {
            "user": user,
            "libraries": libraries,
            "media_totals": media_blocks,
            "series_status": series_status,
            "item_status": item_status,
            "novels": novels,
            "totals": await queries.reading_totals(db, user["id"]),
            "per_kind": per_kind,
            "pace": pace,
            "text_words": pace['words'],
            "comic_visits": comic['pages'],
            "comic_seconds_per_visit": comic['seconds'] / comic['pages'] if comic['pages'] else None,
            "kind_max": kind_max,
            "per_page_groups": per_page_groups,
            "finish": await queries.time_to_finish(db, user["id"]),
            **daily_context,
            "recent": recent,
            "recent_limit": queries.RECENT_LIMIT,
            # Only the buckets something was actually read in: a control
            # offering a filter that can only ever empty the list is
            # worse than no control, and with one bucket there is
            # nothing to choose between.
            "recent_kinds": [k for k in queries.RECENT_KINDS
                             if any(r["kind"] == k for r in recent)],
            "recent_kind_labels": RECENT_KIND_LABELS,
            "kind_labels": KIND_LABELS,
            "viz_slot": STATUS_VIZ_SLOT,
            "media_labels": MEDIA_TYPE_LABELS,
        },
    )


@router.post("/reading-session")
async def activate_reading_session(request: Request, user=Depends(get_current_user)):
    try:
        body = json_object(await request.json())
        async with write_transaction() as db:
            result = await claim_session(db, user['id'], body.get('session_id'), body.get('claim_id'))
            await db.commit()
        return result
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.post("/reading-time")
async def save_reading_time(request: Request, user=Depends(get_current_user)):
    """Stores page-visit spans measured by a reader.

    Called both with fetch() on a normal flush and with sendBeacon() when
    the page is going away -- which is why it answers a plain JSON body and
    never a redirect: a beacon's response is discarded, and the request must
    be cheap enough to complete during unload.
    """
    try:
        body = json_object(await request.json())
    except ValueError:
        raise HTTPException(400, "Expected a JSON body")
    if 'activities' in body:
        try:
            async with write_transaction() as db:
                result = await record_batch(db, user, body['activities'])
                await db.commit()
            return result
        except ValueError as error:
            raise HTTPException(400, str(error))
    spans = body.get("spans")
    if not isinstance(spans, list):
        raise HTTPException(400, "Expected a list of spans")

    async with write_transaction() as db:
        try:
            stored = await record_spans(db, user["id"], user["page_time_cap_seconds"], spans)
        except Exception:
            # A span about something that went away is skipped by
            # record_spans; anything else is not worth failing the reader's
            # unload over -- the timing is the least important thing it is
            # doing at that moment.
            logger.exception("Could not store reading-time spans for user %s", user["id"])
            return {"stored": 0}
        await db.commit()
    return {"stored": stored}
