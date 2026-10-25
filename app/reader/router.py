import asyncio
import hashlib
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


from app.templating import templates
from app.auth.dependencies import get_current_user
from app.database import get_db, write_transaction
from app.http_utils import (
    back_target, is_htmx, json_object, one_of, optional_index, percent, safe_redirect_target, UNTRUSTED_CONTENT_HEADERS,
)
from app.library import events
from app.library.parsers.epub import list_spine_resources
from app.library.router import automatic_item_rank
from app.library.series_scope import items_order
from app.reader import streaming
from app.reading_progress import mark_item_read, mark_item_unread
from app.tracking import service as tracking
from app import text_replacements
from app.stats.estimates import build_estimator

router = APIRouter(prefix="/reader", tags=["reader"])

# What a reader may save (see schema.sql's CHECK). Written out here so a bad
# one is a 400 at the boundary instead of an IntegrityError five frames
# down; the CHECK stays as the backstop it always was. Not 'unread', which
# the CHECK still admits: unread is an item with NO row (see
# reading_progress.mark_item_unread), and a save is not how it gets there.
PROGRESS_STATUSES = ("reading", "completed")


def _content_cache_headers(asked_version: str | None, current_version: str) -> dict:
    """How long a browser may keep one page or one EPUB resource.

    Neither URL is content-addressed -- /reader/7/page/3 goes on naming
    page 3 of item 7 when the file behind it is replaced by a better scan
    -- so `immutable`, which tells a browser never to revalidate, is only
    safe on a URL that names WHICH version it wants. That is what `v` is,
    and a request carrying the current one gets the long answer.

    Anything else gets a short one and revalidates against the ETag: a URL
    with no version (an image inside an EPUB chapter, resolved against a
    `<base href>`), or one naming a version that is no longer current,
    which is exactly the case that must not be answered `immutable`.

    Same two answers, same reason, as a novel cover's `?v=` -- see
    novels/router.py.
    """
    if asked_version and asked_version == current_version:
        return {"Cache-Control": "private, max-age=31536000, immutable"}
    return {"Cache-Control": "private, max-age=60, must-revalidate"}


def _resource_etag(file_hash: str, resource_path: str) -> str:
    """The path HASHED into the tag, not written into it: response headers
    are Latin-1, and a chapter named Text/第一章.xhtml raised
    UnicodeEncodeError -- a 500, and "Couldn't load a chapter" for every
    CJK- or Cyrillic-named file in the book."""
    return f'"{file_hash}-{hashlib.sha1(resource_path.encode()).hexdigest()[:16]}"'


async def _get_item(db, item_id: int):
    cursor = await db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    item = await cursor.fetchone()
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


async def _next_item(db, series_id: int | None, item_id: int):
    """The next item in the same series, in the series page's own order
    (manual when the reader has arranged it) -- or None if there isn't one. Shared by open_reader
    (computed once up front) and chapter_info (called again for each
    chapter the comic reader appends while scrolling, so it always knows
    what comes after the one it just added).

    The title comes along because the text reader names what it is
    offering ("Next: Volume 3") rather than just pointing at an id.

    Seeks directly to "the row right after this one" via ROW_NUMBER()
    instead of fetching every item id in the series and scanning a Python
    list for a match -- same ordering, same result, without pulling the
    whole series into memory just to find one neighbor.
    """
    if series_id is None:
        return None
    series = await (await db.execute(
        "SELECT manual_sort FROM series WHERE id = ?", (series_id,))).fetchone()
    cursor = await db.execute(
        f"""
        WITH ordered AS (
            SELECT id, title,
                   ROW_NUMBER() OVER (ORDER BY {items_order(series and series["manual_sort"])}) AS rn
            FROM items
            WHERE series_id = ?
        )
        SELECT id, title FROM ordered WHERE rn = (SELECT rn FROM ordered WHERE id = ?) + 1
        """,
        (series_id, item_id),
    )
    return await cursor.fetchone()


