import asyncio
import json
import logging
import urllib.parse
from collections import namedtuple
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.templating import templates
from app.library.series_scope import AUTOMATIC_ORDER, has_items, items_order

from app.auth.dependencies import get_current_user, get_streaming_user
# The bounds the blur slider is drawn with, taken from the endpoint that
# enforces them, so the control cannot offer a value its own save would
# refuse. app.auth.router imports nothing from here.
from app.auth.router import MAX_COVER_BLUR, MIN_COVER_BLUR
from app.background import run_background as _run_background
from app.covers.thumbnails import delete_custom_series_cover, save_custom_series_cover
from app.database import get_db, write_transaction, get_connection
from app.http_utils import back_target, is_htmx, json_object, redirect_back
from app.library import events, job_status
from app.library.naturalsort import natural_sort_key
from app.library.scanner import _recompute_series_cover, automatic_series_name, backfill_number_in_series, scan_library
from app.library.search import RESULT_LIMIT, search_items
from app.stats.estimates import build_estimator
from app.library.watcher import watcher
from app.novels.router import format_chapter_number, list_novels_for_settings
from app.reading_progress import mark_series_read as _mark_series_read
from app.reading_progress import mark_series_unread as _mark_series_unread
from app.status_badges import publication_badge, tracking_badge
from app.status_labels import PUBLICATION_STATUS_LABELS, novel_publication_status
from app.tracking import service as tracking
from app.tracking import models as tracker_models
from app.tracking import store as tracker_store
from app.tracking.router import handle_oauth_callback
from app.tracking.router import panel_context as tracker_panel_context
from app.status_labels import STATUS_LABELS as SERIES_STATUS_LABELS
from app import text_replacements, request_timing

logger = logging.getLogger("library")

router = APIRouter(tags=["library"])

async def _reading_items(db, user_id: int, estimator=None) -> list[dict]:
    """The "Reading" shortcut on the home page: items and novels with
    reading in progress for this user, most recent first.

    Factored out of home() so /library/reading-grid can rebuild exactly
    the same section when reading progress changes elsewhere (see
    library-events.js) -- which entries belong in it is a status question
    only the server can answer, so the client re-fetches this rather than
    reimplementing it.
    """
    # "Reading": items/chapters with reading in progress for the logged-in
    # user, most recent first -- to continue where you left off with one
    # click, without navigating to the series/novel page. Always shows
    # everything, regardless of the library selected in the Libraries
    # section (it's a "continue reading" shortcut, not a library listing).
    #
    # Comics/EPUBs use their own per-item "reading" status (reading_progress
    # .status). Novels have no equivalent per-item field -- a saved
    # position (novel_reading_progress) counts as "reading" there instead,
    # UNLESS the user has manually marked the whole novel completed/dropped
    # via the status selector on its page, in which case a leftover
    # progress row shouldn't keep it showing up here.
    cursor = await db.execute(
        """
        SELECT items.id, items.title, items.cover_path, items.page_count,
               items.file_format, items.text_size, items.series_id,
               reading_progress.status, reading_progress.progress_percent,
               reading_progress.last_read_at, reading_progress.current_page,
               COALESCE(series.blur_cover, 0) AS blur_cover
        FROM reading_progress
        JOIN items ON items.id = reading_progress.item_id
        LEFT JOIN series ON series.id = items.series_id
        WHERE reading_progress.user_id = ? AND reading_progress.status = 'reading'
        """,
        (user_id,),
    )
    reading_comics = await cursor.fetchall()

    cursor = await db.execute(
        """
        SELECT novels.id, novels.title, novels.cover_image_content_type, novels.cover_image_hash,
               novel_reading_progress.chapter_percent, novel_reading_progress.updated_at,
               chapters.id AS chapter_id, chapters.volume_id, chapters.number AS chapter_number,
               volumes.number AS volume_number,
               EXISTS (
                   SELECT 1 FROM chapters AS pending
                   LEFT JOIN novel_read_chapters AS marked
                     ON marked.chapter_id=pending.id AND marked.user_id=novel_reading_progress.user_id
                   WHERE pending.volume_id=chapters.volume_id AND marked.chapter_id IS NULL
               ) AS volume_has_unread
          FROM novel_reading_progress
          JOIN novels ON novels.id=novel_reading_progress.novel_id
          JOIN chapters ON chapters.id=novel_reading_progress.chapter_id
          JOIN volumes ON volumes.id=chapters.volume_id
          LEFT JOIN novel_status ON novel_status.user_id=novel_reading_progress.user_id
                                AND novel_status.novel_id=novel_reading_progress.novel_id
         WHERE novel_reading_progress.user_id=?
           AND (novel_status.status IS NULL OR novel_status.status='reading')
        """, (user_id,),
    )
    reading_novels = await cursor.fetchall()

    # One estimator for the whole strip: it loads the user's per-book totals
    # once, so asking it about each card costs nothing further. The home
    # page passes in the one it already built for the library grid below --
    # the same reader's totals answer both sections, and building it twice
    # per page load is two full passes over their reading history for one
    # set of numbers. /library/reading-grid renders this section alone and
    # has none to hand over, so it still builds its own.
    if estimator is None:
        estimator = await build_estimator(db, user_id, cached_only=True)

    await estimator.prepare_items(reading_comics)
    reading_items = []
    for it in reading_comics:
        # page_count is already known for every item since scan time (just
        # a directory listing of the archive, never decoded page images --
        # see library/parsers/cbz.py), and current_page is already saved
        # from the reader -- "pages remaining" is free to compute, no
        # extra work at request time, whether the comic is mid-read or not.
        pages_remaining = None
        if it["page_count"] and it["current_page"] is not None:
            pages_remaining = max(0, it["page_count"] - it["current_page"] - 1)
        reading_items.append({
            "kind": "item", "id": it["id"], "title": it["title"],
            "url": f"/reader/{it['id']}",
            "cover_url": f"/covers/{it['cover_path']}?private=1" if it["cover_path"] else None,
            # A blurred cover is the series' choice, and its volumes wear it
            # here as on the home grid: this strip is on the same page, in
            # front of whoever is looking at the screen.
            "series_id": it["series_id"],
            "blur_cover": bool(it["blur_cover"]),
            "progress_percent": it["progress_percent"],
            "pages_remaining": pages_remaining,
            # The row is both halves of what the estimator asks for: it
            # joins items to reading_progress, so it carries the format and
            # size on one side and the status and percentage on the other.
            "eta": estimator.for_item(it, it),
            "text_eta": it["file_format"] == "epub",
            "eta_state": estimator.item_states.get(it["id"], "complete"),
            # The estimate is about the item the card already names, so
            # there is nothing further to say (see the novel branch).
            "eta_scope": "",
            "last_read_at": it["last_read_at"],
            # What the ✕ on the card posts to (see _reading_grid.html) --
            # the two content types have different endpoints, and the card
            # markup is shared.
            "clear_url": f"/reader/{it['id']}/clear-status",
        })
    for nv in reading_novels:
        chnum = format_chapter_number(nv["chapter_number"])
        # The estimator counts actual words in this volume, anchored to the
        # current chapter/block/character position, independently of percent.
        await estimator.prepare_novel(nv["id"], nv["volume_id"])
        # A novel's card is about the VOLUME for the time left and the
        # CHAPTER for everything else, and the two are labelled so neither
        # can be read as the other. The percentage and the bar stay on the
        # chapter: a whole-novel percentage answered a question nobody
        # asked while looking at a card that opens one chapter -- finishing
        # chapter 1 of 8 showed "13%", true of the novel and useless about
        # the chapter in front of you. The TIME is the volume's, because
        # "have I got an evening for this" is a question about the volume,
        # which is the unit a novel is followed in.
        reading_items.append({
            "kind": "novel", "id": nv["id"], "title": nv["title"],
            "url": f"/novels/{nv['id']}/read/{nv['volume_number']}/{chnum}",
            "cover_url": (
                f"/novels/{nv['id']}/cover?v={nv['cover_image_hash']}"
                if nv["cover_image_hash"] else f"/novels/{nv['id']}/cover"
            ) if nv["cover_image_content_type"] else None,
            "progress_percent": round(nv["chapter_percent"] or 0),
            "pages_remaining": None,
            "chapter_id": nv["chapter_id"],
            # Volume first, chapter after it: the volume is what the time
            # beside them is about, the chapter is what the percentage is
            # about, and both are named so neither number is homeless.
            "chapter_label": f"Vol. {nv['volume_number']} · Ch. {chnum}",
            # Names what the estimate covers, for the card's tooltip. The
            # percentage sits between the two, so the one thing the line
            # must not do is leave the reader guessing which span the time
            # belongs to.
            "eta_scope": f"volume {nv['volume_number']}",
            # The stretch handed over is ONE VOLUME -- rest_bytes above is
            # grouped by volume_id. The same call on the home grid's novel
            # card hands over the whole novel (see _novel_reading).
            "eta": estimator.for_novel(nv["id"], nv["volume_id"]),
            "text_eta": bool(nv["volume_has_unread"]),
            "eta_state": estimator.novel_states.get((nv["id"], nv["volume_id"]), "complete"),
            "eta_volume_id": nv["volume_id"],
            "last_read_at": nv["updated_at"],
            "clear_url": f"/novels/{nv['id']}/clear-status",
        })
    reading_items.sort(key=lambda r: r["last_read_at"] or "", reverse=True)
    return reading_items


# The two numbers a home-grid card carries about READING, as opposed to
# about the shelf: how much of this is still unread, and how long that will
# take. Both answer the same question at the two scales the card has room
# for -- the badge over the cover counts it, the line under the name times
# it -- so they are built together from one pass and cannot disagree about
# what "unread" means.
#
# A namedtuple rather than a dict for the reason Estimate is one: Jinja
# resolves attribute access with getattr BEFORE the mapping, so a dict key
# named `items` or `keys` silently becomes the method (see AGENTS.md).
ShelfReading = namedtuple("ShelfReading", "unread remaining text_eta missing state", defaults=(False, 0, "complete"))

