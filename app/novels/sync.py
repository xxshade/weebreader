"""
Background sync job for web novels. Ported from a standalone companion
app's APScheduler-based scheduler.py (SQLAlchemy ORM) to this project's
raw-SQL / write_transaction pattern.

Each phase acquires the write lock only for its own short DB write --
mirrors the fix already applied to the comic/EPUB library scanner (see
library/scanner.py) of never holding the write lock across slow I/O.
fetch_novel_index/fetch_all_chapter_contents themselves are network-bound
and don't touch the database at all, so they run fully outside any lock.
"""

import asyncio
import datetime as dt
import hashlib
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.covers.thumbnails import THUMBNAIL_EXTENSION, encode_cover_thumbnail
from app.database import get_connection, write_transaction
from app.library import events
from app.novels.scraper import fetch_all_chapter_contents, fetch_image, fetch_novel_index

logger = logging.getLogger("novels.sync")


def _cover_version(image_bytes: bytes) -> str:
    """Short content hash stored alongside the cover, so `/novels/{id}/cover`
    can carry it as `?v=` and be cached immutably (see the route). Same idea
    as the hash in an item thumbnail's filename -- a cover that changes is a
    different URL -- expressed as a query parameter because this image lives
    in the row, not in a file whose name we get to choose."""
    return hashlib.sha256(image_bytes).hexdigest()[:12]

scheduler = AsyncIOScheduler()

# Guards against two overlapping syncs for the same novel -- e.g. clicking
# "sync now" right as the periodic job picks the same novel up. Without
# this, both would launch their own headless Chromium and race each other
# writing sync_status/chapters for the same rows.
_syncing_novel_ids: set[int] = set()


async def renumber_chapters_by_natural_order(db, novel_id: int) -> None:
    """Renumbers every chapter of this novel by (volume number, chapter
    number) ascending so display/navigation order matches the site's own,
    including chapters scraped before this ordering fix existed. Runs
    within the caller's own write_transaction -- doesn't commit itself.
    Used both by sync_novel() (whenever manual_sort is off) and by
    /novels/{id}/manual-sort/reset (to restore this order immediately,
    rather than waiting for the next sync).

    One statement, touching only the rows whose position moved: it was one
    UPDATE per chapter, every sync, whether anything had changed or not --
    71 ms of write lock for 900 chapters, against 1.8 ms.
    """
    await db.execute(
        """
        UPDATE chapters SET order_index = ranked.position
          FROM (SELECT chapters.id AS id,
                       ROW_NUMBER() OVER (
                           ORDER BY volumes.number ASC, chapters.number ASC, chapters.id ASC
                       ) - 1 AS position
                  FROM chapters
                  JOIN volumes ON volumes.id = chapters.volume_id
                 WHERE chapters.novel_id = ?) AS ranked
         WHERE chapters.id = ranked.id AND chapters.order_index IS NOT ranked.position
        """,
        (novel_id,),
    )


async def _set_status(novel_id: int, status: str, error: str | None = None) -> None:
    """Persist a short human-readable sync phase (and optional error) so the
    settings page can show live progress instead of a black box."""
    async with write_transaction() as db:
        await db.execute(
            "UPDATE novels SET sync_status = ?, sync_error = ? WHERE id = ?",
            (status[:128], error, novel_id),
        )
        await db.commit()
    logger.info("novel_id=%s status=%s%s", novel_id, status, f" error={error}" if error else "")