# ---------- Open reader ----------

@router.get("/{item_id}", response_class=HTMLResponse)
async def open_reader(request: Request, item_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    item = await _get_item(db, item_id)
    cursor = await db.execute(
        "SELECT * FROM reading_progress WHERE user_id = ? AND item_id = ?", (user["id"], item_id)
    )
    progress = await cursor.fetchone()

    # next item in the same series -- lets the reader advance to the next
    # chapter on its own when it reaches the end of the current one.
    # Computed here (once, on open) instead of on demand at page-turn
    # time, so there's no extra network call right when the user is
    # trying to advance.
    next_item = await _next_item(db, item["series_id"], item_id)

    template = "reader/comic.html" if item["file_format"] == "cbz" else "reader/epub.html"
    return templates.TemplateResponse(
        request, template,
        {"user": user, "item": item, "progress": progress,
         "next_item_id": next_item["id"] if next_item else None,
         "next_item_title": next_item["title"] if next_item else None,
         # Display-only find-and-replace for this series, applied by the
         # reader as it renders (see app/text_replacements.py). Always a
         # JSON array, so the reader can parse it unconditionally.
         "replacements_json": await text_replacements.rules_json(
             db, user["id"], series_id=item["series_id"])},
    )


# ---------- Comic: chapter metadata (used by the scroll-mode infinite
# scroll to append the next chapter without a page reload -- see
# reader-comic.js's maybeLoadNextChapter) ----------

@router.get("/{item_id}/chapter-info")
async def comic_chapter_info(item_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    item = await _get_item(db, item_id)
    next_item = await _next_item(db, item["series_id"], item_id)
    return {
        "id": item["id"],
        "title": item["title"],
        "page_count": item["page_count"] or 0,
        "next_item_id": next_item["id"] if next_item else None,
        # What the reader puts on this chapter's page URLs as `v` -- see
        # _content_cache_headers. Carried per chapter because scroll mode
        # holds several at once, each with its own file behind it.
        "version": item["file_hash"],
    }


# ---------- Comic: pages ----------

@router.get("/{item_id}/page/{page_index}")
async def comic_page(item_id: int, page_index: int, request: Request, v: str | None = None,
                     user=Depends(get_current_user), db=Depends(get_db)):
    """One page's image.

    `v` is the item's own file hash, put there by the reader (see
    reader-comic.js). It is what makes `immutable` true rather than merely
    convenient: this URL is NOT content-addressed -- /reader/7/page/3 keeps
    naming page 3 of item 7 after the file behind it is replaced -- so
    answering `immutable` unconditionally told every browser never to ask
    again, for a year, about a page that can change. Same rule, and the
    same two answers, as novels/router.py's cover route.

    It no longer reports the page's pixel size: /dimensions already has
    every page's, computed once and cached in the database, and the reader
    reads the double-page layout out of that. Measuring it again here meant
    opening each page in Pillow on the way out, per page turn, for a number
    already known.
    """
    item = await _get_item(db, item_id)
    etag = f'"{item["file_hash"]}-{page_index}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    try:
        # In a thread: this reads the CBZ's central directory and
        # decompresses one page, and an `async def` handler runs ON the
        # event loop -- so every page turn froze the whole app for the
        # duration, SSE and progress saves included. Measured at 7.5 ms per
        # page for a 1.2 MB page here, and the reader asks for several at
        # once (it preloads around the current one). With four in flight, an
        # unrelated endpoint went from 3.9 ms to 37.9 ms median; in a thread
        # it stays at 16.3 ms. The scanner and the thumbnail pipeline
        # already do this -- the reader, the hottest path in the app, was
        # the one that did not.
        data, mime = await asyncio.to_thread(
            streaming.comic_page_bytes, item["file_path"], page_index, item["file_hash"])
    except IndexError:
        raise HTTPException(404, "Page out of range")

    return Response(content=data, media_type=mime,
                    headers={"ETag": etag, **_content_cache_headers(v, item["file_hash"])})


@router.get("/{item_id}/dimensions")
async def comic_page_dimensions(item_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    """
    Width/height of each page, in order -- lets the frontend size the
    scroll placeholders at their correct FINAL size before even downloading
    any image, eliminating the scroll readjustment that the previous
    approach (an estimated-height placeholder, corrected later) required.
    Computed once and cached in `page_dimensions`.
    """
    item = await _get_item(db, item_id)

    cursor = await db.execute(
        "SELECT page_index, width, height FROM page_dimensions WHERE item_id = ? ORDER BY page_index",
        (item_id,),
    )
    rows = await cursor.fetchall()
    if rows and len(rows) == (item["page_count"] or 0):
        return {"pages": [{"width": r["width"], "height": r["height"]} for r in rows]}

    # Same reasoning as comic_page: this opens every page in the archive,
    # so on the event loop it froze the app for the length of a volume.
    dims = await asyncio.to_thread(streaming.comic_all_page_dimensions, item["file_path"])
    async with write_transaction() as wdb:
        await wdb.execute("DELETE FROM page_dimensions WHERE item_id = ?", (item_id,))
        await wdb.executemany(
            "INSERT INTO page_dimensions (item_id, page_index, width, height) VALUES (?, ?, ?, ?)",
            [(item_id, i, w, h) for i, (w, h) in enumerate(dims)],
        )
        await wdb.commit()

    return {"pages": [{"width": w, "height": h} for w, h in dims]}


# ---------- EPUB: manifest and resources ----------
# These two ARE the EPUB reading path: reader-text.js walks the spine from
# /manifest and pulls each chapter (and its images/CSS) from /resource.
# There used to be a third route here, /file, serving the whole .epub for
# epub.js to download and unzip client-side; epub.js is long gone (see
# reader-text.js's header for why) and nothing has referenced /file since.

@router.get("/{item_id}/manifest")
async def epub_manifest(item_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    item = await _get_item(db, item_id)
    # Reads the EPUB's container, its OPF and its spine -- all zip work, so
    # off the event loop for the reason comic_page explains.
    spine = await asyncio.to_thread(list_spine_resources, item["file_path"])
    # The reader appends this to every resource URL it builds, which is what
    # lets those answer `immutable` honestly. See epub_resource.
    metadata = json.loads(item["metadata"] or "{}")
    return JSONResponse({"title": item["title"], "spine": spine, "version": item["file_hash"],
                         "language": metadata.get("language") or ""})


@router.get("/{item_id}/resource/{resource_path:path}")
async def epub_resource(item_id: int, resource_path: str, request: Request, v: str | None = None,
                        user=Depends(get_current_user), db=Depends(get_db)):
    """One file out of the EPUB.

    `v` carries the item's file hash, for the reason comic_page gives. The
    reader puts it on the chapter URLs it builds itself; an image or a
    stylesheet the chapter references arrives WITHOUT it, because those
    resolve against a `<base href>` and a relative URL drops the query.
    Those get the short-lived answer and revalidate against the ETag, which
    is the honest one for a URL whose content can change.
    """
    item = await _get_item(db, item_id)
    etag = _resource_etag(item["file_hash"], resource_path)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=UNTRUSTED_CONTENT_HEADERS)

    try:
        data, mime = await asyncio.to_thread(
            streaming.epub_resource_bytes, item["file_path"], resource_path)
    except KeyError:
        raise HTTPException(404, "Resource not found in the EPUB")

    return Response(
        content=data,
        media_type=mime,
        headers={"ETag": etag, **_content_cache_headers(v, item["file_hash"]),
                 **UNTRUSTED_CONTENT_HEADERS},
    )


# ---------- Progress ----------

def _publish_progress(item, status: str, percent: int, pages_remaining: int | None) -> None:
    """Announces a reading-status change to every open screen (see
    library-events.js). Marking read/unread has to do this as much as the
    reader itself does: the change is invisible to another device
    otherwise, which is how an item stayed in the home page's "Reading"
    section after being marked unread somewhere else."""
    events.publish({
        "type": "progress_updated",
        "item_id": item["id"],
        "series_id": item["series_id"],
        "status": status,
        "percent": percent,
        "pages_remaining": pages_remaining,
    })



def text_position(section, location):
    """Legacy block anchors and fragment anchors share a comparable position."""
    parts = (location or "").split(":")
    if len(parts) not in (2, 3) or parts[0] != "elidx" or not all(len(p) <= 16 and p.isascii() and p.isdigit() for p in parts[1:]):
        return None
    return [section or 0, int(parts[1]), int(parts[2]) if len(parts) == 3 else 0]


@router.get("/{item_id}/progress")
async def read_progress(item_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    """Where this item is saved as being, for a reader that has just come
    back out of the back/forward cache.

    A restored page has its own DOM back, position and all -- which is
    right for the tab that was frozen and wrong for the LIBRARY, if the
    same book was read on another device meanwhile. Nothing else can
    notice: the reader carries no live stream (see the empty
    `live_updates` block in its template), and the document the browser
    handed back never touched the network. So the page asks, once, on
    `pageshow`.

    `progress_percent` and a URL, and nothing else. Percent because it is
    the one number all three readers already compute and post -- a comic
    page index, an EPUB spine position and a novel's paragraph are not
    comparable, and a percentage of the same book is. The URL because the
    answer to "the server is ahead" is to go and be rendered from it,
    which is the same restore path a normal open uses; for an item that
    is this page again, and for a novel it is whichever chapter the saved
    position is in (see the novel router's own).
    """
    await _get_item(db, item_id)
    row = await (
        await db.execute(
            "SELECT progress_percent, current_page, current_location FROM reading_progress WHERE user_id = ? AND item_id = ?",
            (user["id"], item_id),
        )
    ).fetchone()
    return JSONResponse({
        "progress_percent": (row["progress_percent"] if row else 0) or 0,
        "resume_url": f"/reader/{item_id}",
        "position": text_position(row["current_page"], row["current_location"]) if row else None,
    })


@router.post("/{item_id}/progress")
async def save_progress(
    item_id: int,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    # Checked before anything is written, and answered as a 400 naming the
    # field. What this guards against is not a hostile client so much as a
    # stale or broken one: the values land in reading_progress, which every
    # card renders, and SQLite's dynamic typing means a REAL column accepts
    # a string quite happily. See app/http_utils.py for what that cost.
    try:
        body = json_object(await request.json())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Expected a JSON body")
    current_page = optional_index(body.get("current_page"), "current_page")
    current_location = body.get("current_location")
    if current_location is not None and not isinstance(current_location, str):
        raise HTTPException(400, "current_location must be a string")
    progress_percent = percent(body.get("progress_percent", 0), "progress_percent")
    status = one_of(body.get("status", "reading"), PROGRESS_STATUSES, "status")

    # The row's foreign key would refuse an unknown item anyway -- but as
    # an IntegrityError, which is a 500. The reader asking about something
    # that is not there is a 404, the same answer every other route here
    # gives for it.
    item = await _get_item(db, item_id)

    async with write_transaction() as wdb:
        # Whether this save is the one that FINISHES the book. The readers
        # post `completed` on every save from the last page on (the comic
        # reader on each turn there and on leaving, the text reader past
        # 98%), and each of those restamped completed_at and queued another
        # push to the trackers.
        before = await (await wdb.execute(
            "SELECT status FROM reading_progress WHERE user_id = ? AND item_id = ?",
            (user["id"], item_id),
        )).fetchone()
        # A beacon sent while leaving may arrive after "Mark all read".
        # It preserves a position, not an intention to start rereading.
        # Ordinary page-turn saves can still reopen a completed book.
        if (body.get("on_exit") is True and status == "reading"
                and before is not None and before["status"] == "completed"):
            return {"status": "ok"}
        finishing = status == "completed" and (before is None or before["status"] != "completed")
        await wdb.execute(
            """
            INSERT INTO reading_progress
                (user_id, item_id, status, current_page, current_location, progress_percent,
                 last_read_at, completed_at, completed_from_percent)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'),
                    -- A first save can already be the finish (a book opened
                    -- at its last page); it had no finish date at all.
                    CASE WHEN ?3 = 'completed' THEN datetime('now') END, ?)
            ON CONFLICT(user_id, item_id) DO UPDATE SET
                status = excluded.status,
                current_page = excluded.current_page,
                current_location = excluded.current_location,
                progress_percent = excluded.progress_percent,
                last_read_at = datetime('now'),
                completed_at = CASE
                    WHEN excluded.status = 'completed' AND reading_progress.status != 'completed'
                        THEN datetime('now')
                    ELSE reading_progress.completed_at END,
                -- The reader finishing a book IS a completion from where
                -- it had got to, which is the percentage it is saving --
                -- so the same column that exposes a declaration made at
                -- 12% records this one at ~100 and nothing has to treat
                -- the two paths differently. Only set on the transition:
                -- a row already completed keeps what it first recorded.
                completed_from_percent = CASE
                    WHEN excluded.status = 'completed' AND reading_progress.status != 'completed'
                        THEN excluded.progress_percent
                    ELSE reading_progress.completed_from_percent END
            """,
            (user["id"], item_id, status, current_page, current_location, progress_percent,
             # Only meaningful when this first save is already a
             # completion; every other row gets it on the transition in the
             # ON CONFLICT clause above.
             progress_percent if status == "completed" else None),
        )
        await wdb.commit()

    # Lets a home or series page open on another tab/device follow along
    # instead of showing what was true when it loaded. The percentage
    # travels in the event itself: it is one number, and sending it saves
    # every listening screen a round trip per page turn (progress is saved
    # on every one of them -- see reader-text.js). series_id and status are
    # what the client needs to decide whether the change is merely a wider
    # bar or a structural one (an item entering/leaving the "Reading"
    # section, a card gaining its Read badge), which does need the server.
    pages_remaining = None
    if item["page_count"] and current_page is not None:
        pages_remaining = max(0, item["page_count"] - current_page - 1)
    _publish_progress(item, status=status, percent=round(progress_percent or 0), pages_remaining=pages_remaining)
    if finishing and item["series_id"] is not None:
        # THE path a volume becomes read: somebody read it. The mark-read
        # button below is the exception, not the rule, and hooking only
        # that one left the tracker silent for every reader who simply
        # reached the last page -- which is the whole feature. Queued after
        # the commit, like every other push, and only on the save that
        # finished it: this endpoint is called on every page turn.
        tracking.queue_push(user["id"], series_id=item["series_id"])

    return {"status": "ok"}


# ---------- Manually mark as read/unread ----------
# Used by the buttons on the series page (not by the reader itself) -- for
# people who manage their library by deleting already-read chapters,
# manually marking what's left is faster than opening and scrolling to
# the end of each one.

async def _render_item_card(request: Request, db, user_id: int, item_id: int):
    """Re-renders one series item's card (library/_item_card.html) after a
    read/unread toggle -- the htmx response for that button, so the click
    updates just this card's badge instead of reloading the series page."""
    item = await _get_item(db, item_id)
    series = await (
        await db.execute("SELECT * FROM series WHERE id = ?", (item["series_id"],))
    ).fetchone()
    progress = await (
        await db.execute(
            "SELECT * FROM reading_progress WHERE user_id = ? AND item_id = ?", (user_id, item_id)
        )
    ).fetchone()
    # Same estimate the series page renders, so a card swapped by the
    # read/unread toggle comes back saying what its neighbours say. Marking
    # an item read makes the estimate go away, which is the point: there is
    # nothing left to estimate.
    estimator = await build_estimator(db, user_id, cached_only=True)
    await estimator.prepare_items([item])
    eta = estimator.for_item(item, progress)
    return templates.TemplateResponse(
        request, "library/_item_card.html",
        {
            "series": series, "item": dict(item, estimate_state=estimator.item_states.get(item_id, "complete")),
            "progress_map": {item_id: progress} if progress else {},
            "eta_map": {item_id: eta} if eta is not None else {},
            # The card carries its position in the automatic order (see
            # _item_card.html), and this response replaces one card in a
            # grid the sort control may have reordered -- without it the
            # swapped card would come back claiming position 0.
            "original_rank": await automatic_item_rank(db, item["series_id"]),
        },
    )


@router.post("/{item_id}/mark-read")
async def mark_read(
    item_id: int, request: Request, return_to: str | None = Form(default=None),
    user=Depends(get_current_user), db=Depends(get_db),
):
    item = await _get_item(db, item_id)
    last_page = (item["page_count"] - 1) if item["page_count"] else None
    async with write_transaction() as wdb:
        await mark_item_read(wdb, user["id"], item_id, last_page)
        await wdb.commit()

    # Queued AFTER the commit, never before: the push reads what is marked
    # read back out of the database. Fire-and-forget on purpose -- the
    # reader pressed a button about their own shelf, and making that wait
    # on MangaUpdates (or fail with it) would be paying for the tracker
    # with the feature they actually asked for. See app/tracking/service.py.
    if item["series_id"] is not None:
        tracking.queue_push(user["id"], series_id=item["series_id"])

    _publish_progress(item, status="completed", percent=100, pages_remaining=0)

    if is_htmx(request):
        return await _render_item_card(request, db, user["id"], item_id)

    # return_to (an explicit hidden field the form already knows, e.g. the
    # series page it lives on) is preferred over the Referer header, which
    # some privacy-focused browsers/extensions omit -- Referer stays as a
    # fallback for any caller that doesn't send return_to, then "/" if
    # even that's missing.
    target = safe_redirect_target(return_to) or back_target(request, "/")
    return RedirectResponse(target, status_code=302)


async def _mark_unread(request: Request, item_id: int, return_to: str | None, user, db):
    item = await _get_item(db, item_id)  # 404 if the item doesn't exist
    async with write_transaction() as wdb:
        await mark_item_unread(wdb, user["id"], item_id)
        await wdb.commit()

    # An empty status, because that is what the card now has: every screen
    # comparing against data-progress-status sees a change and re-renders,
    # which is what takes the card out of the home page's "Reading" strip.
    _publish_progress(item, status="", percent=0, pages_remaining=None)

    if is_htmx(request):
        return await _render_item_card(request, db, user["id"], item_id)
    target = safe_redirect_target(return_to) or back_target(request, "/")
    return RedirectResponse(target, status_code=302)


@router.post("/{item_id}/mark-unread")
async def mark_unread(
    item_id: int, request: Request, return_to: str | None = Form(default=None),
    user=Depends(get_current_user), db=Depends(get_db),
):
    """Puts an item back to Unread (see reading_progress.mark_item_unread)."""
    return await _mark_unread(request, item_id, return_to, user, db)


@router.post("/{item_id}/clear-status")
async def clear_status(
    item_id: int, request: Request, return_to: str | None = Form(default=None),
    user=Depends(get_current_user), db=Depends(get_db),
):
    """The same action as mark-unread, under the name the home page's
    "Reading" strip posts to: it builds one clear_url per card, beside the
    novel-side /novels/{id}/clear-status -- where clearing is NOT the same
    as unread, because a novel keeps the chapters marked read."""
    return await _mark_unread(request, item_id, return_to, user, db)
