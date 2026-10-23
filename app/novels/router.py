import logging
import hashlib

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.templating import templates
from app.auth.dependencies import get_current_user
from app.background import run_background as _run_background
from app.database import get_db, write_transaction
from app.http_utils import (
    is_htmx, json_object, redirect_back, optional_index, percent, safe_redirect_target, UNTRUSTED_CONTENT_HEADERS,
)
from app import text_replacements
from app.library import events
from app.novels.scraper import tag_paragraphs
from app.novels.sync import renumber_chapters_by_natural_order, sync_novel
from app.status_badges import publication_badge, tracking_badge
from app.status_labels import PUBLICATION_STATUS_LABELS, publication_status_from_source
from app.status_labels import STATUS_LABELS as NOVEL_STATUS_LABELS
from app.tracking import service as tracking
from app.tracking.router import panel_context as tracker_panel_context

router = APIRouter(tags=["novels"])
logger = logging.getLogger("novels.router")


def format_chapter_number(value) -> str:
    """45.0 -> "45", 45.3 -> "45.3". Used both in URLs and in templates so a
    whole-number chapter doesn't show/link as "45.0"."""
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else str(f)


templates.env.filters["chnum"] = format_chapter_number


# Every column of `novels` EXCEPT cover_image, which is the downloaded
# cover's raw bytes (see schema.sql). A plain `SELECT *` pulls that BLOB
# into memory on every listing and every detail page, and then throws it
# away: the only code that actually reads it is the /novels/{id}/cover
# route that serves it, plus one IS NULL check in sync.py. On the home
# page, which lists every novel at once, that measured 8x slower than
# selecting the columns actually used (several MB read and discarded per
# page load, depending on cover size).
#
# Spelled out as "everything but the blob" rather than a per-caller column
# list on purpose -- callers hand these rows straight to templates that
# reach for arbitrary fields, so a narrower list would be a standing
# invitation for a missing-column bug at render time.
NOVEL_COLUMNS = (
    "id, title, source_url, cover_url, cover_image_content_type, cover_image_hash, synopsis, "
    "source_title, source_synopsis, status, publication_status, "
    "active, sync_status, sync_error, last_checked_at, manual_sort, manual_sort_editing, created_at"
)