# The same pair for a novel, plus the one thing the card needs that comes
# out of the very same grouping of `chapters`: when its newest chapter
# turned up, which is a novel's `modified_at` (a novel row itself never
# changes after creation). Riding along rather than being its own GROUP BY
# over the same table for the same rows.
NovelShelf = namedtuple("NovelShelf", "unread remaining newest text_eta missing state", defaults=(False, 0, "complete"))

# What a card with nothing on that side of it shows: no badge (the template
# hides it at zero) and no time. A series whose every item is missing lands
# here, as does a novel added but not yet synced -- neither grouping
# produces a row for it at all, so the card needs an answer that is not a
# KeyError. A novel with no chapters also has no newest one, which sends
# `modified_at` back to its created_at, where it was before chapters
# existed to date it by.
NOTHING_LEFT = ShelfReading(unread=0, remaining=None)
NO_CHAPTERS = NovelShelf(unread=0, remaining=None, newest=None)


async def _series_reading(db, user_id: int, estimator, series_ids=None):
    """What each series still has waiting, keyed by series id.

    "Unread" is NOT COMPLETED, which is the same rule the time beside it
    follows: an item abandoned at 12% is one the reader still has to read,
    and `for_item` still has most of it left to count. A LEFT JOIN is what
    makes an item never opened say so -- it has no `reading_progress` row
    at all, and a NULL status is neither 'completed' nor a percentage.

    A completed item is left OUT by the query rather than skipped in the
    loop, which is what makes the two numbers one pass: it is not counted,
    and `for_item` answers None for it anyway, so the rows that survive are
    exactly the ones both halves are made of and the count is their length.
    It is also what this costs, because this is the only place the home
    page reads `items` in bulk and the bill is the marshalling of each row
    rather than the query: the plan is a scan of `items` with an indexed
    lookup into `reading_progress`, and a bare COUNT(*) over the same rows
    is 2 ms. Measured on 20,000 items with 30% of them finished: 61 ms
    fetching every row against 46 ms for the 14,000 that are unfinished,
    which is the whole difference between filtering here and filtering in
    the loop. A shelf read to the end costs nothing at all.

    `series_ids` narrows it to requested cards. None means the whole shelf;
    an empty collection means no work. One card retains its simple query.
    """
    selected = sorted(set(series_ids)) if series_ids is not None else None
    if selected == []:
        return {}
    scope, params = "", (user_id,)
    if selected is not None:
        scope = ("AND items.series_id = ?" if len(selected) == 1 else
                 "AND items.series_id IN (" + ','.join('?' for _ in selected) + ")")
        params = (user_id, *selected)
    phase_started = request_timing.start()
    rows = await (await db.execute(f"""
        SELECT items.id, items.series_id, items.file_format,
               items.text_size, items.page_count,
               reading_progress.status, reading_progress.progress_percent
          FROM items
     LEFT JOIN reading_progress
            ON reading_progress.item_id = items.id
           AND reading_progress.user_id = ?
         WHERE items.series_id IS NOT NULL
           AND (reading_progress.status IS NULL OR reading_progress.status <> 'completed')
           {scope}
    """, params)).fetchall()

    request_timing.finish("series_query", phase_started)
    phase_started = request_timing.start()
    with request_timing.item_preparation():
        await estimator.prepare_items(rows)
    request_timing.finish("series_prepare", phase_started)
    phase_started = request_timing.start()
    by_series: dict[int, list] = {}
    for row in rows:
        by_series.setdefault(row["series_id"], []).append(row)
    result = {
        sid: ShelfReading(unread=len(items), remaining=estimator.total_remaining(items),
                          text_eta=any(item["file_format"] == "epub" for item in items),
                          missing=estimator.missing_items(items), state=estimator.series_state(items))
        for sid, items in by_series.items()
    }

    request_timing.finish("series_aggregate", phase_started)
    return result


async def _novel_reading(db, user_id: int, estimator, novel_id: int | None = None):
    """Home-card scope: every unmarked chapter of the novel, across volumes.

    The inventory estimator owns word counts and the exact saved text anchor.
    This lightweight grouping shares the same unread definition for the badge
    and leaves unknown chapter content unknown rather than estimating from bytes.
    """
    scope = "WHERE chapters.novel_id=?" if novel_id is not None else ""
    params = (user_id, novel_id) if novel_id is not None else (user_id,)
    rows = await (await db.execute(f"""
        SELECT chapters.novel_id AS novel_id, MAX(chapters.discovered_at) AS newest,
               SUM(CASE WHEN marked.chapter_id IS NULL THEN 1 ELSE 0 END) AS unread
          FROM chapters
          LEFT JOIN novel_read_chapters AS marked
            ON marked.chapter_id=chapters.id AND marked.user_id=?
        {scope}
         GROUP BY chapters.novel_id
    """, params)).fetchall()
    shelf = {}
    for row in rows:
        await estimator.prepare_novel(row["novel_id"])
        estimate = estimator.for_novel(row["novel_id"])
        shelf[row["novel_id"]] = NovelShelf(
            unread=row["unread"] or 0,
            remaining=estimate.remaining if estimate else None,
            newest=row["newest"], text_eta=bool(row["unread"]),
            state=estimator.novel_states.get((row["novel_id"], None), "complete"),
        )
    return shelf


async def _load_series_items(db, user_id: int, series):
    """Every item of a series, plus the progress row for each.

    Unpaginated on purpose. This used to render 60 at a time, which cost
    the series page its own search: the box filters the cards already in
    the DOM, so it could only ever see the current page -- there was no
    way to find a volume without first knowing which page held it, and the
    placeholder had to admit as much ("this page only").

    Measured before dropping it, on a library the size of the one this
    runs against: the home page renders 115 series cards as 734 DOM nodes
    and loads in 75 ms, so a series' worth of items (tens, occasionally a
    few hundred) is not a size worth paginating. Rendering them all is
    what makes the existing client-side filter cover the whole series.

    Factored out so the htmx partial responses below (mark-all-read and
    mark-all-unread) can re-render exactly what the page is showing.
    """
    series_id = series["id"]

    # manual_sort is sticky -- once the user has dragged items into place,
    # that's the order shown from then on, whether or not the drag UI
    # itself (manual_sort_editing) is currently turned on.
    order_by = f"ORDER BY {items_order(series['manual_sort'])}"
    cursor = await db.execute(
        f"SELECT * FROM items WHERE series_id = ? {order_by}", (series_id,)
    )
    items = await cursor.fetchall()

    progress_map = {}
    if items:
        item_ids = [i["id"] for i in items]
        placeholders = ",".join("?" for _ in item_ids)
        cursor = await db.execute(
            f"SELECT * FROM reading_progress WHERE user_id = ? AND item_id IN ({placeholders})",
            (user_id, *item_ids),
        )
        for row in await cursor.fetchall():
            progress_map[row["item_id"]] = row

    # Estimated time left per item, for the card's second line. Computed
    # here rather than in each of the four callers below, so every render
    # of _items_grid.html (the page, the SSE refresh, and both mark-all
    # responses) shows the same thing without having to remember to ask.
    estimator = await build_estimator(db, user_id, cached_only=True)
    await estimator.prepare_items(items)
    items = [dict(item, estimate_state=estimator.item_states.get(item["id"], "complete")) for item in items]
    eta_map = {}
    for item in items:
        estimate = estimator.for_item(item, progress_map.get(item["id"]))
        if estimate is not None:
            eta_map[item["id"]] = estimate

    # Whether "Reading time" is an order this series can be put in at all
    # -- computed here because it is the ESTIMATOR that knows, and this is
    # where the estimator lives. Asked of what an item COULD carry rather
    # than of eta_map, which is empty for a book already finished, so keyed
    # off that the control would appear and disappear as the series is
    # read. See ReadingEstimator.can_estimate.
    can_sort_by_eta = any(estimator.can_estimate(i) for i in items)

    # Where each item sits in the AUTOMATIC order, regardless of which
    # order the rows above came back in. The sort control on the series
    # page reorders the cards client-side (see series-sort.js), and
    # "Original" has to stay reachable while a manual order is in force --
    # which it cannot be if the only ordering the page ever sees is the
    # one the DOM happens to be in. Asked of SQLite rather than re-derived
    # in Python so it is the same order the ORDER BY above produces when
    # manual_sort is off, by construction rather than by agreement.
    original_rank = await automatic_item_rank(db, series_id) if series["manual_sort"] else {
        item["id"]: rank for rank, item in enumerate(items)
    }

    return items, progress_map, eta_map, original_rank, can_sort_by_eta


async def _items_grid_response(request: Request, db, user, series):
    """_items_grid.html alone: the grid the page includes, re-rendered."""
    items, progress_map, eta_map, original_rank, _ = await _load_series_items(db, user["id"], series)
    return templates.TemplateResponse(
        request, "library/_items_grid.html",
        {"series": series, "items": items, "progress_map": progress_map, "eta_map": eta_map,
         "original_rank": original_rank},
    )


async def automatic_item_rank(db, series_id: int) -> dict[int, int]:
    """item id -> position in the automatic (number, then title) order."""
    cursor = await db.execute(
        f"SELECT id FROM items WHERE series_id = ? ORDER BY {AUTOMATIC_ORDER}",
        (series_id,),
    )
    return {row["id"]: rank for rank, row in enumerate(await cursor.fetchall())}