def reconcile_editable_metadata(*, title, synopsis, source_title, source_synopsis,
                                meta_title, meta_synopsis):
    """What a sync should leave in `title`, `synopsis`, `source_title` and
    `source_synopsis` after seeing the source page again.

    Those first two are the only fields of a novel the user can edit here
    (see /novels/{id}/title and /synopsis), so a sync cannot simply
    overwrite them: the nightly one would quietly undo the edit some hours
    after it was made, which reads as the app forgetting. What it may
    overwrite is a field still showing what the source last gave us -- once
    the shown value and the recorded source value differ, the shown one is
    the user's, and only source_* goes on following the site. Keeping the
    source's own wording rather than discarding it is what makes "use the
    source's again" a button press instead of another scrape.

    A NULL source_* is a novel from before any of this was recorded. It is
    read as "not edited", which was true of every novel that existed then,
    and this first pass fills it in. A source that reports nothing for a
    field (meta_* is None) leaves the recorded value alone rather than
    erasing it -- a scrape that came back empty is not the source saying
    the synopsis was deleted.
    """
    new_source_title = meta_title or source_title or title
    new_source_synopsis = meta_synopsis or source_synopsis or synopsis
    return (
        new_source_title if source_title in (None, title) else title,
        new_source_synopsis if source_synopsis in (None, synopsis) else synopsis,
        new_source_title,
        new_source_synopsis,
    )