async def _get_novel(db, novel_id: int):
    row = await (
        await db.execute(f"SELECT {NOVEL_COLUMNS} FROM novels WHERE id = ?", (novel_id,))
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Novel not found")
    return row


# ---------- Detail ----------

@router.get("/novels/{novel_id}/cover")
async def novel_cover(novel_id: int, v: str | None = None, user=Depends(get_current_user), db=Depends(get_db)):
    """Serves the cover image downloaded and stored by the scraper -- the app
    never has the browser hotlink the source site directly, since that
    breaks for anyone blocking third-party domains client-side.

    Unlike an item's thumbnail, this one lives in the row rather than in a
    file, so its URL cannot carry the content's hash in a filename the way
    `/covers/{item_id}-{hash}.webp` does -- and without that, the URL means
    something different after a resync that changes the cover, which is why
    this could only ever be cached for a day at a time. `?v=` closes that:
    the templates render the stored hash, so the URL changes exactly when
    the bytes do, and a request that carries the CURRENT hash can be cached
    for good.

    A `v` that doesn't match (a page rendered before a resync) still gets
    the current cover, just with the short-lived caching -- serving the
    right image matters more than honouring a stale URL, and that URL is
    never rendered again anyway. `private` throughout: this is behind the
    session, so no shared cache should keep it.
    """
    novel = await (await db.execute(
        "SELECT cover_image, cover_image_content_type, cover_image_hash FROM novels WHERE id = ?", (novel_id,)
    )).fetchone()
    if novel is None or not novel["cover_image"]:
        return Response(status_code=404)
    is_current_version = bool(v) and v == novel["cover_image_hash"]
    cache_control = (
        "private, max-age=31536000, immutable" if is_current_version else "private, max-age=86400"
    )
    return Response(
        content=novel["cover_image"],
        media_type=novel["cover_image_content_type"] or "image/jpeg",
        # An image Pillow could not re-encode is stored as the source
        # sent it, which can be an SVG -- a document with scripts.
        headers={"Cache-Control": cache_control, **UNTRUSTED_CONTENT_HEADERS},
    )


@router.get("/novels/{novel_id}", response_class=HTMLResponse)
async def novel_detail(novel_id: int, request: Request, edit_error: str | None = None,
                       user=Depends(get_current_user), db=Depends(get_db)):
    novel = await _get_novel(db, novel_id)

    volumes = await (
        await db.execute("SELECT * FROM volumes WHERE novel_id = ? ORDER BY number", (novel_id,))
    ).fetchall()
    # Named columns, not SELECT *: `content_html` holds a whole chapter's
    # text, and this page renders only numbers, titles and dates. Measured
    # on a 900-chapter novel of ~6 KB chapters -- 16.3 ms and 6.5 MB per
    # request against 5.6 ms and 0.21 MB -- and both figures grow with the
    # novel, which is exactly the direction a long-running scrape goes.
    # Whether a chapter HAS text is still worth knowing (a sync that has
    # not fetched it yet is why a chapter refuses to open), so it comes
    # back as a flag rather than as the text.
    chapters = await (
        await db.execute(
            """
            SELECT id, volume_id, number, title, release_date,
                   content_html IS NOT NULL AS downloaded
              FROM chapters WHERE novel_id = ? ORDER BY order_index
            """,
            (novel_id,),
        )
    ).fetchall()
    chapters_by_volume: dict[int, list] = {}
    for c in chapters:
        chapters_by_volume.setdefault(c["volume_id"], []).append(c)

    progress = await (
        await db.execute(
            "SELECT * FROM novel_reading_progress WHERE user_id = ? AND novel_id = ?", (user["id"], novel_id)
        )
    ).fetchone()

    read_rows = await (
        await db.execute(
            """
            SELECT novel_read_chapters.chapter_id FROM novel_read_chapters
            JOIN chapters ON chapters.id = novel_read_chapters.chapter_id
            WHERE novel_read_chapters.user_id = ? AND chapters.novel_id = ?
            """,
            (user["id"], novel_id),
        )
    ).fetchall()
    read_ids = {r["chapter_id"] for r in read_rows}

    # Where the reader is, which the list marks (the ▶ on the row, the
    # "● Reading" on the volume, and which volume opens on arrival). The
    # page no longer offers to jump there: the chapter list IS the way in,
    # and a button above it that lands somewhere the list already points at
    # was a second answer to the same question.
    current_chapter_id = None
    current_volume_id = None
    if progress:
        current_chapter = next((c for c in chapters if c["id"] == progress["chapter_id"]), None)
        if current_chapter:
            current_chapter_id = current_chapter["id"]
            current_volume_id = current_chapter["volume_id"]

    # One walk, three answers. The per-volume count is what lets a
    # collapsed volume say how far in it you are without being opened --
    # the whole point of collapsing them on a novel with hundreds of
    # chapters -- and "all read" is that count against the volume's size
    # rather than a second pass asking the same question.
    volume_read_counts = {
        v["id"]: sum(1 for c in chapters_by_volume.get(v["id"], []) if c["id"] in read_ids)
        for v in volumes
    }
    volume_all_read = {
        v["id"]: bool(chapters_by_volume.get(v["id"]))
        and volume_read_counts[v["id"]] == len(chapters_by_volume[v["id"]])
        for v in volumes
    }
    # read_ids is already this novel's -- its query joins through
    # chapters and filters on novel_id -- so this is a count, not a
    # filter. An intersection here would only look like a safety net.
    chapters_read = len(read_ids)

    status_row = await (
        await db.execute(
            "SELECT status FROM novel_status WHERE user_id = ? AND novel_id = ?", (user["id"], novel_id)
        )
    ).fetchone()
    current_status = status_row["status"] if status_row else None

    return templates.TemplateResponse(
        request, "novels/detail.html",
        {
            "user": user, "novel": novel, "volumes": volumes, "chapters_by_volume": chapters_by_volume,
            "read_ids": read_ids, "volume_all_read": volume_all_read,
            "volume_read_counts": volume_read_counts,
            "chapters_read": chapters_read, "chapters_total": len(chapters),
            "current_chapter_id": current_chapter_id, "current_volume_id": current_volume_id,
            "current_status": current_status, "status_labels": NOVEL_STATUS_LABELS,
            # Two different questions, side by side: where the READER is
            # (above) and where the WORK is. The second is normally the
            # source page's answer, and this is the override for when that
            # page words it in a way nothing here recognises.
            "publication_labels": PUBLICATION_STATUS_LABELS,
            # What "From the source" currently resolves to, so the option
            # that means "don't override" says what it is deferring to
            # rather than making the reader work it out from the badge.
            # None when the site's wording is one this app cannot read --
            # which is the case the override exists for.
            "source_publication_label": PUBLICATION_STATUS_LABELS.get(
                publication_status_from_source(novel["status"])),
            # Only ever set by the no-htmx fallback below, and it is what
            # opens the edit panel on arrival so the message is not
            # reported to a closed drawer.
            "edit_error": NOVEL_EDIT_ERRORS.get(edit_error or ""),
            # Same shared panel as the series page; see the comment there.
            **await tracker_panel_context(db, user["id"], novel_id=novel_id),
        },
    )


# ---------- Reader ----------

@router.get("/novels/{novel_id}/manifest")
async def novel_manifest(novel_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    """Same shape/purpose as the EPUB reader's /reader/{id}/manifest --
    the shared reader engine (reader-text.js) uses this to drive both
    Paginated and Continuous scroll for novels exactly like it already
    does for EPUBs. The list is FLAT across every volume, in order_index
    order -- novels no longer get a special "confirm before continuing to
    the next volume" interruption; reaching the end of a volume just
    continues seamlessly into the next one's first chapter, same as
    crossing any other chapter boundary.

    `size` weights reading progress by how much text each chapter holds
    instead of counting every chapter the same (see reader-text.js). It is
    LENGTH() of the stored HTML, not a word count -- close enough for a
    progress bar. It is read from `chapters.content_size`, written beside
    the text, rather than measured here: LENGTH() on a TEXT column reads
    the whole value (SQLite can shortcut it only for a BLOB), so asking for
    it at query time pulled every chapter of the novel out of the database
    on every open of the reader -- measured at 4.8 ms and 5.4 MB for 900
    chapters of ~6 KB, against 1.8 ms for the column. A chapter whose text
    hasn't been fetched yet reports 0, and the reader gives those the
    average weight of the ones it does know.

    `source_url` doubles as the resource's base href (see
    novel_resource below) -- unlike EPUB chapters (served from files this
    app fully controls), a scraped chapter's relative image references (if
    any) only resolve correctly against the ORIGINAL site, since this app
    never mirrors that content itself.
    """
    novel = await _get_novel(db, novel_id)
    rows = await (
        await db.execute(
            """
            SELECT id, number, url, title, COALESCE(content_size, 0) AS size
            FROM chapters WHERE novel_id = ? ORDER BY order_index
            """,
            (novel_id,),
        )
    ).fetchall()
    return {
        "title": novel["title"],
        "spine": [
            {"chapter_id": r["id"], "source_url": r["url"], "title": r["title"], "size": r["size"]}
            for r in rows
        ],
    }


@router.get("/novels/{novel_id}/resource/{chapter_id}")
async def novel_resource(novel_id: int, chapter_id: int, request: Request,
                         db=Depends(get_db), user=Depends(get_current_user)):
    """Same purpose as the EPUB reader's /reader/{id}/resource/{path} --
    returns one chapter's raw content for the shared reader engine to
    paginate/scroll client-side. tag_paragraphs() is applied the same way
    as the server-rendered reader page used to (see _load_chapter_context)
    so the paragraph-index anchor scheme (elidx:N) works identically for
    both content types.
    """
    chapter = await (
        await db.execute(
            "SELECT content_html, content_size, content_fetched_at FROM chapters "
            "WHERE id = ? AND novel_id = ?",
            (chapter_id, novel_id),
        )
    ).fetchone()
    if chapter is None:
        raise HTTPException(404, "Chapter not found")

    # A chapter body is the largest thing this app serves per request, and
    # it changes only when a sync rewrites it -- but it carried no
    # validator at all, so every revisit downloaded the whole chapter
    # again, where the EPUB half of the same reader has answered 304 all
    # along. The two columns are what a rewrite moves: the timestamp the
    # sync stamps, and the length, which catches a rewrite landing inside
    # the same second.
    etag = f'W/"{chapter_id}-{chapter["content_fetched_at"] or ""}-{chapter["content_size"] or 0}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    content_html = chapter["content_html"] or ""
    if content_html:
        tagged = tag_paragraphs(content_html)
        if tagged != content_html:
            async with write_transaction() as wdb:
                await wdb.execute(
                    "UPDATE chapters SET content_html = ?, content_size = LENGTH(?) WHERE id = ?",
                    (tagged, tagged, chapter_id))
                await wdb.commit()
            content_html = tagged
            # That rewrite just changed what the validator describes, so
            # the response must not carry the one computed before it.
            etag = f'W/"{chapter_id}-{chapter["content_fetched_at"] or ""}-{len(tagged)}"'
    return Response(
        content=content_html,
        media_type="text/html",
        headers={"ETag": etag, "X-Reading-Source": hashlib.sha256(content_html.encode('utf-8')).hexdigest(),
                 "Cache-Control": "private, max-age=60, must-revalidate"},
    )


async def _load_chapter_context(novel_id: int, volume_number: int, chapter_number: float, user, db):
    """Everything novels/reader.html needs to bootstrap the reader: which
    novel, which chapter (title + its spine position), and the saved
    paragraph anchor to resume at.

    That is deliberately much less than this used to return. The page is
    now just a shell -- the shared reader engine (reader-text.js) fetches
    the chapter list from /novels/{id}/manifest and each chapter's HTML
    from /novels/{id}/resource/{chapter_id}, so the volume row, the
    prev/next/next-volume URLs, the saved scroll percentage, the read flag
    and the chapter BODY were all still being computed here and then
    dropped on the floor by the template. That cost four extra queries per
    chapter open, plus reading the whole content_html column out of SQLite
    (tens of KB) only to discard it, plus a possible BeautifulSoup parse
    and its own write transaction.

    tag_paragraphs() in particular belongs on the resource endpoint (which
    already applies it, and is where the HTML is actually served from),
    not on the page load that no longer renders any chapter text.
    """
    chapter = await (
        await db.execute(
            """
            SELECT chapters.id, chapters.title, chapters.order_index
            FROM chapters JOIN volumes ON volumes.id = chapters.volume_id
            WHERE chapters.novel_id = ? AND volumes.number = ? AND chapters.number = ?
            """,
            (novel_id, volume_number, chapter_number),
        )
    ).fetchone()
    if chapter is None:
        return None

    novel = await _get_novel(db, novel_id)

    progress = await (
        await db.execute(
            "SELECT chapter_id, paragraph_index, character_offset, scroll_percent FROM novel_reading_progress "
            "WHERE user_id = ? AND novel_id = ?",
            (user["id"], novel_id),
        )
    ).fetchone()
    # Only resume at the saved paragraph when the saved position is in THIS
    # chapter; -1 means "start at the top" (see reader.html's data-start-cfi).
    saved_paragraph = (
        progress["paragraph_index"]
        if progress and progress["chapter_id"] == chapter["id"] and progress["paragraph_index"] is not None
        else -1
    )

    return {
        "novel": novel, "chapter": chapter, "saved_paragraph": saved_paragraph,
        "saved_offset": progress["character_offset"] if saved_paragraph >= 0 else 0,
        # What the SERVER thought when this page was rendered, for
        # reader-resume.js to measure a later answer against. See
        # read_novel_progress.
        "start_percent": (progress["scroll_percent"] if progress else 0) or 0,
        # Display-only find-and-replace for this novel, applied by the
        # reader as it renders (see app/text_replacements.py) -- the
        # scraped chapter in `chapters.content_html` is never rewritten.
        "replacements_json": await text_replacements.rules_json(db, user["id"], novel_id=novel_id),
    }


@router.get("/novels/{novel_id}/read/{volume_number}/{chapter_number}", response_class=HTMLResponse)
async def read_chapter(
    novel_id: int, volume_number: int, chapter_number: float, request: Request,
    user=Depends(get_current_user), db=Depends(get_db),
):
    ctx = await _load_chapter_context(novel_id, volume_number, chapter_number, user, db)
    if ctx is None:
        raise HTTPException(404, "Chapter not found")
    return templates.TemplateResponse(
        request, "novels/reader.html", {"user": user, **ctx}, headers={"Cache-Control": "no-store"}
    )


# ---------- Progress / read-status APIs ----------

@router.get("/novels/{novel_id}/progress")
async def read_novel_progress(novel_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    """The novel half of the item reader's own GET -- see read_progress in
    app/reader/router.py for what asks and why.

    `scroll_percent` is the one reported, because it is of the WHOLE novel
    and that is the only scale on which "the other device is ahead" means
    anything; chapter_percent is of whichever chapter, and 90% of chapter
    2 is behind 10% of chapter 40.

    The URL is not this page. A novel reader is opened per chapter
    (/novels/{id}/read/{volume}/{chapter}), so reloading where you are
    would re-render the chapter you were already on and land at the top of
    it while the saved position sits eleven chapters further on. What goes
    back is the URL of the chapter the saved position is IN.
    """
    await _get_novel(db, novel_id)
    row = await (
        await db.execute(
            """
            SELECT novel_reading_progress.scroll_percent AS percent,
                   novel_reading_progress.paragraph_index, novel_reading_progress.character_offset,
                   chapters.order_index,
                   volumes.number AS volume_number, chapters.number AS chapter_number
            FROM novel_reading_progress
            LEFT JOIN chapters ON chapters.id = novel_reading_progress.chapter_id
            LEFT JOIN volumes ON volumes.id = chapters.volume_id
            WHERE novel_reading_progress.user_id = ? AND novel_reading_progress.novel_id = ?
            """,
            (user["id"], novel_id),
        )
    ).fetchone()
    # Nothing to go to: no saved position, a chapter with no NUMBER (the
    # column is nullable -- a scrape can produce one), or a chapter gone
    # out from under the row. A URL built from any of those is
    # /novels/1/read/2/ or /read/None/, which the reader route does not
    # answer -- and 0 is behind wherever the asking tab is, so it stays
    # exactly where it was, which is the right answer for all three.
    if row is None or row["volume_number"] is None or row["chapter_number"] is None:
        return JSONResponse({"progress_percent": 0, "resume_url": None})
    return JSONResponse({
        "progress_percent": row["percent"] or 0,
        "position": ([row["order_index"], row["paragraph_index"], row["character_offset"]]
                     if row["paragraph_index"] is not None else None),
        "resume_url": f"/novels/{novel_id}/read/"
                      f"{row['volume_number']}/{format_chapter_number(row['chapter_number'])}",
    })


@router.post("/novels/{novel_id}/progress")
async def save_novel_progress(novel_id: int, request: Request, user=Depends(get_current_user),
                              db=Depends(get_db)):
    # Checked at the boundary, like the item reader's own progress save --
    # see app/http_utils.py for why a percentage that reaches the column
    # unchecked is worse than a rejected request.
    try:
        body = json_object(await request.json())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Expected a JSON body")
    chapter_id = optional_index(body.get("chapter_id"), "chapter_id")
    if chapter_id is None:
        raise HTTPException(400, "chapter_id is required")
    # Two different questions, both answered by the same saved position.
    # scroll_percent is the whole novel (what the reading-rate learning
    # needs); chapter_percent is the chapter the reader is actually in
    # (what the cards show). Only the client can compute either -- it is
    # the side that knows the spine weights and where on the page the
    # reader stopped -- so both travel in the same request.
    scroll_percent = percent(body.get("scroll_percent", 0.0), "scroll_percent")
    chapter_percent = percent(body.get("chapter_percent", 0.0), "chapter_percent")
    paragraph_index = optional_index(body.get("paragraph_index"), "paragraph_index")
    character_offset = optional_index(body.get("character_offset", 0), "character_offset") or 0

    # A chapter of THIS novel -- the foreign key only says the chapter
    # exists, not that it belongs here, and the progress row is keyed by
    # (user, novel) with the chapter hanging off it.
    belongs = await (
        await db.execute("SELECT 1 FROM chapters WHERE id = ? AND novel_id = ?", (chapter_id, novel_id))
    ).fetchone()
    if belongs is None:
        raise HTTPException(404, "Chapter not found in this novel")

    async with write_transaction() as db:
        await db.execute(
            """
            INSERT INTO novel_reading_progress
                (user_id, novel_id, chapter_id, scroll_percent, chapter_percent, paragraph_index, character_offset, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, novel_id) DO UPDATE SET
                chapter_id = excluded.chapter_id,
                scroll_percent = excluded.scroll_percent,
                chapter_percent = excluded.chapter_percent,
                paragraph_index = excluded.paragraph_index,
                character_offset = excluded.character_offset,
                updated_at = datetime('now')
            """,
            (user["id"], novel_id, chapter_id, scroll_percent, chapter_percent, paragraph_index, character_offset),
        )
        await db.commit()

    # Same purpose as the item progress event (see reader/router.py): a
    # home page open elsewhere follows the percentage without a reload.
    # A novel has no per-item read status, so there is no status to send --
    # a saved position IS "reading" for a novel.
    #
    # `chapter_id` rides along because crossing into another chapter
    # changes the card's LABEL and its time estimate, which are
    # server-rendered -- the client compares it against data-chapter-id
    # and only then pays for a refetch, exactly as it does with
    # data-progress-status for items.
    events.publish({
        "type": "novel_progress_updated",
        "novel_id": novel_id,
        "chapter_id": chapter_id,
        "percent": round(chapter_percent or 0),
    })
    return JSONResponse({"ok": True})


# There used to be a GET counterpart to the save above, for the client to
# re-pull the latest cross-device position on tab focus. The reader has
# resumed from the position server-rendered into the page (see
# _load_chapter_context) since the two readers were unified, and nothing
# has called it since -- reader-text.js only ever POSTs here.


async def _mark_chapter_read(db, user_id: int, chapter_id: int,
                             read_from_percent: float | None = None) -> None:
    """Marks one chapter read. The whole-volume path (set_volume_read_status)
    and the tracker's (reading_progress.mark_novel_read_up_to) write the same
    row by the same rule in one INSERT ... SELECT each -- a loop of this
    over a hundred chapters was a hundred statements.

    `read_from_percent` is how far into THIS chapter the reader actually
    was: their saved position when it is the chapter they are in, and 0
    for any other -- which is what a chapter ticked off a list is. See
    novel_read_chapters.read_from_percent; the reading rate in
    stats/estimates.py pairs a chapter's seconds with its bytes, and
    without this a chapter abandoned at 10% and then marked read claims
    its whole text for those minutes.

    OR IGNORE, so re-marking something already read keeps what it first
    recorded -- the same rule mark_item_read follows.

    `read_from_percent`, when given, is the reader saying it read out of the
    chapter's end: the saved position is the START of the page it was on,
    so a chapter paged through to its last page recorded where that page
    began -- (P-1)/P of a P-page chapter, which made the reading rate claim
    that much less text for the time and inflated every estimate by P/(P-1).
    """
    await db.execute(
        """
        INSERT OR IGNORE INTO novel_read_chapters (user_id, chapter_id, read_from_percent)
        VALUES (?, ?, COALESCE(?, (SELECT chapter_percent FROM novel_reading_progress
                                    WHERE user_id = ? AND chapter_id = ?), 0))
        """,
        (user_id, chapter_id, read_from_percent, user_id, chapter_id),
    )


@router.post("/novels/chapters/{chapter_id}/read-status")
async def set_read_status(chapter_id: int, request: Request, user=Depends(get_current_user)):
    """Mark/unmark a chapter as read. Accepts JSON {read} and returns
    {ok, chapter_id, read}; the client updates the relevant button and row
    styling itself so this one endpoint serves both the reader page's
    toggle button and the chapter-list checkmarks."""
    try:
        body = json_object(await request.json())
    except ValueError:
        raise HTTPException(400, "Expected a JSON body")
    read = bool(body.get("read", True))
    read_from = body.get("read_from_percent")
    read_from = None if read_from is None else percent(read_from, "read_from_percent")

    async with write_transaction() as db:
        if read:
            await _mark_chapter_read(db, user["id"], chapter_id, read_from)
        else:
            await db.execute(
                "DELETE FROM novel_read_chapters WHERE user_id = ? AND chapter_id = ?", (user["id"], chapter_id)
            )
        # Read INSIDE the block: `db` is the write lock's connection, and
        # once the block ends it belongs to whichever writer is next.
        row = await (
            await db.execute("SELECT novel_id FROM chapters WHERE id = ?", (chapter_id,))
        ).fetchone()
        novel_id = row["novel_id"] if row else None
        await db.commit()
    if novel_id is not None:
        _publish_novel_updated(novel_id)
        if read:
            # Only on the way UP. Unmarking a chapter is not a claim to have
            # un-read it on MangaUpdates, and the push would decline to send
            # a lower number anyway -- so this asks nothing rather than
            # asking and being told no.
            tracking.queue_push(user["id"], novel_id=novel_id)
    return JSONResponse({"ok": True, "chapter_id": chapter_id, "read": read})


def _publish_novel_updated(novel_id: int) -> None:
    """Marking chapters read changes the novel's home card -- its unread
    count and time left -- and these endpoints published nothing, so an open
    home tab kept the old card until a reload. AGENTS.md: every path that
    changes progress or a status publishes."""
    events.publish({"type": "novel_updated", "novel_id": novel_id})


@router.post("/novels/volumes/{volume_id}/read-status")
async def set_volume_read_status(volume_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    """Mark/unmark every chapter in ONE volume as read -- the request
    body's {read: bool} applies to all chapters whose volume_id matches,
    and only those. Returns the affected chapter ids so the client can
    update every visible row/button without re-rendering the page."""
    try:
        body = json_object(await request.json())
    except ValueError:
        raise HTTPException(400, "Expected a JSON body")
    read = bool(body.get("read", True))

    chapter_rows = await (await db.execute("SELECT id FROM chapters WHERE volume_id = ?", (volume_id,))).fetchall()
    chapter_ids = [r["id"] for r in chapter_rows]

    if chapter_ids:
        async with write_transaction() as wdb:
            if read:
                # This is the path the column exists for: one click
                # declaring a hundred chapters finished, almost none of
                # which were opened. One statement, the same rule as
                # _mark_chapter_read for each (OR IGNORE; how far into the
                # chapter the reader was, 0 for one never opened).
                await wdb.execute(
                    """
                    INSERT OR IGNORE INTO novel_read_chapters (user_id, chapter_id, read_from_percent)
                    SELECT ?, chapters.id,
                           COALESCE((SELECT chapter_percent FROM novel_reading_progress
                                      WHERE user_id = ? AND chapter_id = chapters.id), 0)
                      FROM chapters WHERE chapters.volume_id = ?
                    """,
                    (user["id"], user["id"], volume_id),
                )
            else:
                placeholders = ",".join("?" * len(chapter_ids))
                await wdb.execute(
                    f"DELETE FROM novel_read_chapters WHERE user_id = ? AND chapter_id IN ({placeholders})",
                    (user["id"], *chapter_ids),
                )
            await wdb.commit()

    if chapter_ids:
        volume_row = await (
            await db.execute("SELECT novel_id FROM volumes WHERE id = ?", (volume_id,))
        ).fetchone()
        if volume_row:
            _publish_novel_updated(volume_row["novel_id"])
            if read:
                # One push for the whole volume: the number sent is a MAX
                # over what is marked read, so a hundred chapters and one
                # give the same answer, and this asks the service once.
                tracking.queue_push(user["id"], novel_id=volume_row["novel_id"])

    return JSONResponse({"ok": True, "chapter_ids": chapter_ids, "read": read})


# ---------- Settings integration (add / sync / toggle) ----------

async def list_novels_for_settings(db):
    """Used by library/router.py's settings_page to fold a "Web Novels"
    section into the same settings screen as everything else -- a
    personal-use app doesn't need a whole separate admin area just for
    "add a novel" and "sync now"."""
    novels = await (await db.execute(f"SELECT {NOVEL_COLUMNS} FROM novels ORDER BY title")).fetchall()
    chapter_counts: dict[int, tuple[int, int]] = {}
    if novels:
        rows = await (
            await db.execute(
                "SELECT novel_id, COUNT(*) AS total, COUNT(content_html) AS downloaded "
                "FROM chapters GROUP BY novel_id"
            )
        ).fetchall()
        chapter_counts = {r["novel_id"]: (r["total"], r["downloaded"]) for r in rows}
    return {"novels": novels, "chapter_counts": chapter_counts}


@router.post("/settings/novels/add")
async def add_novel(source_url: str = Form(...), user=Depends(get_current_user), db=Depends(get_db)):
    existing = await (await db.execute("SELECT id FROM novels WHERE source_url = ?", (source_url,))).fetchone()
    if existing is None:
        async with write_transaction() as wdb:
            # source_title gets the same placeholder as title. NULL there
            # means "a novel from before edits were recorded", which a sync
            # reads as "not edited" -- so a title changed by hand before the
            # first successful sync was overwritten by the site's.
            cursor = await wdb.execute(
                "INSERT INTO novels (title, source_title, source_url, sync_status) VALUES (?, ?, ?, 'queued')",
                (source_url, source_url, source_url),
            )
            await wdb.commit()
            novel_id = cursor.lastrowid
    else:
        novel_id = existing["id"]

    _run_background(_sync_with_error_log(novel_id))
    return RedirectResponse("/settings", status_code=302)


async def _sync_with_error_log(novel_id: int) -> None:
    """Fire-and-forget, but never silent: sync_novel persists its own
    status/error to novels.sync_status/sync_error at every stage (see
    novels/sync.py), and any exception is logged here as a last-resort
    safety net."""
    try:
        # Asked for by a person (adding a novel, Sync now): runs whether or
        # not the novel is in the periodic sync -- see sync_novel.
        await sync_novel(novel_id, manual=True)
    except Exception:
        logger.exception("Background sync task for novel_id=%s ended with an error", novel_id)


# Both of these are posted from two places now -- the settings list and the
# novel's own page -- so where to come back to is the caller's to say.
# `return_to` defaults to /settings, which is where they were born and what
# the settings forms still get by not sending it.


@router.post("/settings/novels/{novel_id}/sync")
async def sync_novel_now(novel_id: int, return_to: str | None = Form(default=None),
                         user=Depends(get_current_user)):
    async with write_transaction() as db:
        await db.execute(
            "UPDATE novels SET sync_status = 'queued', sync_error = NULL WHERE id = ?", (novel_id,)
        )
        await db.commit()
    _run_background(_sync_with_error_log(novel_id))
    return RedirectResponse(safe_redirect_target(return_to) or "/settings", status_code=302)


@router.post("/settings/novels/{novel_id}/toggle-active")
async def toggle_novel_active(novel_id: int, return_to: str | None = Form(default=None),
                              user=Depends(get_current_user)):
    async with write_transaction() as db:
        await db.execute("UPDATE novels SET active = 1 - active WHERE id = ?", (novel_id,))
        await db.commit()
    return RedirectResponse(safe_redirect_target(return_to) or "/settings", status_code=302)


# ---------- Editing what this app shows ----------
#
# Title and description are editable because the source's own wording is
# not always what you want to read on the shelf -- a machine-translated
# title, a description that opens with three lines of site boilerplate, a
# series everyone calls something shorter. Two forms, not one: they are
# different lengths and different kinds of writing, and saving a corrected
# title should not re-submit a paragraph you never touched.
#
# The panel and its endpoints are the series page's, in shape and for the
# same reasons (see _series_edit_response in library/router.py): over htmx
# the answer IS the panel, re-rendered from the database, so nothing
# reloads and the panel does not close; success is an HX-Trigger header
# rather than something inferred from the body, so an error -- the same
# panel carrying its message -- simply does not fire it.
#
# What makes an edit survive is in sync.py: `source_title`/`source_synopsis`
# keep following the site, and a sync overwrites the shown value only while
# it still matches them. Because the source's version is kept rather than
# overwritten, going back to it is a button press and not another scrape.

NOVEL_EDIT_ERRORS = {"empty-title": "A title cannot be empty."}


async def _novel_edit_response(request: Request, db, novel_id: int, error: str | None = None):
    """The novel page's half of _series_edit_response -- same shape, same
    reasons, including the 422 a refusal answers with and the HX-Trigger
    that a save adds on top of its 200. See that docstring."""
    if not is_htmx(request):
        # Still plain form posts without htmx, so the fallback is the
        # redirect they always were: re-rendering the whole page from a
        # POST would leave a URL that re-submits on reload.
        target = f"/novels/{novel_id}"
        if error:
            target = f"{target}?edit_error={error}"
        return RedirectResponse(target, status_code=302)

    novel = await _get_novel(db, novel_id)
    response = templates.TemplateResponse(
        request, "novels/_novel_edit.html",
        {"novel": novel, "oob": True, "edit_error": NOVEL_EDIT_ERRORS.get(error or "")},
        status_code=200 if error is None else 422,
    )
    if error is None:
        response.headers["HX-Trigger"] = "novel-edit-saved"
    return response


async def _write_novel_field(novel_id: int, column: str, value: str | None) -> None:
    async with write_transaction() as db:
        await db.execute(f"UPDATE novels SET {column} = ? WHERE id = ?", (value, novel_id))
        await db.commit()


@router.post("/novels/{novel_id}/title")
async def edit_novel_title(novel_id: int, request: Request, title: str = Form(default=""),
                           user=Depends(get_current_user), db=Depends(get_db)):
    await _get_novel(db, novel_id)
    cleaned = " ".join(title.split())
    # `novels.title` is NOT NULL and it is what every card, the reader and
    # the statistics all label this novel by, so an empty one is refused
    # rather than accepted and worked around downstream.
    if not cleaned:
        return await _novel_edit_response(request, db, novel_id, "empty-title")
    await _write_novel_field(novel_id, "title", cleaned)
    return await _novel_edit_response(request, db, novel_id)


@router.post("/novels/{novel_id}/title/reset")
async def reset_novel_title(novel_id: int, request: Request,
                            user=Depends(get_current_user), db=Depends(get_db)):
    novel = await _get_novel(db, novel_id)
    # A NULL source means nothing has ever been scraped for this field (a
    # novel added but never synced), so there is nothing to go back to --
    # leave what is there rather than blanking it. The button is not
    # rendered in that state either; this is the endpoint being asked
    # directly.
    if novel["source_title"]:
        await _write_novel_field(novel_id, "title", novel["source_title"])
    return await _novel_edit_response(request, db, novel_id)


@router.post("/novels/{novel_id}/synopsis")
async def edit_novel_synopsis(novel_id: int, request: Request, synopsis: str = Form(default=""),
                              user=Depends(get_current_user), db=Depends(get_db)):
    await _get_novel(db, novel_id)
    # Unlike the title, an empty description is a legitimate answer -- the
    # paragraph simply disappears -- so it is stored as NULL, which is what
    # the column already means everywhere else.
    await _write_novel_field(novel_id, "synopsis", synopsis.strip() or None)
    return await _novel_edit_response(request, db, novel_id)


@router.post("/novels/{novel_id}/synopsis/reset")
async def reset_novel_synopsis(novel_id: int, request: Request,
                               user=Depends(get_current_user), db=Depends(get_db)):
    novel = await _get_novel(db, novel_id)
    if novel["source_synopsis"]:
        await _write_novel_field(novel_id, "synopsis", novel["source_synopsis"])
    return await _novel_edit_response(request, db, novel_id)


@router.post("/novels/{novel_id}/publication-status")
async def set_novel_publication_status(novel_id: int, request: Request,
                                       user=Depends(get_current_user), db=Depends(get_db)):
    """Where the WORK is, said by hand, overriding the source page.

    A novel is normally never asked: its own page says whether it is
    finished, and `publication_status_from_source` maps that wording onto
    this vocabulary. What that mapping cannot do is recognise a wording it
    has never seen -- those get no badge at all rather than a guess -- and
    this is how a reader supplies one. An EMPTY value clears the override
    and goes back to following the source, which is the same shape the
    novel's title and synopsis already use.

    Validated against PUBLICATION_STATUS_LABELS here because the column
    has no CHECK: it arrives by ALTER TABLE, and SQLite cannot add one.
    """
    form = await request.form()
    status = (form.get("publication_status") or "").strip() or None
    if status is not None and status not in PUBLICATION_STATUS_LABELS:
        raise HTTPException(400, "Invalid publication status")

    await _get_novel(db, novel_id)
    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE novels SET publication_status = ? WHERE id = ?", (status, novel_id))
        await wdb.commit()

    # The home grid draws this as a badge and filters on it, and the page
    # it was changed on is not the page that shows it -- the same reason
    # the series twin publishes.
    events.publish({"type": "novel_updated", "novel_id": novel_id})

    if is_htmx(request):
        # The badge itself, not a 204: it wears the chosen value's COLOUR,
        # and a <select> keeps the class it was rendered with. The empty
        # option carries the SOURCE's own word, which is what makes going
        # back to it a choice rather than a guess -- and the only reason
        # this endpoint has to know about `novels.status` at all.
        cursor = await db.execute("SELECT status FROM novels WHERE id = ?", (novel_id,))
        row = await cursor.fetchone()
        source_label = PUBLICATION_STATUS_LABELS.get(
            publication_status_from_source(row["status"] if row else None))
        return publication_badge(
            f"/novels/{novel_id}/publication-status", status, PUBLICATION_STATUS_LABELS,
            empty_label="From the source" + (f" ({source_label})" if source_label else ""),
            source_word=row["status"] if row else None,
        )
    return redirect_back(request, f"/novels/{novel_id}")


# ---------- Status (same tracking labels as series_status, see app/status_labels.py) ----------

@router.post("/novels/{novel_id}/status")
async def set_novel_status(novel_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    form = await request.form()
    status = (form.get("status") or "").strip() or None
    if status is not None and status not in NOVEL_STATUS_LABELS:
        raise HTTPException(400, "Invalid status")

    await _get_novel(db, novel_id)

    async with write_transaction() as wdb:
        if status is None:
            await wdb.execute(
                "DELETE FROM novel_status WHERE user_id = ? AND novel_id = ?", (user["id"], novel_id)
            )
        else:
            await wdb.execute(
                """
                INSERT INTO novel_status (user_id, novel_id, status, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(user_id, novel_id) DO UPDATE SET status = excluded.status, updated_at = datetime('now')
                """,
                (user["id"], novel_id, status),
            )
        await wdb.commit()

    # A novel counts as "reading" on the home page while it has a saved
    # position AND no status of its own that says otherwise (see
    # _reading_items) -- so setting or clearing one here adds or removes
    # it from that section on every other open screen.
    events.publish({"type": "novel_status_updated", "novel_id": novel_id, "status": status})

    # Same as set_series_status in library/router.py: every status a
    # catalogue has a word for is reported, once per change.
    if status is not None:
        tracking.queue_push(user["id"], novel_id=novel_id)

    if is_htmx(request):
        # Same reasoning as set_series_status in library/router.py: the
        # badge's colour is half of what it says, so the endpoint answers
        # with the badge rather than a 204.
        return tracking_badge(f"/novels/{novel_id}/status", status, NOVEL_STATUS_LABELS)
    return redirect_back(request, f"/novels/{novel_id}")


@router.post("/novels/{novel_id}/clear-status")
async def clear_novel_status(novel_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    """Puts a novel back to never-opened: the saved position goes, and so
    does any status set by hand.

    The novel-side twin of /reader/{id}/clear-status (see
    reading_progress.mark_item_unread for the reasoning). The chapters
    marked read stay: those are a record of what was actually finished, not
    a side effect of opening the novel once.
    """
    await _get_novel(db, novel_id)
    async with write_transaction() as wdb:
        await wdb.execute(
            "DELETE FROM novel_reading_progress WHERE user_id = ? AND novel_id = ?",
            (user["id"], novel_id),
        )
        await wdb.execute(
            "DELETE FROM novel_status WHERE user_id = ? AND novel_id = ?", (user["id"], novel_id)
        )
        await wdb.commit()

    # Both halves of "is this being read" are gone, so every open screen
    # has to re-ask -- which is exactly what this event makes them do.
    events.publish({"type": "novel_status_updated", "novel_id": novel_id, "status": None})

    if is_htmx(request):
        return Response(status_code=204)
    return redirect_back(request, "/")


@router.post("/novels/{novel_id}/manual-sort/toggle")
async def toggle_novel_manual_sort(novel_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    novel = await _get_novel(db, novel_id)
    turning_editing_on = not novel["manual_sort_editing"]

    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE novels SET manual_sort_editing = ? WHERE id = ?",
            (1 if turning_editing_on else 0, novel_id),
        )
        if turning_editing_on and not novel["manual_sort"]:
            # First time this novel enters manual mode -- make it sticky
            # (see sync_novel: manual_sort alone decides whether a sync
            # renumbers chapters, regardless of manual_sort_editing, so
            # turning the drag UI back off does NOT let a later sync wipe
            # out the dragged order). No renumbering needed here:
            # order_index already holds a well-defined order from the last
            # sync, which is exactly the baseline dragging should start
            # from. Re-entering edit mode on an already-sticky novel skips
            # this -- its existing order_index is preserved as-is.
            await wdb.execute("UPDATE novels SET manual_sort = 1 WHERE id = ?", (novel_id,))
        await wdb.commit()
    return redirect_back(request, f"/novels/{novel_id}")


@router.post("/novels/{novel_id}/manual-sort/reset")
async def reset_novel_manual_sort(novel_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    """Discards the manual order entirely and goes back to automatic
    (volume/chapter number) -- unlike the toggle button, which only turns
    dragging on/off without touching the order itself. Renumbers
    order_index immediately, rather than leaving chapters in their old
    manual positions until the next sync happens to run."""
    await _get_novel(db, novel_id)

    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE novels SET manual_sort = 0, manual_sort_editing = 0 WHERE id = ?", (novel_id,)
        )
        await renumber_chapters_by_natural_order(wdb, novel_id)
        await wdb.commit()
    return redirect_back(request, f"/novels/{novel_id}")


@router.post("/novels/{novel_id}/manual-sort/reorder")
async def reorder_novel_chapters(novel_id: int, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    """Reorders the chapters of ONE volume in place -- chapter_ids must be
    exactly that volume's current chapters, given in the new order. They go
    in as one block where the volume's first chapter was, and the whole
    novel is numbered 0..n-1 again.

    It used to reuse the volume's min..min+n range, which is only that
    volume's if its chapters are contiguous -- and a sync in manual mode
    appends new chapters at the end of the NOVEL. A chapter appended to
    volume 1 and dragged into place then shared a position with one of
    volume 2, and links opened the wrong chapter."""
    novel = await _get_novel(db, novel_id)
    if not novel["manual_sort_editing"]:
        raise HTTPException(409, "Manual sort is not enabled for this novel")

    try:
        body = json_object(await request.json())
    except ValueError:
        raise HTTPException(400, "Expected a JSON body")
    volume_id = body.get("volume_id")
    chapter_ids = body.get("chapter_ids")
    if not isinstance(chapter_ids, list) or not chapter_ids:
        raise HTTPException(400, "chapter_ids must be a non-empty list")

    valid_rows = await (
        await db.execute(
            "SELECT id, order_index FROM chapters WHERE novel_id = ? AND volume_id = ?", (novel_id, volume_id)
        )
    ).fetchall()
    valid_ids = {r["id"] for r in valid_rows}
    if set(chapter_ids) != valid_ids:
        raise HTTPException(400, "chapter_ids must match exactly this volume's current chapters")

    async with write_transaction() as wdb:
        ordered = await (await wdb.execute(
            "SELECT id, volume_id, order_index FROM chapters WHERE novel_id = ? ORDER BY order_index, id",
            (novel_id,))).fetchall()
        # The volume, in its new order, as one block where its first chapter
        # was -- which also gathers in a chapter a sync appended at the end
        # of the novel, the way the page shows it, inside its volume.
        first = next(i for i, row in enumerate(ordered) if row["volume_id"] == volume_id)
        others = [row["id"] for row in ordered if row["volume_id"] != volume_id]
        before = sum(1 for row in ordered[:first] if row["volume_id"] != volume_id)
        sequence = others[:before] + list(chapter_ids) + others[before:]
        current = {row["id"]: row["order_index"] for row in ordered}
        for position, chapter_id in enumerate(sequence):
            if current[chapter_id] != position:
                await wdb.execute("UPDATE chapters SET order_index = ? WHERE id = ?", (position, chapter_id))
        await wdb.commit()
    return {"ok": True}