async def _get_hidden_statuses(db, user_id: int) -> set[str]:
    cursor = await db.execute("SELECT hidden_statuses FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    if row is None or not row["hidden_statuses"]:
        return set()
    try:
        return {s for s in json.loads(row["hidden_statuses"]) if s in SERIES_STATUS_LABELS}
    except (json.JSONDecodeError, TypeError):
        return set()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    hidden_statuses = await _get_hidden_statuses(db, user["id"])
    novels_ctx = await list_novels_for_settings(db)
    cursor = await db.execute("SELECT id, name FROM libraries ORDER BY name")
    libraries = await cursor.fetchall()
    problem_count = await _problem_item_count(db)
    # A real cover for the blur slider to act on. A number of pixels says
    # nothing about whether a cover is still recognisable, which is the
    # only question that setting answers -- so the slider is shown doing
    # it. A series that already asked to be blurred is the honest sample,
    # falling back to any cover at all, and to none on an empty library
    # (the preview is then simply absent, not a grey box pretending).
    preview_row = await (
        await db.execute(
            f"SELECT cover_path FROM series WHERE cover_path IS NOT NULL "
            f"AND {has_items()} ORDER BY blur_cover DESC, id LIMIT 1"
        )
    ).fetchone()
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "user": user, "status_labels": SERIES_STATUS_LABELS,
            "hidden_statuses": hidden_statuses, "libraries": libraries,
            "problem_count": problem_count,
            "blur_preview_cover": preview_row["cover_path"] if preview_row else None,
            "min_cover_blur": MIN_COVER_BLUR, "max_cover_blur": MAX_COVER_BLUR,
            "replacement_rules": await text_replacements.list_all(db, user["id"]),
            "replacement_targets": await _replacement_targets(db),
            # Set by the profile forms in auth/router.py when a save is
            # rejected; they are plain form posts, so the message has to
            # survive a redirect. An unrecognised code renders nothing,
            # which is what keeps a stale bookmark harmless.
            "profile_error": request.query_params.get("profile_error"),
            # The tracking section. Same shape as profile_error: the connect
            # form is a plain POST, so its refusal has to survive a redirect.
            "trackers": [
                {"name": name, "label": module.LABEL,
                 "account": await tracker_store.get_account(db, user["id"], name)}
                for name, module in sorted(
                    tracker_models.registry().items(), key=lambda kv: kv[1].LABEL)
            ],
            "tracker_error": request.query_params.get("tracker_error"),
            "tracker_detail": request.query_params.get("tracker_detail"),
            **novels_ctx,
        },
    )


async def _replacement_targets(db) -> list[dict]:
    """What a text replacement can be attached to: series holding at least
    one EPUB, plus every web novel.

    Comic-only series are left out rather than listed and ignored -- the
    substitution happens in the TEXT reader, and there is no text in a
    page of a comic to substitute. Listing them would offer a setting that
    silently does nothing.
    """
    cursor = await db.execute(
        "SELECT DISTINCT series.id, series.name FROM series "
        "JOIN items ON items.series_id = series.id "
        "WHERE items.file_format = 'epub' ORDER BY series.sort_name"
    )
    targets = [{"value": f"series:{row['id']}", "name": row["name"], "kind": "Series"}
               for row in await cursor.fetchall()]
    cursor = await db.execute("SELECT id, title FROM novels ORDER BY title")
    targets += [{"value": f"novel:{row['id']}", "name": row["title"], "kind": "Web novel"}
                for row in await cursor.fetchall()]
    return targets


async def _replacement_rules_response(request: Request, db, user_id: int):
    """The rules list, for htmx, or a redirect back to /settings without it.

    Every one of the three endpoints below answers this way, so the page
    never reloads when htmx is there -- an avatar just chosen or a
    page-time limit half-typed elsewhere on the page survives -- and still
    works as a plain form post when it is not.

    The whole list is re-rendered rather than one row: the order depends on
    the target's name, and an edit can move a rule within it.
    """
    if not is_htmx(request):
        return RedirectResponse("/settings#text-replacements", status_code=302)
    return templates.TemplateResponse(
        request, "_replacement_rules.html",
        {"replacement_rules": await text_replacements.list_all(db, user_id)},
    )


@router.post("/settings/text-replacements")
async def add_text_replacement(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    form = await request.form()
    series_id, novel_id = text_replacements.parse_target(str(form.get("target") or ""))
    find_text = str(form.get("find_text") or "")
    if (series_id is None and novel_id is None) or not find_text:
        # A rule with nothing to find would match at every position; a
        # rule pointing at nothing has no reader to run in. Both are only
        # reachable by submitting a form that skipped the required
        # attributes, so the list comes back unchanged rather than
        # explaining itself.
        return await _replacement_rules_response(request, db, user["id"])
    async with write_transaction() as wdb:
        cursor = await wdb.execute(
            "SELECT COUNT(*) AS n FROM text_replacements WHERE user_id = ? "
            "AND series_id IS ? AND novel_id IS ?",
            (user["id"], series_id, novel_id),
        )
        # A cap rather than no cap: every rule is a pass over the text of
        # every chapter as it renders, and this is a personal setting, not
        # a rules engine.
        if (await cursor.fetchone())["n"] < text_replacements.MAX_RULES_PER_TARGET:
            await wdb.execute(
                "INSERT INTO text_replacements (user_id, series_id, novel_id, find_text, replace_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (user["id"], series_id, novel_id, find_text, str(form.get("replace_text") or "")),
            )
        await wdb.commit()
    return await _replacement_rules_response(request, db, user["id"])


@router.post("/settings/text-replacements/{rule_id}")
async def update_text_replacement(
    rule_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db),
):
    """Edits the two texts of one rule. NOT what it applies to: a rule for
    another series is a different rule, and changing the target in place
    would move a substitution onto a book the reader never asked about."""
    form = await request.form()
    find_text = str(form.get("find_text") or "")
    if find_text:
        async with write_transaction() as wdb:
            # user_id in the WHERE, not just the id: the id comes from a form.
            await wdb.execute(
                "UPDATE text_replacements SET find_text = ?, replace_text = ? "
                "WHERE id = ? AND user_id = ?",
                (find_text, str(form.get("replace_text") or ""), rule_id, user["id"]),
            )
            await wdb.commit()
    return await _replacement_rules_response(request, db, user["id"])


@router.post("/settings/text-replacements/{rule_id}/delete")
async def delete_text_replacement(
    rule_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db),
):
    async with write_transaction() as wdb:
        await wdb.execute(
            "DELETE FROM text_replacements WHERE id = ? AND user_id = ?", (rule_id, user["id"])
        )
        await wdb.commit()
    return await _replacement_rules_response(request, db, user["id"])


@router.post("/settings/hidden-statuses")
async def save_hidden_statuses(request: Request, user=Depends(get_current_user)):
    form = await request.form()
    # Only real status values are kept -- an unrecognized value showing up
    # here would mean a form tampered with client-side, not a real choice.
    selected = [v for v in form.getlist("hidden_statuses") if v in SERIES_STATUS_LABELS]
    async with write_transaction() as db:
        await db.execute(
            "UPDATE users SET hidden_statuses = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(selected), user["id"]),
        )
        await db.commit()
    # 204 rather than a reload: this changes what the HOME page shows, not
    # this one, so re-rendering the settings page teaches the reader
    # nothing and costs them their place on it. Same rule as the two
    # sliders (see auth/router.py's _settings_saved), and the plain form
    # post still gets its redirect.
    if is_htmx(request):
        return Response(status_code=204)
    return RedirectResponse("/settings", status_code=302)


@router.get("/library/job-status")
async def library_job_status(user=Depends(get_current_user)):
    """Background jobs currently known to this process (library scans and
    the whole-collection renumber), running or just finished.

    The SSE stream only ever delivers what happens NEXT, so a page opened
    while a job is already under way would sit blank until the next unit
    was processed. This is what it asks on load; from then on the same
    payload arrives over SSE as job_progress events.
    """
    return {"jobs": job_status.snapshot()}


@router.get("/library/events")
async def library_events_stream(request: Request, since: str = "",
                                user=Depends(get_streaming_user)):
    """SSE: notifies open screens (home, series page) whenever the watcher
    or a scan changes the library -- items/series removed or added -- so
    they update themselves without needing an F5. See
    app/library/events.py for the pub/sub side and library-events.js for
    the consumer side.

    No external queue: one open HTTP connection per tab, with a periodic
    heartbeat (an SSE comment, ignored by the client) just to keep it alive
    through proxies that close idle connections.

    Every connection opens by saying whether the page on the other end is
    behind. Where the page stands is the `Last-Event-ID` the browser sends
    on a reconnect, or, on a document's first connection, the `since` the
    page was rendered with (base.html) -- which is what catches a page the
    browser served from its own cache, of any age. Behind, and it is told
    `events_gap` and re-renders itself; nothing it missed is replayed.
    """
    seen = request.headers.get("last-event-id") or since

    def frame(name: str, payload: str, number: int = 0) -> str:
        """One SSE frame.

        The `event:` line is not optional and not decoration. The client
        reads this stream through htmx's hx-sse extension, which DISPATCHES
        a named message as a DOM event and SWAPS an unnamed one into the
        page as HTML -- so a frame that lost its name would paint its own
        JSON onto the screen. Every frame written here goes through this
        function for that reason (see events.Event).

        The `id:` line is written only for a numbered event. A transient one
        (job progress) and the gap notice below carry number 0, which leaves
        the browser's Last-Event-ID on the last event that actually changed
        something.
        """
        head = f"id: {events.event_id(number)}\n" if number else ""
        return f"{head}event: {name}\ndata: {payload}\n\n"

    def position(number: int) -> str:
        """An `id:` and nothing else: where this page stands from now on.

        Written on every connection, so the extension always has an id to
        send back on the next one -- without it, a page that never received
        an event would reconnect with none, and fall back on a `since` from
        however long ago it was rendered.

        A frame with no `data:` is NOT the unnamed frame the docstring
        above forbids. That rule is about a frame the extension would swap
        into the page as HTML, and it only swaps one that HAS data: its
        parser yields `hasId` for this and the consumer sets the last id
        and then skips it for having neither data nor a name. So this
        reaches `lastEventId` and nothing else -- no DOM event, no handler,
        nothing for the two lists in test_events.py to drift over.
        """
        return f"id: {events.event_id(number)}\n\n"

    def gap() -> str:
        return frame(events.GAP_EVENT,
                     json.dumps({"type": events.GAP_EVENT}))

    async def event_generator():
        # Subscribed BEFORE the position is read, so nothing published in
        # between is lost. What the position already covers is dropped
        # from the queue below: a page that was current has it, and one
        # that was behind is about to re-render past it.
        queue = events.subscribe()
        try:
            delivered = events.current_id()
            if seen and not events.is_current(seen):
                # Before the position, so the gap is read against the page
                # it was about; see library-events.js.
                yield gap()
            yield position(delivered)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    if event == events.GAP_MARKER:
                        # This stream fell too far behind to be kept (see
                        # events.publish). The same answer as a reconnect
                        # from a page that is behind, and the same new
                        # position after it, since what was dropped is
                        # what the re-render covers.
                        delivered = events.current_id()
                        yield gap()
                        yield position(delivered)
                        continue
                    if event.number and event.number <= delivered:
                        continue        # covered by the position above
                    if event.number:
                        delivered = event.number
                    yield frame(event.name, event.payload, event.number)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            events.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Exactly what _series_card and _novel_card read, and nothing else.