async def sync_novel(novel_id: int, *, manual: bool = False) -> int:
    """Re-scrape a novel's index. Inserts newly discovered chapters, and also
    refreshes title/release_date/volume on chapters that already exist (so a
    parser fix retroactively repairs previously-scraped rows, not just new
    ones going forward). Fetches content for any chapter missing it. Volume
    titles are always normalized to "Volume N". After every sync, all
    chapters are renumbered by (volume number, chapter number) ascending so
    display order is correct regardless of the order the site listed them
    in. Returns the number of genuinely new chapters found.

    Every phase updates novels.sync_status/sync_error so progress is
    visible from the settings page even while this is still running in the
    background, and so a failure is never silent.

    `manual` is a sync somebody asked for (Sync now, adding a novel):
    `active` governs the PERIODIC sync only (see schema.sql), and a manual
    one of a paused novel used to return at once -- nothing scraped, and
    the settings page showing "queued" for good.
    """
    if novel_id in _syncing_novel_ids:
        logger.info("novel_id=%s sync already in progress, skipping this trigger", novel_id)
        return 0
    _syncing_novel_ids.add(novel_id)
    try:
        db = await get_connection()
        try:
            novel = await (
                await db.execute("SELECT active, source_url FROM novels WHERE id = ?", (novel_id,))
            ).fetchone()
        finally:
            await db.close()
        if novel is None or (not novel["active"] and not manual):
            return 0
        source_url = novel["source_url"]

        await _set_status(novel_id, "launching headless browser")
        meta = await fetch_novel_index(source_url)

        await _set_status(novel_id, f"found {len(meta.chapters)} chapter(s) on the site, comparing with database")

        needs_content_urls: list[str] = []
        cover_url_to_fetch: str | None = None
        new_count = 0

        async with write_transaction() as db:
            # `cover_image IS NULL` rather than selecting the BLOB itself:
            # all this needs to know is WHETHER a cover has been stored, and
            # the column holds the full image bytes (see NOVEL_COLUMNS in
            # novels/router.py for the same reasoning applied to listings).
            novel = await (
                await db.execute(
                    "SELECT title, cover_url, synopsis, source_title, source_synopsis, "
                    "status, manual_sort, "
                    "(cover_image IS NULL) AS has_no_cover FROM novels WHERE id = ?",
                    (novel_id,),
                )
            ).fetchone()
            manual_sort = bool(novel["manual_sort"])

            needs_cover_download = bool(meta.cover_url) and meta.cover_url != novel["cover_url"]
            new_cover_url = meta.cover_url or novel["cover_url"]
            if needs_cover_download or novel["has_no_cover"]:
                cover_url_to_fetch = new_cover_url

            title, synopsis, source_title, source_synopsis = reconcile_editable_metadata(
                title=novel["title"], synopsis=novel["synopsis"],
                source_title=novel["source_title"], source_synopsis=novel["source_synopsis"],
                meta_title=meta.title, meta_synopsis=meta.synopsis,
            )

            await db.execute(
                """
                UPDATE novels SET title = ?, synopsis = ?,
                    source_title = ?, source_synopsis = ?, status = ?,
                    last_checked_at = datetime('now')
                WHERE id = ?
                """,
                (
                    title,
                    synopsis,
                    source_title,
                    source_synopsis,
                    meta.status or novel["status"],
                    novel_id,
                ),
            )

            # `content_html IS NOT NULL`, not the column: all this asks is
            # whether the body still needs downloading, and selecting the
            # text itself read every chapter of the novel into memory on
            # every sync -- 5.4 MB for 900 chapters of 6 KB, thrown away
            # immediately. Same mistake, same fix, as the detail page's
            # `downloaded` flag.
            existing_rows = await (
                await db.execute(
                    "SELECT id, url, title, release_date, number, volume_id, "
                    "content_html IS NOT NULL AS has_content "
                    "FROM chapters WHERE novel_id = ?",
                    (novel_id,),
                )
            ).fetchall()
            existing_by_url = {r["url"]: r for r in existing_rows}

            volume_rows = await (
                await db.execute("SELECT id, number, title FROM volumes WHERE novel_id = ?", (novel_id,))
            ).fetchall()
            volumes_by_number = {r["number"]: r["id"] for r in volume_rows}
            # Normalized once per VOLUME, and only when it is not already:
            # this was an UPDATE per CHAPTER, every sync.
            for row in volume_rows:
                if row["title"] != f"Volume {row['number']}":
                    await db.execute("UPDATE volumes SET title = ? WHERE id = ?",
                                     (f"Volume {row['number']}", row["id"]))

            # A manually ordered novel takes its new chapters at the END,
            # numbered right here, in this transaction. They used to be
            # inserted at position 0 and moved only after every body had
            # downloaded -- and the reader opens a chapter by its position,
            # so for that whole window (half an hour for 900 chapters) the
            # links opened the wrong chapter, and reading across one marked
            # the newest chapters read and sent that to the trackers.
            max_row = await (await db.execute(
                "SELECT MAX(order_index) AS m FROM chapters WHERE novel_id = ?", (novel_id,))).fetchone()
            next_index = (max_row["m"] + 1) if max_row["m"] is not None else 0

            for ref in meta.chapters:
                volume_id = volumes_by_number.get(ref.volume_number)
                if volume_id is None:
                    cursor = await db.execute(
                        "INSERT INTO volumes (novel_id, number, title) VALUES (?, ?, ?)",
                        (novel_id, ref.volume_number, f"Volume {ref.volume_number}"),
                    )
                    volume_id = cursor.lastrowid
                    volumes_by_number[ref.volume_number] = volume_id

                existing = existing_by_url.get(ref.url)
                if existing is not None:
                    # Only a chapter the site describes differently now --
                    # a parser fix still repairs every old row, and an
                    # unchanged sync writes nothing.
                    if (existing["title"], existing["release_date"], existing["number"],
                            existing["volume_id"]) != (ref.title, ref.release_date,
                                                       ref.chapter_number, volume_id):
                        await db.execute(
                            "UPDATE chapters SET title = ?, release_date = ?, number = ?, volume_id = ? "
                            "WHERE id = ?",
                            (ref.title, ref.release_date, ref.chapter_number, volume_id, existing["id"]),
                        )
                    if not existing["has_content"]:
                        needs_content_urls.append(ref.url)
                else:
                    insert_cursor = await db.execute(
                        """
                        INSERT INTO chapters (novel_id, volume_id, number, title, url, release_date, order_index)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (novel_id, volume_id, ref.chapter_number, ref.title, ref.url, ref.release_date,
                         next_index),
                    )
                    next_index += 1
                    new_count += 1
                    needs_content_urls.append(ref.url)
                    # Listed twice is still one chapter (the parser dedupes
                    # too; this is what keeps the insert from failing the
                    # whole sync if anything else ever produces a repeat).
                    existing_by_url[ref.url] = {
                        "id": insert_cursor.lastrowid, "title": ref.title, "release_date": ref.release_date,
                        "number": ref.chapter_number, "volume_id": volume_id, "has_content": False,
                    }

            if not manual_sort:
                await renumber_chapters_by_natural_order(db, novel_id)
            # A volume left with no chapter (one moved to another volume
            # upstream) was kept, and the novel page opened it first.
            await db.execute(
                "DELETE FROM volumes WHERE novel_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM chapters WHERE chapters.volume_id = volumes.id)",
                (novel_id,),
            )

            await db.commit()
        if new_count:
            # The home card's unread count, time left and "Recently
            # modified" place all move with a new chapter.
            events.publish({"type": "novel_updated", "novel_id": novel_id})

        if cover_url_to_fetch:
            await _set_status(novel_id, "downloading cover image")
            downloaded = await fetch_image(cover_url_to_fetch)
            if downloaded:
                image_bytes, content_type = downloaded
                # Stored through the same thumbnail pipeline as comic/ebook
                # covers instead of as the source site served it: the same
                # 400x600 WebP, in a BLOB that is read back on every cover
                # request. A source page's own cover is routinely several
                # hundred KB of full-size JPEG.
                try:
                    image_bytes = await encode_cover_thumbnail(image_bytes)
                    content_type = f"image/{THUMBNAIL_EXTENSION}"
                except Exception:
                    # An image Pillow cannot read is not a reason to lose
                    # the cover -- store what was downloaded, as before.
                    logger.exception("Could not encode cover for novel %s; storing as downloaded", novel_id)
                # The URL is recorded WITH the image, never before it: it is
                # what the next sync compares against to decide whether to
                # download, so written first and followed by a failed
                # download it said "have it" -- and the old picture stayed
                # for good.
                async with write_transaction() as db:
                    await db.execute(
                        "UPDATE novels SET cover_url = ?, cover_image = ?, cover_image_content_type = ?, "
                        "cover_image_hash = ? WHERE id = ?",
                        (cover_url_to_fetch, image_bytes, content_type, _cover_version(image_bytes),
                         novel_id),
                    )
                    await db.commit()

        fetched_count = 0
        if needs_content_urls:
            total = len(needs_content_urls)
            await _set_status(novel_id, f"downloading chapter 0/{total}")

            async def on_chapter_done(i, tot, url, success, content):
                nonlocal fetched_count
                if success and content:
                    async with write_transaction() as chapter_db:
                        await chapter_db.execute(
                            "UPDATE chapters SET content_html = ?, content_size = LENGTH(?), "
                            "content_fetched_at = datetime('now') "
                            "WHERE novel_id = ? AND url = ?",
                            (content, content, novel_id, url),
                        )
                        await chapter_db.commit()
                    fetched_count += 1
                await _set_status(
                    novel_id, f"downloading chapter {i}/{tot}" + ("" if success else f" (last one failed: {url})")
                )

            await fetch_all_chapter_contents(needs_content_urls, on_chapter_done=on_chapter_done)

        await _set_status(
            novel_id, f"done — {new_count} new chapter(s), {fetched_count} chapter body/bodies downloaded"
        )
        logger.info(
            "Synced novel_id=%s: %d new chapters, %d bodies downloaded", novel_id, new_count, fetched_count
        )
        return new_count

    except Exception as exc:
        logger.exception("Sync failed for novel_id=%s", novel_id)
        await _set_status(novel_id, "error", error=str(exc))
        raise
    finally:
        _syncing_novel_ids.discard(novel_id)


async def sync_all_active_novels() -> None:
    db = await get_connection()
    try:
        rows = await (await db.execute("SELECT id FROM novels WHERE active = 1")).fetchall()
    finally:
        await db.close()
    ids = [r["id"] for r in rows]
    for i, novel_id in enumerate(ids):
        if i > 0:
            # A short pause between different novels' initial page loads --
            # the per-chapter delay inside fetch_all_chapter_contents only
            # covers requests within one novel, so back-to-back novels would
            # otherwise hit the site with no gap at all.
            await asyncio.sleep(settings.novel_scrape_request_delay_seconds)
        try:
            await sync_novel(novel_id)
        except Exception:
            # sync_novel already logged + persisted the error; this catch
            # only prevents one novel's failure from stopping the batch.
            pass


def start_scheduler() -> None:
    if scheduler.running:
        return
    jitter_seconds = int(settings.novel_scrape_interval_minutes * 60 * 0.10)  # ±10%
    scheduler.add_job(
        sync_all_active_novels,
        "interval",
        minutes=settings.novel_scrape_interval_minutes,
        jitter=jitter_seconds,
        id="sync_all_active_novels",
        next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


def stop_scheduler() -> None:
    """Called from app.main's lifespan shutdown. wait=False: a sync in
    progress may have a headless Chromium mid-scrape (see scraper.py), which
    can run far longer than Docker's stop grace period (10s by default)
    before it sends SIGKILL -- blocking here would just mean the container
    gets killed anyway, mid-write, instead of shutting down cleanly."""
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def backfill_chapter_content_size() -> int:
    """Fills in `chapters.content_size` for chapters scraped before the
    column existed, once, at startup.

    Same shape as the backfills in main.py's lifespan and for the same
    reason: a sync only ever writes a chapter's body when it does not
    already have one, so nothing would revisit the rows already stored.

    One UPDATE for the whole table rather than a walk: it is the only time
    the text has to be read, and reading it row by row from Python would
    carry every chapter of every novel across the connection to compute a
    number SQLite can compute in place. Does nothing at all on every start
    after the first -- the WHERE matches no rows -- which is what makes it
    safe to run unconditionally.
    """
    async with write_transaction() as db:
        cursor = await db.execute(
            "UPDATE chapters SET content_size = LENGTH(content_html) "
            "WHERE content_size IS NULL AND content_html IS NOT NULL"
        )
        filled = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        await db.commit()
    if filled:
        logger.info("Backfilled content_size for %d chapter(s)", filled)
    return filled


async def backfill_novel_cover_thumbnails() -> None:
    """Re-encodes covers stored before they went through the thumbnail
    pipeline, and fills in the hash their URL now carries, once, at startup.

    A novel's cover is only ever downloaded when it has none
    (`cover_image IS NULL`, see sync_novel), so a sync would never revisit
    one that is already stored -- without this, every novel imported before
    the change would keep its full-size original forever. There are as many
    of these as the user has novels (tens, not thousands), and each is one
    decode plus one encode, so it is done in one pass rather than lazily.
    """
    read_db = await get_connection()
    try:
        cursor = await read_db.execute(
            "SELECT id FROM novels WHERE cover_image IS NOT NULL "
            "AND (COALESCE(cover_image_content_type, '') != ? OR cover_image_hash IS NULL)",
            (f"image/{THUMBNAIL_EXTENSION}",),
        )
        pending = [row["id"] for row in await cursor.fetchall()]
    finally:
        await read_db.close()

    if not pending:
        return

    converted = 0
    for novel_id in pending:
        read_db = await get_connection()
        try:
            cursor = await read_db.execute(
                "SELECT cover_image, cover_image_content_type FROM novels WHERE id = ?", (novel_id,)
            )
            row = await cursor.fetchone()
        finally:
            await read_db.close()
        if row is None or not row["cover_image"]:
            continue
        stored = row["cover_image"]
        content_type = row["cover_image_content_type"]
        if content_type != f"image/{THUMBNAIL_EXTENSION}":
            try:
                stored = await encode_cover_thumbnail(stored)
                content_type = f"image/{THUMBNAIL_EXTENSION}"
                converted += 1
            except Exception:
                # Left exactly as it is: it still serves, with its own
                # stored content type. It does get a hash, so at least its
                # URL stops being re-fetched daily.
                logger.exception("Could not convert stored cover for novel %s", novel_id)
        async with write_transaction() as db:
            await db.execute(
                "UPDATE novels SET cover_image = ?, cover_image_content_type = ?, "
                "cover_image_hash = ? WHERE id = ?",
                (stored, content_type, _cover_version(stored), novel_id),
            )
            await db.commit()

    if converted:
        logger.info("Converted %s novel cover(s) to %s", converted, THUMBNAIL_EXTENSION)