#
# `SELECT *` here also carried `summary` and `source_summary` -- the
# description a tracker fills in, which is a paragraph or several -- for
# every series on the grid, to be dropped on the floor. The novel side was
# already narrowed to skip the cover BLOB, but kept the synopsis for the
# same nothing. Measured on 400 series carrying 1.4 KB descriptions:
# 5.7 ms and ~1.1 MB read, against 3.5 ms. It is the same reasoning that
# keeps content_html off the novel detail page's chapter query, one screen
# over.
#
# Written out rather than "everything but the big ones" so adding a column
# to the table cannot silently put it on this query.
SERIES_CARD_COLUMNS = (
    "id, library_id, name, cover_path, publication_status, blur_cover, created_at, updated_at"
)
NOVEL_CARD_COLUMNS = (
    "id, title, status, publication_status, cover_image_content_type, cover_image_hash, created_at"
)


def _series_card(row, *, status, last_read_at, reading, tracked_count, tracker_total) -> dict:
    """One home-grid card for a scanned series.

    THE definition of that shape: the grid renders it, `/library/series/
    {id}/card` answers it as JSON, and library-events.js builds a live
    card from that JSON. A card that arrives live must not look different
    from one the server rendered, and three hand-written copies of these
    fields is how that stops being true -- the cover URL was already
    "the third place the same URL is built" before this.
    """
    return {
        "kind": "series", "id": row["id"], "name": row["name"],
        "detail_url": f"/series/{row['id']}",
        "cover_url": f"/covers/{row['cover_path']}?private=1" if row["cover_path"] else None,
        "status": status,
        "status_label": SERIES_STATUS_LABELS.get(status) if status else None,
        "publication_status": row["publication_status"],
        "publication_label": PUBLICATION_STATUS_LABELS.get(row["publication_status"]),
        # Set from the series page; only this grid honours it.
        "blur_cover": bool(row["blur_cover"]),
        "added_at": row["created_at"],
        "modified_at": row["updated_at"],
        "last_read_at": last_read_at,
        # The badge over the cover counts what is still UNREAD rather than
        # what the shelf holds, and the line under the name says how long
        # that will take -- one question asked twice (see ShelfReading).
        # `None` seconds is "there is no basis for an estimate", which the
        # template renders as nothing at all; see total_remaining.
        "unread_count": reading.unread,
        "remaining_seconds": reading.remaining,
        "text_eta": reading.text_eta,
        "eta_missing": reading.missing, "eta_state": reading.state,
        # The tracking dot, and what "linked to all of them" means for it.
        "tracked_count": tracked_count,
        "tracker_total": tracker_total,
        # Matched against the library tabs' data-library-filter (see
        # home.js) -- a real library id for a series, or the fixed "novels"
        # marker for a novel, so "All" shows both while a specific tab
        # shows only its own kind.
        "library_id": row["library_id"],
    }


def _novel_card(row, *, status, last_read_at, reading,
                tracked_count, tracker_total) -> dict:
    """The same shape for a scraped web novel. Same reasons, same keys --
    the grid and the live builder cannot tell the two apart beyond `kind`
    and which id attribute the card carries."""
    publication = novel_publication_status(row["publication_status"], row["status"])
    return {
        "kind": "novel", "id": row["id"], "name": row["title"],
        "detail_url": f"/novels/{row['id']}",
        # ?v= is what lets the cover be cached immutably (see
        # novels/router.py's novel_cover).
        "cover_url": (
            f"/novels/{row['id']}/cover?v={row['cover_image_hash']}"
            if row["cover_image_hash"] else f"/novels/{row['id']}/cover"
        ) if row["cover_image_content_type"] else None,
        "status": status,
        "status_label": SERIES_STATUS_LABELS.get(status) if status else None,
        # The reader's own answer where they gave one, the source's
        # wording otherwise -- see status_labels.novel_publication_status.
        # Wording the site uses that this app has no word for gets no
        # badge rather than a guessed one.
        "publication_status": publication,
        "publication_label": PUBLICATION_STATUS_LABELS.get(publication),
        # Series-only for now: the checkbox lives on a series page.
        "blur_cover": False,
        "added_at": row["created_at"],
        # A novel row itself never changes after creation, so this used to
        # be created_at -- which made "Recently modified" say the same
        # thing as "Recently added" for every novel. What actually changes
        # about a novel is its CHAPTERS.
        "modified_at": reading.newest or row["created_at"],
        "last_read_at": last_read_at,
        # A novel's items are its chapters, so this counts the ones not
        # marked read -- including the one the reader is part-way through,
        # which is not finished. See _novel_reading.
        "unread_count": reading.unread,
        "remaining_seconds": reading.remaining,
        "text_eta": reading.text_eta,
        "eta_missing": reading.missing, "eta_state": reading.state,
        "tracked_count": tracked_count,
        "tracker_total": tracker_total,
        "library_id": "novels",
    }


@router.get("/library/series/{series_id}/card")
async def series_card(series_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    """Same card fields home() renders for a single series, as JSON --
    lets library-events.js insert a newly-added series into the grid
    without a reload instead of just showing the "new content" banner
    (which is all it can do for item_added, since there's no single grid
    row to append an item to on the home page)."""
    cursor = await db.execute(
        f"SELECT {SERIES_CARD_COLUMNS} FROM series WHERE id = ?", (series_id,))
    s = await cursor.fetchone()
    if s is None:
        raise HTTPException(404, "Series not found")

    cursor = await db.execute(
        "SELECT status FROM series_status WHERE user_id = ? AND series_id = ?", (user["id"], series_id)
    )
    status_row = await cursor.fetchone()

    cursor = await db.execute(
        """
        SELECT MAX(reading_progress.last_read_at) AS last_read_at
        FROM reading_progress
        JOIN items ON items.id = reading_progress.item_id
        WHERE reading_progress.user_id = ? AND items.series_id = ?
        """,
        (user["id"], series_id),
    )
    last_read_row = await cursor.fetchone()
    status = status_row["status"] if status_row else None

    # Through the same helper the grid uses, narrowed to this series: a
    # card that arrives live must not show a number the page would not have
    # rendered, and a second COUNT(*) written out here is exactly how that
    # stops being true.
    reading = await _series_reading(
        db, user["id"], await build_estimator(db, user["id"], cached_only=True), [series_id])

    return _series_card(
        s, status=status,
        last_read_at=last_read_row["last_read_at"] if last_read_row else None,
        reading=reading.get(series_id, NOTHING_LEFT),
        tracked_count=(await tracker_store.link_counts(db, user["id"]))[0].get(series_id, 0),
        tracker_total=len(tracker_models.registry()),
    )


@router.get("/library/novel/{novel_id}/card")
async def novel_card(novel_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    """The novel twin of the route above, and the only way a novel card on
    the home grid can be brought up to date without a reload.

    It exists because the tracking dot can change from somewhere else: you
    link a novel on its own page, and a home grid open in another tab is
    then wrong until it is reloaded. Series had this path already (it was
    built for a scan adding files); novels had nothing, because a novel
    changes on a schedule rather than under a file watcher.
    """
    row = await (
        await db.execute(
            f"SELECT {NOVEL_CARD_COLUMNS} FROM novels WHERE id = ?", (novel_id,))
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Novel not found")

    status_row = await (
        await db.execute(
            "SELECT status FROM novel_status WHERE user_id = ? AND novel_id = ?",
            (user["id"], novel_id))
    ).fetchone()
    progress_row = await (
        await db.execute(
            "SELECT updated_at FROM novel_reading_progress WHERE user_id = ? AND novel_id = ?",
            (user["id"], novel_id))
    ).fetchone()
    reading = await _novel_reading(
        db, user["id"], await build_estimator(db, user["id"], cached_only=True), novel_id)

    return _novel_card(
        row, status=status_row["status"] if status_row else None,
        last_read_at=progress_row["updated_at"] if progress_row else None,
        reading=reading.get(novel_id, NO_CHAPTERS),
        tracked_count=(await tracker_store.link_counts(db, user["id"]))[1].get(novel_id, 0),
        tracker_total=len(tracker_models.registry()),
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    # MyAnimeList redirects to a URI registered on ITS side, and the one
    # registered for this install is the app's ROOT -- so an OAuth callback
    # arrives here rather than at a path of the tracking module's choosing.
    # This returns None for every request that carries no `code`, which is
    # all of them but the one; the alternative was teaching this page about
    # OAuth, or asking for a redirect URI to be re-registered before the
    # feature could work at all. See tracking/router.py.
    redirect = await handle_oauth_callback(request, user)
    if redirect is not None:
        return redirect

    data_started = request_timing.start()
    phase_started = request_timing.start()
    cursor = await db.execute("SELECT * FROM libraries ORDER BY name")
    libraries = await cursor.fetchall()

    # The library tabs filter entirely client-side (see home.js), the same
    # as the status filter and the name search -- every series and novel
    # is always fetched and rendered here, and which ones show is a
    # show/hide driven by each card's data-library attribute. A
    # server-side ?library=<id> navigation per click was slower and, with
    # htmx boost, kept losing keyboard focus.
    # Only series that still hold something. An emptied one is kept when
    # the reader invested something in it (see series_scope), and a card
    # for it here would open onto nothing.
    cursor = await db.execute(
        f"SELECT {SERIES_CARD_COLUMNS} FROM series WHERE {has_items()} ORDER BY sort_name")
    all_series = await cursor.fetchall()

    all_novels = await (
        await db.execute(f"SELECT {NOVEL_CARD_COLUMNS} FROM novels ORDER BY title")
    ).fetchall()

    cursor = await db.execute(
        "SELECT series_id, status FROM series_status WHERE user_id = ?", (user["id"],)
    )
    status_map = {row["series_id"]: row["status"] for row in await cursor.fetchall()}

    novel_status_map = {
        row["novel_id"]: row["status"]
        for row in await (
            await db.execute("SELECT novel_id, status FROM novel_status WHERE user_id = ?", (user["id"],))
        ).fetchall()
    }

    # Series whose status the user chose to hide (see /settings) are hidden
    # from browsing by default -- same treatment as the status-filter
    # dropdown, and for the same reason: search deliberately ignores both,
    # so a hidden series is still findable by typing its name. That means
    # this can't be filtered out server-side (the card has to reach the
    # DOM for search to find it) -- it's applied client-side in home.js,
    # which reads it off the grid's data attribute below. Applies to
    # novels too, since they share the same status vocabulary and the same
    # grid/filter now.
    hidden_statuses = await _get_hidden_statuses(db, user["id"])

    # Last reading activity per series/novel, for the "reading" sort option
    # -- same recency signal the "Reading" shortcut section above already
    # uses, just keyed by series/novel instead of by item/chapter so a card
    # with several items picks up its most recently touched one.
    cursor = await db.execute(
        """
        SELECT items.series_id, MAX(reading_progress.last_read_at) AS last_read_at
        FROM reading_progress
        JOIN items ON items.id = reading_progress.item_id
        WHERE reading_progress.user_id = ? AND items.series_id IS NOT NULL
        GROUP BY items.series_id
        """,
        (user["id"],),
    )
    series_last_read = {row["series_id"]: row["last_read_at"] for row in await cursor.fetchall()}

    cursor = await db.execute(
        "SELECT novel_id, updated_at FROM novel_reading_progress WHERE user_id = ?", (user["id"],)
    )
    novel_last_read = {row["novel_id"]: row["updated_at"] for row in await cursor.fetchall()}

    # Unified card list -- series and novels now share the same grid,
    # search box, status filter, and hidden-statuses setting. `kind`
    # decides which id attribute a card gets (data-series-id vs
    # data-novel-id) -- only real series keep data-series-id, since that's
    # the attribute library-events.js's live-removal handler matches on;
    # novels have no equivalent live-removal event (they sync periodically,
    # not via a file watcher), so data-novel-id is just there for
    # consistency/debugging, nothing currently reads it back.
    # What each card still has waiting -- the unread count on its thumbnail
    # and the time under its name (see library/index.html) -- one grouped
    # pass per content type instead of a per-card query, the same batching
    # pattern as status_map/series_last_read above.
    #
    # ONE estimator for the whole page: the grid, and the "Reading" strip
    # below, are the same reader's numbers, and it is built from their whole
    # reading history (see build_estimator).
    #
    # What the pair costs, measured on 20,000 items and 27,000 chapters:
    # 110 ms for this handler before they existed, 182 ms after, of which
    # 46 ms is the series pass and 24 ms the novel one. The estimator they
    # share is 2 ms of that, and would have been 4 building it twice.
    request_timing.finish("home_queries", phase_started)
    phase_started = request_timing.start()
    estimator = await build_estimator(db, user["id"], cached_only=True)
    request_timing.finish("home_estimator", phase_started)
    phase_started = request_timing.start()
    series_reading = await _series_reading(db, user["id"], estimator)
    request_timing.finish("home_series", phase_started)
    phase_started = request_timing.start()
    novel_reading = await _novel_reading(db, user["id"], estimator)
    request_timing.finish("home_novels", phase_started)
    phase_started = request_timing.start()

    # How many tracking services each card is tied to, for the dot on its
    # cover. Per user, like the statuses above: a link belongs to a reader,
    # not to the shelf. One grouped query for the whole grid.
    series_links, novel_links = await tracker_store.link_counts(db, user["id"])
    request_timing.finish("home_tracking", phase_started)
    phase_started = request_timing.start()

    cards = []
    tracker_total = len(tracker_models.registry())
    for s in all_series:
        cards.append(_series_card(
            s, status=status_map.get(s["id"]),
            last_read_at=series_last_read.get(s["id"]),
            reading=series_reading.get(s["id"], NOTHING_LEFT),
            tracked_count=series_links.get(s["id"], 0),
            tracker_total=tracker_total,
        ))
    for n in all_novels:
        cards.append(_novel_card(
            n, status=novel_status_map.get(n["id"]),
            last_read_at=novel_last_read.get(n["id"]),
            reading=novel_reading.get(n["id"], NO_CHAPTERS),
            tracked_count=novel_links.get(n["id"], 0),
            tracker_total=tracker_total,
        ))

    # Base order (name, ascending) -- the actual sort shown to the user is
    # applied client-side in home.js (like the status filter, persisted in
    # localStorage, no reload/URL change), using the added_at/modified_at/
    # last_read_at data attributes rendered on each card below. Sorting here
    # too just gives JS a sane starting order before it runs on page load.
    cards.sort(key=lambda c: c["name"].lower())

    request_timing.finish("home_cards", phase_started)
    phase_started = request_timing.start()
    reading_items = await _reading_items(db, user["id"], estimator)
    request_timing.finish("home_reading", phase_started)

    context = {
        "user": user,
        "libraries": libraries,
        "cards": cards,
        "status_labels": SERIES_STATUS_LABELS,
        "publication_labels": PUBLICATION_STATUS_LABELS,
        # What "linked to everything" means, so the grid does not have
        # to hardcode two -- a third service would otherwise turn every
        # green dot into a lie.
        "tracker_total": tracker_total,
        "hidden_statuses": hidden_statuses,
        "reading_items": reading_items,
        "library_form": _library_form(
            LIBRARY_CREATE_ERRORS.get(request.query_params.get("library_error") or ""),
            request.query_params.get("library_name"),
            request.query_params.get("library_path"),
            request.query_params.get("library_type"),
        ),
    }
    request_timing.finish("home_data", data_started)
    render_started = request_timing.start()
    response = templates.TemplateResponse(request, "library/index.html", context)
    request_timing.finish("render", render_started)
    return response


# Why "New library" refused, keyed by what travels in the query string of
# the no-JS redirect. Before these were checked, the first two reached
# SQLite as a UNIQUE or CHECK failure and answered 500 -- which htmx does
# not swap, so the button did nothing at all.
LIBRARY_CREATE_ERRORS = {
    "empty": "A library needs a name and a path.",
    "taken": "Another library already reads that folder.",
    "overlaps": "That folder is inside another library's folder, or holds one -- "
                "a file there would belong to whichever library saw it first.",
    "not-a-folder": "There is no folder at that path inside the container.",
    "media-type": "Choose what the library holds.",
}
LIBRARY_MEDIA_TYPES = ("comic", "ebook", "mixed")


def _library_form(error=None, name=None, path=None, media_type=None) -> dict:
    return {"error": error, "name": name, "path": path, "media_type": media_type}


def _overlap(resolved_root: str, existing: list[str]) -> str | None:
    """Whether a new library at `resolved_root` would share files with one
    at any of `existing`: "taken" for the same folder, "overlaps" for one
    inside the other. The existing roots are resolved too (one stored
    before roots were), so `/lib/` is `/lib` and a symlink is the folder it
    names. Nested roots were accepted, and a file under both then belonged
    to whichever library's scan or watcher saw it first, the other showing
    that folder empty for good."""
    new = Path(resolved_root)
    for other in existing:
        old = Path(other).resolve()
        if new == old:
            return "taken"
        if new.is_relative_to(old) or old.is_relative_to(new):
            return "overlaps"
    return None


def _is_folder(path: str) -> bool | None:
    """None when the answer is unknown (a permission error on the way):
    that is not a no, and refusing on it would block a library the
    container can in fact read."""
    try:
        return Path(path).is_dir()
    except OSError:
        return None


@router.post("/libraries")
async def create_library(
    request: Request,
    name: str = Form(""),
    root_path: str = Form(""),
    media_type: str = Form("comic"),
    user=Depends(get_current_user),
):
    name, root_path = name.strip(), root_path.strip()
    error = None
    if not name or not root_path:
        error = "empty"
    elif media_type not in LIBRARY_MEDIA_TYPES:
        error = "media-type"
    elif await asyncio.to_thread(_is_folder, root_path) is False:
        error = "not-a-folder"
    library_id = None
    if error is None:
        # Stored resolved, so the comparison below and every path the
        # scanner derives from it name the folder one way.
        resolved = str(await asyncio.to_thread(lambda: Path(root_path).resolve()))
        async with write_transaction() as db:
            existing = [row["root_path"] for row in await (await db.execute(
                "SELECT root_path FROM libraries")).fetchall()]
            error = await asyncio.to_thread(_overlap, resolved, existing)
            if error is None:
                cursor = await db.execute(
                    "INSERT INTO libraries (name, root_path, media_type) VALUES (?, ?, ?)",
                    (name, resolved, media_type),
                )
                await db.commit()
                library_id = cursor.lastrowid

    if error is not None:
        if is_htmx(request):
            return templates.TemplateResponse(
                request, "library/_new_library_form.html",
                {"library_form": _library_form(
                    LIBRARY_CREATE_ERRORS[error], name, root_path, media_type)},
                status_code=422,
            )
        query = urllib.parse.urlencode({
            "library_error": error, "library_name": name,
            "library_path": root_path, "library_type": media_type,
        })
        return RedirectResponse(f"/?{query}", status_code=302)

    # kicks off an initial background scan and starts the watcher
    _run_background(scan_library(library_id, resolved))
    watcher.start(library_id, resolved, asyncio.get_running_loop())

    return RedirectResponse("/", status_code=302)


@router.post("/libraries/{library_id}/scan")
async def trigger_scan(library_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    cursor = await db.execute("SELECT root_path FROM libraries WHERE id = ?", (library_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, "Library not found")
    # runs in the background and redirects back to home immediately --
    # without this, the plain <form> (no HTMX) navigates to this URL and
    # shows the raw JSON response, on top of leaving the browser waiting
    # for the whole scan to finish (289 files can take a while) before
    # responding
    _run_background(scan_library(library_id, row["root_path"]))
    return RedirectResponse("/", status_code=302)


async def _problem_item_count(db) -> int:
    """How many things /library/issues would currently list -- its scan
    errors. Drives the marker next to the settings entry, so a broken
    import is noticeable without opening that page to check. Counted rather
    than fetched: the settings page only needs to know whether there is
    anything, and how much."""
    return (
        await (await db.execute("SELECT COUNT(*) AS n FROM scan_events WHERE event_type = 'error'")).fetchone()
    )["n"]


_estimate_slots = asyncio.Semaphore(1)


async def get_estimate_db():
    # Slow inventory work must never occupy the interactive request pool.
    async with _estimate_slots:
        db = await get_connection()
        try:
            await db.execute("PRAGMA query_only = ON")
            yield db
        finally:
            await db.close()


@router.post("/library/reading-estimates")
async def reading_estimates(request: Request, user=Depends(get_streaming_user), db=Depends(get_estimate_db)):
    """Authoritative word-based estimates for the text cards currently on screen.

    Only numbers are refreshed: covers, focus and client-side ordering survive.
    Each requested scope is bounded and resolved against this account's saved
    positions; callers never submit percentages or a pace to extrapolate.
    """
    try:
        body = json_object(await request.json())
    except ValueError:
        raise HTTPException(400, "Expected a JSON body")
    targets = body.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
        raise HTTPException(400, "Expected 1 to 100 reading estimate targets")
    parsed = {}
    for target in targets:
        if not isinstance(target, dict):
            raise HTTPException(400, "Invalid reading estimate target")
        kind, identity, volume = target.get("kind"), target.get("id"), target.get("volume_id")
        if (kind not in ("item", "novel", "series") or type(identity) is not int or identity < 1
                or (volume is not None and (kind != "novel" or type(volume) is not int or volume < 1))):
            raise HTTPException(400, "Invalid reading estimate target")
        key = f"{kind}:{identity}:{volume or ''}"
        parsed[key] = (kind, identity, volume)
    estimator = await build_estimator(db, user["id"])
    series_ids = {identity for kind, identity, _ in parsed.values() if kind == "series"}
    series_reading = await _series_reading(db, user["id"], estimator, series_ids) if series_ids else {}
    results = []
    for key, (kind, identity, volume) in parsed.items():
        estimate, state, missing = None, "complete", 0
        if kind == "item":
            item = await (await db.execute("SELECT * FROM items WHERE id=? AND file_format='epub'",
                                          (identity,))).fetchone()
            if item is not None:
                progress = await (await db.execute(
                    "SELECT * FROM reading_progress WHERE user_id=? AND item_id=?",
                    (user["id"], identity))).fetchone()
                await estimator.prepare_items([item])
                estimate = estimator.for_item(item, progress)
                state = estimator.item_states.get(identity, "content_unavailable")
        elif kind == "novel":
            scope = "AND chapters.volume_id=?" if volume is not None else ""
            parameters = (user["id"], identity, volume) if volume is not None else (user["id"], identity)
            unread = await (await db.execute(f"""SELECT COUNT(*) AS n FROM chapters
                LEFT JOIN novel_read_chapters AS marked
                  ON marked.chapter_id=chapters.id AND marked.user_id=?
                WHERE chapters.novel_id=? AND marked.chapter_id IS NULL {scope}""", parameters)).fetchone()
            if unread["n"]:
                await estimator.prepare_novel(identity, volume)
                estimate = estimator.for_novel(identity, volume)
                state = estimator.novel_states.get((identity, volume), "content_unavailable")
        else:
            reading = series_reading.get(identity, NOTHING_LEFT)
            state, missing = reading.state, reading.missing
            results.append({"key": key, "remaining": reading.remaining, "total": None,
                            "collecting": state == "collecting", "state": state, "missing": missing})
            continue
        results.append({"key": key, "remaining": estimate.remaining if estimate else None,
                        "total": estimate.total if estimate else None, "collecting": state == "collecting",
                        "state": state, "missing": missing})
    return {"estimates": results}


@router.get("/library/reading-grid", response_class=HTMLResponse)
async def reading_grid(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    """The home page's "Reading" section, re-rendered. Fetched by
    library-events.js when an item's reading STATUS changes -- a book
    that was just started belongs in this section and one that was just
    finished does not, and that is a question about the whole set, not
    about the one item whose progress arrived."""
    return templates.TemplateResponse(
        request, "library/_reading_grid.html",
        {"reading_items": await _reading_items(db, user["id"]), "user": user}
    )


@router.get("/library/search-items", response_class=HTMLResponse)
async def search_items_partial(request: Request, q: str = "", user=Depends(get_current_user),
                               db=Depends(get_db)):
    """Volumes/books whose own title matches, for the home page's search box.

    The series half of that box stays client-side: every series card is
    already in the DOM, so filtering them is instant and needs no round
    trip. Items are the half that could not work that way -- no page lists
    every item, so there was no way at all to find a volume without already
    knowing which series held it.

    Returns the partial rather than JSON because the caller is htmx, like
    every other live-updating piece of this app; and it ignores the status
    filter and the hidden-statuses setting for the same reason the series
    search does (see home()): a search is how you reach something you have
    deliberately hidden from browsing.
    """
    return templates.TemplateResponse(
        request, "library/_search_items.html",
        {"results": await search_items(db, q, limit=RESULT_LIMIT), "query": q.strip(),
         "limit": RESULT_LIMIT, "user": user},
    )


@router.get("/library/issues", response_class=HTMLResponse)
async def library_issues(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    cursor = await db.execute(
        "SELECT * FROM scan_events WHERE event_type = 'error' ORDER BY occurred_at DESC LIMIT 200"
    )
    errors = await cursor.fetchall()

    return templates.TemplateResponse(
        request, "library/issues.html", {"user": user, "errors": errors}
    )


@router.post("/library/issues/clear-errors")
async def clear_errors(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    async with write_transaction() as wdb:
        await wdb.execute("DELETE FROM scan_events WHERE event_type = 'error'")
        await wdb.commit()

    if is_htmx(request):
        return templates.TemplateResponse(request, "library/_processing_errors.html", {"errors": []})
    return RedirectResponse("/library/issues", status_code=302)


@router.post("/library/reindex-numbers")
async def reindex_numbers(request: Request, user=Depends(get_current_user)):
    """Recomputes the volume/chapter number for the whole library from the
    already-saved title and file path (without reopening any file) -- use
    this after an update that changes how that number is worked out, since
    "Rescan" alone doesn't touch items whose hash hasn't changed.

    Runs in the background and redirects immediately, the same pattern as
    "Rescan" (see trigger_scan): the caller is a plain <form>, so awaiting
    the whole job here would leave the browser on a blank request until it
    finished -- and there would be nothing to watch the progress bar for,
    since the page reporting it only exists after the response."""
    _run_background(backfill_number_in_series(report_progress=True))
    return redirect_back(request, "/library/issues")


@router.get("/series/{series_id}", response_class=HTMLResponse)
async def series_detail(
    request: Request, series_id: int, edit_error: str | None = None,
    user=Depends(get_current_user), db=Depends(get_db),
):
    series = await _get_series_or_404(db, series_id)

    items, progress_map, eta_map, original_rank, can_sort_by_eta = await _load_series_items(db, user["id"], series)

    cursor = await db.execute(
        "SELECT status FROM series_status WHERE user_id = ? AND series_id = ?", (user["id"], series_id)
    )
    status_row = await cursor.fetchone()
    current_status = status_row["status"] if status_row else None

    return templates.TemplateResponse(
        request, "library/series.html",
        {
            "user": user, "series": series, "items": items, "progress_map": progress_map,
            "eta_map": eta_map, "original_rank": original_rank,
            "can_sort_by_eta": can_sort_by_eta,
            "current_status": current_status, "status_labels": SERIES_STATUS_LABELS,
            "publication_labels": PUBLICATION_STATUS_LABELS,
            "automatic_name": _automatic_series_name(series),
            "edit_error": COVER_EDIT_ERRORS.get(edit_error or ""),
            # The tracking panel is a shared partial with its own endpoints;
            # its context comes from the one function they all use, so the
            # copy embedded here and the copy a search swaps in cannot
            # disagree about what is linked.
            **await tracker_panel_context(db, user["id"], series_id=series_id),
        },
    )


@router.get("/series/{series_id}/items-grid", response_class=HTMLResponse)
async def series_items_grid(
    request: Request, series_id: int,
    user=Depends(get_current_user), db=Depends(get_db),
):
    """A series' item grid, as the same partial series.html includes --
    what library-events.js swaps in when an item is added to the series
    currently on screen (see its handleItemAdded).

    Re-rendering the whole grid rather than appending one card is
    deliberate: where a new item belongs depends on natural and manual
    ordering, which _load_series_items already resolves. Inserting client-side would mean reimplementing that in JS
    and keeping the two in step; a personal library's page is small enough
    that re-rendering it is the cheaper and more reliable option.
    """
    series = await _get_series_or_404(db, series_id)

    return await _items_grid_response(request, db, user, series)


@router.post("/series/{series_id}/mark-all-read")
async def mark_series_read(
    series_id: int, request: Request,
    user=Depends(get_current_user), db=Depends(get_db),
):
    series = await _get_series_or_404(db, series_id)

    async with write_transaction() as wdb:
        await _mark_series_read(wdb, user["id"], series_id)
        await wdb.commit()

    # Queued AFTER the commit, never before: the push reads what is marked
    # read back out of the database. Fire-and-forget on purpose -- the
    # reader pressed a button about their own shelf, and making that wait
    # on MangaUpdates (or fail with it) would be paying for the tracker
    # with the feature they actually asked for. See app/tracking/service.py.
    # One push for the whole shelf, not one per item: the number sent is a
    # MAX over what is marked read, so the answer is the same either way
    # and this asks the service once.
    tracking.queue_push(user["id"], series_id=series_id)

    # One event for the whole series rather than one per item: the client
    # re-renders the grid and the "Reading" section either way, and a
    # series of hundreds of volumes would otherwise flood every connected
    # screen with hundreds of events saying the same thing.
    events.publish({"type": "series_progress_updated", "series_id": series_id})

    if is_htmx(request):
        # Re-render the grid in place instead of a full-page redirect --
        # manual_sort/manual_sort_editing aren't touched by this action, so
        # the `series` row fetched above is still accurate for that part of
        # _load_series_items.
        return await _items_grid_response(request, db, user, series)
    return redirect_back(request, f"/series/{series_id}")


@router.post("/series/{series_id}/mark-all-unread")
async def mark_series_unread(
    series_id: int, request: Request,
    user=Depends(get_current_user), db=Depends(get_db),
):
    series = await _get_series_or_404(db, series_id)

    async with write_transaction() as wdb:
        await _mark_series_unread(wdb, user["id"], series_id)
        await wdb.commit()

    # One event for the whole series rather than one per item: the client
    # re-renders the grid and the "Reading" section either way, and a
    # series of hundreds of volumes would otherwise flood every connected
    # screen with hundreds of events saying the same thing.
    events.publish({"type": "series_progress_updated", "series_id": series_id})

    if is_htmx(request):
        return await _items_grid_response(request, db, user, series)
    return redirect_back(request, f"/series/{series_id}")


@router.post("/series/{series_id}/manual-sort/toggle")
async def toggle_series_manual_sort(
    series_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)
):
    cursor = await db.execute("SELECT manual_sort, manual_sort_editing FROM series WHERE id = ?", (series_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, "Series not found")
    turning_editing_on = not row["manual_sort_editing"]

    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE series SET manual_sort_editing = ? WHERE id = ?",
            (1 if turning_editing_on else 0, series_id),
        )
        if turning_editing_on and not row["manual_sort"]:
            # First time this series enters manual mode -- make it sticky
            # (see series_detail: manual_sort alone decides the ORDER BY,
            # regardless of manual_sort_editing, so turning the drag UI
            # back off later does NOT revert to the automatic order) and
            # seed manual_order from what's on screen right now (the
            # existing number/title order) so dragging starts from a
            # sensible baseline instead of every item being an unordered
            # NULL tie. Re-entering edit mode on a series that's already
            # sticky skips this -- its existing manual_order is preserved.
            await wdb.execute("UPDATE series SET manual_sort = 1 WHERE id = ?", (series_id,))
            items = await (
                await wdb.execute(
                    f"SELECT id FROM items WHERE series_id = ? ORDER BY {AUTOMATIC_ORDER}",
                    (series_id,),
                )
            ).fetchall()
            for idx, item in enumerate(items):
                await wdb.execute("UPDATE items SET manual_order = ? WHERE id = ?", (idx, item["id"]))
        await wdb.commit()
    return redirect_back(request, f"/series/{series_id}")


@router.post("/series/{series_id}/manual-sort/reset")
async def reset_series_manual_sort(
    series_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)
):
    """Discards the manual order entirely and goes back to automatic
    (number_in_series/sort_title) -- unlike the toggle button, which only
    turns dragging on/off without touching the order itself."""
    cursor = await db.execute("SELECT id FROM series WHERE id = ?", (series_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(404, "Series not found")

    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE series SET manual_sort = 0, manual_sort_editing = 0 WHERE id = ?", (series_id,)
        )
        await wdb.execute("UPDATE items SET manual_order = NULL WHERE series_id = ?", (series_id,))
        await _recompute_series_cover(wdb, series_id)
        await wdb.commit()
    return redirect_back(request, f"/series/{series_id}")


@router.post("/series/{series_id}/manual-sort/reorder")
async def reorder_series_items(
    series_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)
):
    cursor = await db.execute("SELECT manual_sort_editing FROM series WHERE id = ?", (series_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, "Series not found")
    if not row["manual_sort_editing"]:
        raise HTTPException(409, "Manual sort is not enabled for this series")

    body = await request.json()
    item_ids = body.get("item_ids")
    if not isinstance(item_ids, list) or not item_ids:
        raise HTTPException(400, "item_ids must be a non-empty list")

    valid_rows = await (
        await db.execute("SELECT id FROM items WHERE series_id = ?", (series_id,))
    ).fetchall()
    valid_ids = {r["id"] for r in valid_rows}
    if set(item_ids) != valid_ids:
        raise HTTPException(400, "item_ids must match exactly the series' current items")

    async with write_transaction() as wdb:
        for idx, item_id in enumerate(item_ids):
            await wdb.execute("UPDATE items SET manual_order = ? WHERE id = ?", (idx, item_id))
        # The card's thumbnail on the home page is the cover of whatever
        # item counts as "first" -- dragging a different item into that
        # spot needs to update it right away, not wait for the next scan.
        await _recompute_series_cover(wdb, series_id)
        await wdb.commit()
    return {"ok": True}


# Uploads are read fully into memory before Pillow sees them, so the limit is
# what keeps a hostile (or simply mistaken) upload from being one. A cover is
# downscaled to 400x600 either way, so anything past a few MB is detail that
# is about to be thrown out.
MAX_COVER_UPLOAD_BYTES = 10 * 1024 * 1024

# Messages for the rejection codes the four name/cover endpoints produce.
# Kept here rather than in the template so the codes and their wording live
# together, and an unrecognized one (a hand-edited URL, since the non-htmx
# fallback puts the code in the query string) simply shows nothing.
COVER_EDIT_ERRORS = {
    "empty-name": "The name cannot be empty.",
    "no-file": "No file was selected.",
    "too-large": "That image is too large (10 MB maximum).",
    "not-an-image": "That file could not be read as an image.",
}


def _automatic_series_name(series) -> str:
    return automatic_series_name(series["folder_path"]) or series["name"]


async def _get_series_or_404(db, series_id: int):
    cursor = await db.execute("SELECT * FROM series WHERE id = ?", (series_id,))
    series = await cursor.fetchone()
    if series is None:
        raise HTTPException(404, "Series not found")
    return series


async def _series_edit_response(request: Request, db, series_id: int, error: str | None = None):
    """What the four name/cover endpoints return.

    Over htmx (which is how the panel actually submits) it is the panel
    itself, re-rendered from the database. That is the whole point of the
    partial: the page does not reload and the dialog does not close, so
    the name and the cover can be edited one after the other instead of one
    per visit. Success is reported through an HX-Trigger event rather than
    inferred from the body, so an error -- the same panel, carrying its
    message -- simply does not fire it.

    Without htmx these are still plain form posts, so the fallback is the
    redirect they always were, with the error as a query parameter:
    re-rendering the whole page from the POST would leave a URL that
    re-submits on reload. A browser following a 302 has no use for a 422,
    and the redirect is not the panel, so the status stays with the
    htmx branch.

    A refusal is a **422**, not the 200 it used to be. That 200 was the
    shape htmx 2 forced -- it did not swap a 4xx at all, so an error body
    had to arrive with a success status and the success had to be signalled
    some other way. htmx 4 swaps whatever the element opts into, and the
    four forms in the panel carry `hx-status:422`; base.html's `noSwap`
    keeps every OTHER 4xx in the app from painting its JSON into a card.

    The HX-Trigger event stays, and is not a second way of saying the same
    thing: it is NARROWER than "the status was 200". The blur-cover
    checkbox posts from inside this same panel and answers 200 too, and it
    is not a save anyone should see the word "Saved" for.
    """
    if not is_htmx(request):
        target = back_target(request, f"/series/{series_id}")
        if error:
            target = f"{target.split('?')[0]}?edit_error={error}"
        return RedirectResponse(target, status_code=302)

    series = await _get_series_or_404(db, series_id)
    response = templates.TemplateResponse(
        request, "library/_series_edit.html",
        {
            "series": series, "oob": True,
            "automatic_name": _automatic_series_name(series),
            "edit_error": COVER_EDIT_ERRORS.get(error or ""),
        },
        status_code=200 if error is None else 422,
    )
    if error is None:
        response.headers["HX-Trigger"] = "series-edit-saved"
    return response


@router.post("/series/{series_id}/name")
async def rename_series(
    series_id: int, request: Request, name: str = Form(...),
    user=Depends(get_current_user), db=Depends(get_db),
):
    """Renames a series for display, leaving the folder on disk untouched.

    sort_name goes along with it: it is what the home page orders by, and
    leaving it on the old name would sort the series under a title that is
    no longer shown anywhere.
    """
    await _get_series_or_404(db, series_id)
    new_name = name.strip()
    if not new_name:
        return await _series_edit_response(request, db, series_id, "empty-name")

    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE series SET name = ?, sort_name = ? WHERE id = ?",
            (new_name, natural_sort_key(new_name), series_id),
        )
        await wdb.commit()

    # updated_at is deliberately NOT touched: it means the series' CONTENTS
    # changed (see _touch_series), which is what the home page's "Date
    # modified" sort promises. Renaming changes neither.
    events.publish({"type": "series_updated", "series_id": series_id})
    return await _series_edit_response(request, db, series_id)


@router.post("/series/{series_id}/name/reset")
async def reset_series_name(
    series_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)
):
    series = await _get_series_or_404(db, series_id)
    automatic = _automatic_series_name(series)

    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE series SET name = ?, sort_name = ? WHERE id = ?",
            (automatic, natural_sort_key(automatic), series_id),
        )
        await wdb.commit()

    events.publish({"type": "series_updated", "series_id": series_id})
    return await _series_edit_response(request, db, series_id)


@router.post("/series/{series_id}/cover")
async def upload_series_cover(
    series_id: int, request: Request, cover: UploadFile = File(...),
    user=Depends(get_current_user), db=Depends(get_db),
):
    """Replaces the series' cover with an uploaded image.

    It goes through the same thumbnail pipeline as every other cover
    (400x600 WebP) rather than being stored as sent -- the card it ends up
    in is a couple of hundred pixels wide, and this is the one cover path
    where the source is whatever the user happened to have on hand.
    """
    series = await _get_series_or_404(db, series_id)

    data = await cover.read(MAX_COVER_UPLOAD_BYTES + 1)
    if not data:
        return await _series_edit_response(request, db, series_id, "no-file")
    if len(data) > MAX_COVER_UPLOAD_BYTES:
        return await _series_edit_response(request, db, series_id, "too-large")

    try:
        relative_path = await save_custom_series_cover(data, series_id)
    except Exception:
        # Not an image, or one Pillow cannot decode. The current cover is
        # untouched, so there is nothing to roll back.
        logger.exception("Could not store uploaded cover for series %s", series_id)
        return await _series_edit_response(request, db, series_id, "not-an-image")

    previous = series["cover_path"]
    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE series SET cover_path = ?, cover_overridden = 1 WHERE id = ?",
            (relative_path, series_id),
        )
        await wdb.commit()

    # Only if it was itself an upload, and only if it was actually replaced
    # -- re-uploading the same image resolves to the same content-addressed
    # name, which would otherwise delete the file just written.
    if previous != relative_path:
        delete_custom_series_cover(previous)

    events.publish({"type": "series_updated", "series_id": series_id})
    return await _series_edit_response(request, db, series_id)


@router.post("/series/{series_id}/cover/reset")
async def reset_series_cover(
    series_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)
):
    """Back to the automatic cover: the first item's own thumbnail."""
    series = await _get_series_or_404(db, series_id)
    previous = series["cover_path"]

    async with write_transaction() as wdb:
        await wdb.execute("UPDATE series SET cover_overridden = 0 WHERE id = ?", (series_id,))
        # Clearing the flag first is what lets this recompute at all -- it
        # returns early while the override is in place.
        await _recompute_series_cover(wdb, series_id)
        await wdb.commit()

    delete_custom_series_cover(previous)
    events.publish({"type": "series_updated", "series_id": series_id})
    return await _series_edit_response(request, db, series_id)


@router.post("/series/{series_id}/publication-status")
async def set_series_publication_status(series_id: int, request: Request,
                                        user=Depends(get_current_user), db=Depends(get_db)):
    """Unlike the tracking status next to it, this is a property of the
    SERIES and not of the reader, so it is a column rather than a row keyed
    by user -- and it has no empty value: every series is 'ongoing' until
    somebody says otherwise (see status_labels.py)."""
    form = await request.form()
    status = (form.get("publication_status") or "").strip()
    # The CHECK constraint only exists on a database created from
    # schema.sql -- SQLite cannot add one by ALTER TABLE -- so this is
    # what actually keeps the column to its four values.
    if status not in PUBLICATION_STATUS_LABELS:
        raise HTTPException(400, "Invalid publication status")

    cursor = await db.execute("SELECT id FROM series WHERE id = ?", (series_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(404, "Series not found")

    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE series SET publication_status = ? WHERE id = ?", (status, series_id)
        )
        await wdb.commit()

    # Same event a rename or a blurred cover publishes: this decision is
    # rendered as a badge on a card the home grid already drew, and the
    # page it was changed on is not the page that shows it.
    events.publish({"type": "series_updated", "series_id": series_id})

    # updated_at is deliberately not touched, for the same reason renaming
    # does not touch it: it means the series' CONTENTS changed, which is
    # what the home page's "Date modified" sort promises.
    if is_htmx(request):
        # The badge itself, not a 204: it wears the chosen value's COLOUR,
        # and markup keeps the class it was rendered with. Answering with
        # the re-rendered macro is what keeps that colour map in one
        # template instead of adding a copy in JS for this page.
        return publication_badge(
            f"/series/{series_id}/publication-status", status, PUBLICATION_STATUS_LABELS
        )
    return redirect_back(request, f"/series/{series_id}")


@router.post("/series/{series_id}/summary")
async def edit_series_summary(
    series_id: int, request: Request, summary: str = Form(default=""),
    user=Depends(get_current_user), db=Depends(get_db),
):
    """The description shown under the series title.

    Seeded from whichever tracker the series is linked to and edited here
    afterwards. `source_summary` is deliberately NOT touched: it is what
    the tracker last said, and keeping it is what lets a later fetch
    update the tracker's wording without undoing this edit -- and what
    makes "use the tracker's again" a button rather than another request.
    """
    await _get_series_or_404(db, series_id)
    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE series SET summary = ? WHERE id = ?", (summary.strip() or None, series_id))
        await wdb.commit()
    return await _series_edit_response(request, db, series_id)


@router.post("/series/{series_id}/summary/reset")
async def reset_series_summary(
    series_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db),
):
    """Back to what the tracker said, which is why its wording is kept."""
    series = await _get_series_or_404(db, series_id)
    if series["source_summary"]:
        async with write_transaction() as wdb:
            await wdb.execute(
                "UPDATE series SET summary = ? WHERE id = ?",
                (series["source_summary"], series_id))
            await wdb.commit()
    return await _series_edit_response(request, db, series_id)


@router.post("/series/{series_id}/blur-cover")
async def set_series_blur_cover(series_id: int, request: Request,
                                user=Depends(get_current_user), db=Depends(get_db)):
    """Whether the home grid renders this series' cover blurred.

    A property of the SERIES, like publication_status beside it: what is on
    a cover is a fact about the book, not about who is reading it. An
    unchecked checkbox posts NOTHING at all, which is why the absence of
    the field is read as 0 rather than left alone -- a form that could only
    ever turn the flag on would have no way to turn it off.
    """
    form = await request.form()
    blur = 1 if form.get("blur_cover") else 0

    cursor = await db.execute("SELECT id FROM series WHERE id = ?", (series_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(404, "Series not found")

    async with write_transaction() as wdb:
        await wdb.execute("UPDATE series SET blur_cover = ? WHERE id = ?", (blur, series_id))
        await wdb.commit()

    # Same event a rename or a cover upload publishes: the home page's only
    # copy of this decision is the class on an <img> it already rendered.
    events.publish({"type": "series_updated", "series_id": series_id})

    # updated_at is left alone for the same reason a rename leaves it
    # alone: it means the series' CONTENTS changed, which is what the home
    # page's "Date modified" sort promises.
    if is_htmx(request):
        return Response(status_code=204)
    return redirect_back(request, f"/series/{series_id}")


@router.post("/series/{series_id}/status")
async def set_series_status(series_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    form = await request.form()
    status = (form.get("status") or "").strip() or None
    if status is not None and status not in SERIES_STATUS_LABELS:
        raise HTTPException(400, "Invalid status")

    cursor = await db.execute("SELECT id FROM series WHERE id = ?", (series_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(404, "Series not found")

    async with write_transaction() as wdb:
        if status is None:
            await wdb.execute(
                "DELETE FROM series_status WHERE user_id = ? AND series_id = ?", (user["id"], series_id)
            )
        else:
            await wdb.execute(
                """
                INSERT INTO series_status (user_id, series_id, status, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(user_id, series_id) DO UPDATE SET status = excluded.status, updated_at = datetime('now')
                """,
                (user["id"], series_id, status),
            )
        await wdb.commit()

    # The home grid draws this as a badge and filters on it, so every
    # other open screen has to hear about it. The event carries no status:
    # this row belongs to a READER and the event reaches every connected
    # browser, so each one refetches the card and is answered with its own
    # (the same rule a tracker link follows).
    events.publish({"type": "series_updated", "series_id": series_id})

    # Every status the catalogues have a word for is reported to them
    # (each client's STATUS_FOR). Queued like every other push -- after the
    # commit, so it can read what was just written, and never able to fail
    # the button that caused it. The push itself says each change once:
    # `last_pushed_status` is what keeps a re-read from re-announcing it.
    # Clearing the status says nothing: no status is no opinion.
    if status is not None:
        tracking.queue_push(user["id"], series_id=series_id)

    if is_htmx(request):
        # The badge itself, for the same reason as the publication one
        # above: its colour is half of what it says.
        return tracking_badge(f"/series/{series_id}/status", status, SERIES_STATUS_LABELS)
    return redirect_back(request, f"/series/{series_id}")
