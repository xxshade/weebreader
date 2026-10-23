"""
Import/scan flow (see the architecture document, section 7).

`scan_library()` does a complete, idempotent scan of a library.
`process_file()` processes a single (new/updated) file and is reused both by
the initial scan and by the incremental watcher -- only who decides *when*
to call it changes.
"""

import asyncio
import hashlib
import json
import logging
import os
import stat
import time
import zipfile
from pathlib import Path

from app.config import settings
from app.covers.thumbnails import (
    THUMBNAIL_EXTENSION,
    delete_custom_series_cover,
    encode_cover_thumbnail,
    migrate_legacy_thumbnail,
    write_cover_thumbnail,
)
from app.database import get_connection, read_connection, write_transaction
from app.library import events, job_status
from app.library.series_scope import items_order
from app.library.naturalsort import (
    extract_candidates,
    extract_number_in_series,
    natural_sort_key,
    resolve_series_numbers,
)
from app.library.parsers import cbz as cbz_parser
from app.library.parsers import epub as epub_parser

logger = logging.getLogger("scanner")

ELIGIBLE_EXTENSIONS = {".epub", ".cbz", ".zip", ".cbr", ".rar"}
IGNORED_SUFFIXES = {".part", ".tmp", ".crdownload"}


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# Folders that are never part of a library: anything hidden (a desktop's
# .Trash-1000, Syncthing's .stversions, a NAS's .@__thumb) and the recycle
# bins and thumbnail stores NAS systems put in every share. They were
# walked like any other folder and imported as series: on a USB drive,
# deleting a volume in a file manager MOVES it into .Trash-1000 inside the
# library, so the series came straight back as a new one.
IGNORED_FOLDERS = {"#recycle", "#snapshot", "@eaDir", "@Recycle", "$RECYCLE.BIN",
                   "System Volume Information"}


def _ignored_folder(name: str) -> bool:
    return name.startswith(".") or name in IGNORED_FOLDERS


def _inside_ignored_folder(path: Path, root: Path) -> bool:
    """Whether `path` sits in a folder no library includes -- judged below
    the library root only, so a library that itself lives under a dot
    folder is still a library. Anything outside the root is outside it."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(_ignored_folder(part) for part in relative.parts[:-1])


def _is_eligible(path: Path) -> bool:
    if path.name.startswith("."):
        return False
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return False
    return path.suffix.lower() in ELIGIBLE_EXTENSIONS


async def _log_error(db, library_id: int, file_path: str, detail: str) -> None:
    """A problem /library/issues lists. Errors are the only rows scan_events
    holds: 'added', 'modified' and 'removed' were written for every import
    and removal and never read by anything."""
    await db.execute(
        "INSERT INTO scan_events (library_id, event_type, file_path, detail) VALUES (?, 'error', ?, ?)",
        (library_id, file_path, detail),
    )


def automatic_series_name(folder_path: str) -> str:
    """The name a scan gives the series in this folder: the folder's own.
    A series' name differs from it only by a rename the reader asked for,
    which is what lets the series page offer "Reset" without storing the
    original anywhere."""
    return Path(folder_path).name


async def _get_or_create_series(db, library_id: int, root: Path, file_path: Path,
                                adopt_series_id: int | None = None):
    """Folder-based organization: the immediate folder containing the file
    is the series.

    Returns (series_id, appeared) -- `appeared` is only used to decide
    whether to publish a "series_added" event (see process_file), so the
    question it answers is "does this series need to show up on a screen
    that does not have it", not "was this row INSERTed". Those came apart
    once an emptied series could be kept (see _series_holds_reader_state):
    a chapter landing back in its folder finds the existing row, so keying
    the event on the INSERT left the card missing from every open grid
    until a reload -- the series is listed only while it holds items."""
    parent = file_path.parent
    if parent == root:
        return None, False  # standalone item, no series

    folder_path = str(parent.relative_to(root))
    cursor = await db.execute(
        "SELECT id FROM series WHERE library_id = ? AND folder_path = ?",
        (library_id, folder_path),
    )
    row = await cursor.fetchone()
    if row is not None:
        was_empty = not (await (await db.execute(
            "SELECT EXISTS(SELECT 1 FROM items WHERE series_id = ?) AS n", (row["id"],)
        )).fetchone())["n"]
        return row["id"], was_empty

    # parent.name and not automatic_series_name(folder_path): a file at the
    # library's root has folder_path ".", which has no name.
    name = parent.name
    if adopt_series_id is not None:
        # The folder was RENAMED (see _moved_item): the series follows it,
        # with its status, tracker links, text replacements, summary and
        # everything edited on its page. A name the reader typed stays;
        # the automatic one -- the folder's own -- becomes the new folder's.
        old = await (await db.execute(
            "SELECT name, folder_path FROM series WHERE id = ?", (adopt_series_id,))).fetchone()
        if old is not None:
            automatic = old["name"] == automatic_series_name(old["folder_path"])
            await db.execute(
                "UPDATE series SET folder_path = ?, name = ?, sort_name = ? WHERE id = ?",
                (folder_path,
                 name if automatic else old["name"],
                 natural_sort_key(name if automatic else old["name"]),
                 adopt_series_id))
            return adopt_series_id, False
    await db.execute(
        "INSERT INTO series (library_id, name, sort_name, folder_path) VALUES (?, ?, ?, ?)",
        (library_id, name, natural_sort_key(name), folder_path),
    )
    cursor = await db.execute(
        "SELECT id FROM series WHERE library_id = ? AND folder_path = ?",
        (library_id, folder_path),
    )
    return (await cursor.fetchone())["id"], True


async def _touch_series(db, series_id: int) -> None:
    """Records that the series' CONTENTS changed -- an item was imported,
    reprocessed after changing on disk, or removed.

    `series.updated_at` had a DEFAULT and nothing ever wrote to it, so it
    equalled `created_at` on every row and the home page's "Recently
    modified" sort was a duplicate of "Recently added" (see
    library/index.html). Called from exactly the two places where the
    membership actually changes -- process_file and _delete_item -- and
    deliberately NOT from scan_library's reconciliation pass, which visits
    every series on every scan whether or not anything happened to it, and
    would therefore mark the entire library as modified nightly.
    """
    await db.execute("UPDATE series SET updated_at = datetime('now') WHERE id = ?", (series_id,))


async def _recompute_series_cover(db, series_id: int) -> None:
    """Series cover = cover of the first item in the series' current order.
    Recomputed from scratch on every call rather than tracked incrementally
    -- cheap enough for the size of a personal library, and always correct
    even if volumes are imported out of order or the item that owned the
    cover was just removed. Shared by process_file (one series, right after
    an item changes), _delete_item (one series, right after an item is
    removed), scan_library's full reconciliation pass (every series in the
    library, since a series whose items were all already imported in an
    earlier session never re-enters process_file), and the manual-sort
    toggle/reorder/reset endpoints (dragging a different item into first
    place, or resetting away from manual order, changes what "first" means).

    manual_sort is sticky (see series_detail's ORDER BY in library/router.py)
    -- once the user has dragged items into place, that's what "first" means
    here too, whether or not the drag UI itself is currently open.

    A hand-uploaded cover is left completely alone. This runs from every
    path that touches a series, so without that check an override would
    survive only until the next scan -- which is the same trap the manual
    ORDER already avoids by being sticky."""
    cursor = await db.execute(
        "SELECT manual_sort, cover_overridden FROM series WHERE id = ?", (series_id,)
    )
    series = await cursor.fetchone()
    if series and series["cover_overridden"]:
        return
    order_by = f"ORDER BY {items_order(series and series['manual_sort'])}"
    cursor = await db.execute(
        f"SELECT cover_path FROM items WHERE series_id = ? AND cover_path IS NOT NULL {order_by} LIMIT 1",
        (series_id,),
    )
    first_cover = await cursor.fetchone()
    await db.execute(
        "UPDATE series SET cover_path = ? WHERE id = ?",
        (first_cover["cover_path"] if first_cover else None, series_id),
    )


def _path_exists(path: Path) -> bool | None:
    """True/False only when known; None when the path cannot be inspected.

    Python 3.14's Path.exists() swallows every OSError, including EACCES.
    stat() preserves the distinction that destructive reconciliation needs.
    """
    try:
        path.stat()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def _folder_has_eligible_file(folder: Path) -> bool | None:
    """Whether `folder` directly contains at least one importable file.

    Returns None -- meaning "couldn't tell" -- when the folder can't be
    listed at all, so callers can distinguish that from a confident "no"
    (see the reconciliation pass in scan_library: a confident "no" deletes
    the series, "couldn't tell" must not).
    """
    try:
        if not stat.S_ISDIR(folder.stat().st_mode):
            return False
        entries = list(folder.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None
    try:
        for entry in entries:
            if not _is_eligible(entry):
                continue
            try:
                if stat.S_ISREG(entry.stat().st_mode):
                    return True
            except (FileNotFoundError, NotADirectoryError):
                continue  # a file disappeared after the directory was listed
        return False
    except OSError:
        return None


class UnknownArchiveFormat(Exception):
    """`transient` distinguishes "this file will never be readable" (a real
    RAR) from "this file is not a valid archive YET" -- which is what a
    file still being copied, downloaded or moved into the library looks
    like. See _readable_archive_when_settled for why that distinction is
    worth carrying."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def _detect_archive_kind(path: Path) -> str:
    """Detects the file's real format from its magic bytes, not its
    extension -- an extension alone is not trustworthy in a manga library
    (a .cbz that is really a RAR, and the reverse, both happen)."""
    with open(path, "rb") as f:
        header = f.read(8)
    if header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06"):
        return "zip"
    if header.startswith(b"Rar!\x1a\x07"):
        return "rar"
    return "unknown"


def _check_readable_archive(file_path: Path) -> None:
    """Blocking: raises if this file isn't an archive the reader can serve
    pages from directly. Called via asyncio.to_thread -- see process_file.

    There used to be a CBR->CBZ conversion step here: a RAR was unpacked
    via the `unrar` binary and repackaged as a .cbz in a separate
    `converted/` directory, and the item was then recorded against THAT
    path instead of the real file on disk. It was removed, for three
    independent reasons:

    1. It generated duplicate items. `scan_library`'s reconciliation
       collects the paths it walked (originals, inside the library) and
       removes any item whose recorded path isn't among them -- unless
       that path still exists. A converted item's path pointed into
       `converted/`, outside the library, which always still existed, so
       the row was permanently immune to removal. Replace the source file
       with a real ZIP (or just delete it) and the stale row stayed
       forever, next to the newly imported one: the same volume listed
       twice on the series page.
    2. It did not actually work for the format that motivated it. The
       image shipped `unrar-free`, which handles RAR4 but not RAR5 -- so a
       RAR5 file, the common case today, failed conversion anyway.
    3. It bought nothing for real files. The library this was built for
       turned out to be genuine ZIPs all along, opening normally without
       any conversion.

    So a RAR now reports a clear, actionable error instead of being
    silently rewritten into a second copy of itself. Detection stays --
    without it `zipfile` raises `BadZipFile`, which says nothing about
    what is actually wrong or how to fix it.
    """
    kind = _detect_archive_kind(file_path)
    if kind == "rar":
        raise UnknownArchiveFormat(
            f"This file is a RAR archive, not a ZIP: {file_path}. Comics are read "
            "directly from the file, so repackage it as a real .cbz (a ZIP) -- "
            "renaming a .rar to .cbz is not enough."
        )
    if kind == "unknown":
        # Transient: the magic bytes are the FIRST bytes of the file, so a
        # file that has only just been created (or pre-allocated with
        # zeros, which is what a torrent client does) reads as "unknown"
        # and turns into a real archive seconds later.
        raise UnknownArchiveFormat(
            f"Neither ZIP nor RAR recognized from the magic bytes: {file_path}", transient=True
        )

    # kind == "zip", so the file STARTS like an archive -- which says
    # nothing about whether all of it has arrived. A zip's central
    # directory is written last, at the END of the file, so opening it is
    # what actually proves the copy finished; a half-copied file gets this
    # far and then fails deeper in, with zipfile's bare "File is not a zip
    # file". EPUBs used to skip this whole function, which is exactly why
    # they reported that message raw. Only the central directory is read
    # here -- no entry is decompressed.
    try:
        with zipfile.ZipFile(file_path):
            pass
    except zipfile.BadZipFile as exc:
        raise UnknownArchiveFormat(
            f"Incomplete or corrupt archive: {file_path} ({exc}). If the file was still "
            "being copied or downloaded, this resolves itself once it finishes.",
            transient=True,
        ) from exc


# A file arriving in the library is not atomic: a copy, a download or a
# move across filesystems makes it visible long before it is complete, and
# the watcher's debounce cannot tell "finished" from "paused" -- any gap
# longer than the debounce fires on a half-written file. Timing alone
# therefore cannot answer the question; the file's own content can, since
# a zip is only openable once all of it is there. So a file that isn't a
# valid archive YET is re-checked a few times before being called broken.
# A RAR is not retried: it is instantly identifiable and will never become
# a zip on its own.
_INCOMPLETE_RETRY_DELAYS = (2.0, 5.0, 10.0)

# Only a file modified this recently can still be arriving. One that has
# sat unchanged for longer is simply broken, and waiting on it again cost
# every scan 17 seconds per broken file, one after the other.
SETTLING_SECONDS = 120


async def _readable_archive_when_settled(file_path: Path, *, settling: bool = True) -> None:
    for delay in (_INCOMPLETE_RETRY_DELAYS if settling else ()):
        try:
            await asyncio.to_thread(_check_readable_archive, file_path)
            return
        except UnknownArchiveFormat as exc:
            if not exc.transient:
                raise
            logger.info("Not a complete archive yet, will re-check: %s", file_path)
        await asyncio.sleep(delay)
    # Out of attempts (or none were due): report it. If the file really was just slow to
    # arrive, the watcher fires again when the copy finally ends and the
    # successful reprocess clears this error (see _clear_errors_for).
    await asyncio.to_thread(_check_readable_archive, file_path)


async def _replace_error(db, library_id: int, file_path: str, detail: str) -> None:
    """Records a processing error, replacing any earlier one for the same
    file: /library/issues should say what is wrong with a file now, not
    carry one row per scan that ever tried it."""
    await _clear_errors_for(db, file_path)
    await _log_error(db, library_id, file_path, detail)


async def _clear_errors_for(db, file_path: str) -> None:
    """Drops previously logged errors for a path that has now been read
    successfully. Without this, /library/issues is an append-only log
    presented as current state: a file that failed once while it was still
    being copied stayed listed as a problem forever, even though every
    scan since read it fine, and the only way out was the "clear errors"
    button, which wipes the genuine ones too."""
    await db.execute(
        "DELETE FROM scan_events WHERE event_type = 'error' AND file_path = ?", (file_path,)
    )


async def _record_unchanged(existing, db_path: str, had_errors: bool, mtime_ns: int) -> None:
    """A file whose content is what was imported. Nothing to reprocess --
    but reading it this far proves the file is fine, which is news if it
    failed on an earlier pass (typically: the scan caught it mid-copy);
    its modification time may have moved without its content (a `touch`,
    a copy that kept the bytes), and is recorded so the next scan skips
    it without hashing; and this is where a library imported before
    thumbnails became WebP converts itself -- nothing about the FILE
    changed, so this is the only pass its cover would ever get."""
    new_cover = await migrate_legacy_thumbnail(existing["cover_path"])
    moved = existing["file_mtime_ns"] != mtime_ns
    if not (had_errors or new_cover or moved):
        return
    async with write_transaction() as db:
        if had_errors:
            await _clear_errors_for(db, db_path)
        if moved:
            await db.execute("UPDATE items SET file_mtime_ns = ? WHERE id = ?", (mtime_ns, existing["id"]))
        if new_cover:
            await db.execute(
                "UPDATE items SET cover_path = ? WHERE id = ?", (new_cover, existing["id"])
            )
            if existing["series_id"] is not None:
                # series.cover_path holds a COPY of the item's file
                # name, so it has to follow the rename or it points
                # at a file that no longer exists.
                await _recompute_series_cover(db, existing["series_id"])
        await db.commit()


async def _moved_item(library_id: int, root: Path, file_hash: str, db_path: str) -> dict | None:
    """The item a NEW path is, when it is a file already in the library that
    was renamed or moved: same content (hash) as an item whose own path is
    confidently gone. None when there is no such item, or more than one
    could be it.

    An item used to BE its path, so renaming "Serie 1.cbz" to "Serie
    01.cbz" (the zero padding this scanner's own numbering asks for)
    imported a new, unread item and deleted the old one with its reading
    progress; renaming a series folder did it to every item at once and
    stranded the series' status on an empty row. The content is what the
    reader read, so the content is what is followed.

    A file that is merely a COPY (the original still there) is a new item;
    and a file put back after its item was removed is too -- that item is
    gone, and its reading stays a removed item (GLOSSARY.md).

    `series_gone` says the item's series folder has disappeared with it:
    the folder was renamed, and the series moves along (see
    _get_or_create_series)."""
    async with read_connection() as rdb:
        rows = await (await rdb.execute(
            "SELECT items.id, items.file_path, series.folder_path FROM items"
            " LEFT JOIN series ON series.id = items.series_id"
            " WHERE items.library_id = ? AND items.file_hash = ? AND items.file_path != ?",
            (library_id, file_hash, db_path))).fetchall()
    if not rows:
        return None
    gone = await asyncio.to_thread(_missing, [row["file_path"] for row in rows])
    candidates = [row for row in rows if row["file_path"] in gone]
    if len(candidates) != 1:
        return None
    row = candidates[0]
    series_gone = False
    if row["folder_path"] is not None:
        new_folder = str(Path(db_path).parent.relative_to(root)) if Path(db_path).parent != root else None
        if new_folder != row["folder_path"]:
            series_gone = await asyncio.to_thread(_path_exists, root / row["folder_path"]) is False
    return {"id": row["id"], "file_path": row["file_path"], "series_gone": series_gone}


async def process_file(library_id: int, root_path: str, file_path: Path,
                       renumber_later: set[int] | None = None) -> bool:
    """Return whether an item was imported or confirmed unchanged. Cheap for a file
    that has not changed.

    "Not changed" is decided by the file's size and modification time
    against what was recorded when it was imported, before anything reads
    the file. Every scan used to hash every file in full just to learn
    that it was the same -- the whole library read from disk once a day
    by the scheduled scan. The hash is still what decides for a file whose
    size or time moved.

    Format detection, hashing and parsing are blocking, and run in
    threads; the write lock is taken only for the database writes at the
    end, so a scan never freezes the app or holds up a progress save.

    `renumber_later`, from a scan: the series this file lands in or leaves
    is added to it instead of being renumbered here, and the scan renumbers
    each one once at the end. Renumbering reads the whole series, so doing
    it per file made importing a series quadratic (1000 files: 18.4 s of
    the 25.9 s import, on the event loop, inside the write lock).
    """
    root = Path(root_path)
    db_path = str(file_path)

    # Plain reads, on a pooled connection: on a rescan the common case is
    # that nothing changed, which lets it finish without the write lock.
    # The error check rides along; the table only ever holds errors.
    async with read_connection() as rdb:
        cursor = await rdb.execute("SELECT * FROM items WHERE file_path = ?", (db_path,))
        existing = await cursor.fetchone()
        cursor = await rdb.execute(
            "SELECT 1 FROM scan_events WHERE event_type = 'error' AND file_path = ? LIMIT 1", (db_path,)
        )
        had_errors = await cursor.fetchone() is not None

    try:
        stat = await asyncio.to_thread(file_path.stat)
    except OSError:
        stat = None  # gone, or unreadable: the checks below say which, as before
    if (stat is not None and existing is not None and not had_errors
            and existing["file_size"] == stat.st_size
            and existing["file_mtime_ns"] == stat.st_mtime_ns):
        await _record_unchanged(existing, db_path, had_errors, stat.st_mtime_ns)
        return True

    try:
        if stat is None:
            stat = await asyncio.to_thread(file_path.stat)
        await _readable_archive_when_settled(
            file_path, settling=time.time() - stat.st_mtime < SETTLING_SECONDS)
        file_hash = await asyncio.to_thread(_sha256_of, file_path)
    except Exception as exc:
        logger.exception("Failed to process %s", file_path)
        async with write_transaction() as db:
            await _replace_error(db, library_id, str(file_path), str(exc))
            await db.commit()
        return False

    file_format = "epub" if file_path.suffix.lower() == ".epub" else "cbz"

    if existing is not None and existing["file_hash"] == file_hash:
        await _record_unchanged(existing, db_path, had_errors, stat.st_mtime_ns)
        return True

    try:
        parse_fn = epub_parser.parse_epub if file_format == "epub" else cbz_parser.parse_cbz
        parsed = await asyncio.to_thread(parse_fn, db_path)
    except Exception as exc:
        logger.exception("Failed to process %s", file_path)
        async with write_transaction() as db:
            await _replace_error(db, library_id, str(file_path), str(exc))
            await db.commit()
        return False

    # The thumbnail is encoded here, before the write lock: it is the slow
    # part of an import (83 ms for a 1.4 MB page), and inside the lock every
    # progress save queued behind it -- with 100 files dropped in at once,
    # a save waited 7.6 s. A cover that cannot be decoded (an SVG
    # cover-image, a truncated first page) is NO cover, not a failed import:
    # it used to raise inside the transaction, which committed the item
    # half-written, published nothing, and never looked at the file again.
    thumbnail = None
    if parsed.cover_bytes:
        try:
            thumbnail = await encode_cover_thumbnail(parsed.cover_bytes)
        except Exception as exc:
            logger.warning("Imported %s without a cover: %s", file_path, exc)

    # A file that is an item already, renamed or moved (see _moved_item).
    moved = await _moved_item(library_id, root, file_hash, db_path) if existing is None else None

    async with write_transaction() as db:
        try:
            # Re-checked here, now that the write lock is actually held --
            # the plain read above happened before it, so in principle
            # another writer could have processed this same file in
            # between (e.g. the watcher and a manual rescan racing on the
            # same file). Cheap, and makes the INSERT below safe against
            # that race instead of hitting file_path's UNIQUE constraint.
            cursor = await db.execute("SELECT * FROM items WHERE file_path = ?", (db_path,))
            existing = await cursor.fetchone()
            if existing is not None and existing["file_hash"] == file_hash:
                return True
            moved_from_series = None
            if existing is None and moved is not None:
                # Still where it was found, now that the lock is held.
                existing = await (await db.execute(
                    "SELECT * FROM items WHERE id = ? AND file_path = ?",
                    (moved["id"], moved["file_path"]))).fetchone()
                if existing is not None:
                    moved_from_series = existing["series_id"]

            series_id, series_appeared = await _get_or_create_series(
                db, library_id, root, file_path,
                adopt_series_id=moved_from_series if moved and moved["series_gone"] else None)
            # From the FILENAME, not the title: sort_title breaks ties
            # between items with the same number (and orders the ones with
            # none), and an EPUB's title is its dc:title, which varies from
            # one release to the next in ways nobody sees -- two parts of a
            # volume titled alike on screen came out Part 2 before Part 1.
            # The filename is what the user sees in the folder and can
            # rename. backfill_sort_title brings existing rows in line.
            sort_title = natural_sort_key(file_path.stem)
            # Seeded from this file alone, then refined by
            # _renumber_series() below once the whole series can be
            # compared. Both the title and the FILENAME are offered: an
            # EPUB's title is its dc:title metadata, which very often names
            # only the work and not the volume -- the volume is in the
            # filename, which used to be discarded whenever dc:title
            # existed. (A CBZ's title already IS its filename.)
            number_in_series = extract_number_in_series(parsed.title, file_path.stem)

            if existing is not None:
                await db.execute(
                    """
                    UPDATE items SET file_path=?, title=?, sort_title=?, file_hash=?, file_size=?,
                        file_mtime_ns=?, text_size=?, page_count=?, number_in_series=?, metadata=?,
                        metadata_version=?, series_id=?, updated_at=datetime('now')
                    WHERE id = ?
                    """,
                    (
                        db_path,
                        parsed.title,
                        sort_title,
                        file_hash,
                        stat.st_size,
                        stat.st_mtime_ns,
                        parsed.text_size,
                        parsed.page_count,
                        number_in_series,
                        json.dumps(parsed.metadata),
                        epub_parser.METADATA_VERSION,
                        series_id,
                        existing["id"],
                    ),
                )
                item_id = existing["id"]
                content_changed = existing["file_hash"] != file_hash
                # The file behind this item is a different file now (the
                # hash was compared above), so every page's width and
                # height was measured from something that is gone.
                # page_dimensions is a CACHE and the endpoint that reads it
                # only recomputes when the ROW COUNT disagrees with
                # page_count -- so a re-scan or a re-encode that keeps the
                # same number of pages, which is the ordinary kind, left
                # the old sizes in place and served them forever.
                #
                # What that looks like: every placeholder in continuous
                # scroll reserved at the wrong aspect ratio -- the one
                # thing the cache exists to prevent -- and the double-page
                # walk pairing from dimensions that are not the file's.
                # Reproduced by swapping a 1600x2400 CBZ for an 800x1200
                # one with the same 200 pages: /dimensions kept answering
                # 1600x2400 while the pages themselves came back 800x1200.
                #
                # Dropped rather than recomputed: /dimensions rebuilds it
                # on the next open, which is the same thing it does for a
                # comic that never had one.
                if content_changed:
                    await db.execute("DELETE FROM page_dimensions WHERE item_id = ?", (item_id,))
                is_new_item = False
            else:
                await db.execute(
                    """
                    INSERT INTO items
                        (library_id, series_id, title, sort_title, file_path, file_format,
                         file_hash, file_size, file_mtime_ns, text_size, page_count,
                         number_in_series, metadata, metadata_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        library_id,
                        series_id,
                        parsed.title,
                        sort_title,
                        db_path,
                        file_format,
                        file_hash,
                        stat.st_size,
                        stat.st_mtime_ns,
                        parsed.text_size,
                        parsed.page_count,
                        number_in_series,
                        json.dumps(parsed.metadata),
                        epub_parser.METADATA_VERSION,
                    ),
                )
                cursor = await db.execute("SELECT id FROM items WHERE file_path = ?", (db_path,))
                item_id = (await cursor.fetchone())["id"]
                is_new_item = True

            # The name is content-addressed (see write_cover_thumbnail), so a
            # changed file's cover is a NEW file and the previous one is
            # removed once nothing names it. A new version with no cover
            # at all clears it: it used to keep the old file's thumbnail,
            # on the item and on its series, for good.
            previous_cover = existing["cover_path"] if existing is not None else None
            cover_relative = previous_cover if moved_from_series is not None or (
                existing is not None and not content_changed) else None
            if thumbnail is not None and cover_relative is None:
                cover_relative = await write_cover_thumbnail(thumbnail, item_id, file_hash)
            if previous_cover and previous_cover != cover_relative:
                try:
                    (settings.covers_dir / previous_cover).unlink(missing_ok=True)
                except OSError:
                    pass  # already gone, or no permission -- not worth failing the import over
            await db.execute("UPDATE items SET cover_path = ? WHERE id = ?", (cover_relative, item_id))

            if series_id is not None:
                # Numbers first: the cover is the first item in ORDER,
                # and the order is by number.
                if renumber_later is None:
                    await _renumber_series(db, series_id)
                await _recompute_series_cover(db, series_id)
                await _touch_series(db, series_id)
            left_series_removed = False
            if moved_from_series is not None and moved_from_series != series_id:
                # Moved to another folder: the series it came from may be
                # empty now, exactly as if the item had been deleted.
                left_series_removed = await _settle_series_after_leaving(
                    db, moved_from_series, renumber=renumber_later is None)

            if had_errors:
                await _clear_errors_for(db, db_path)

            await db.commit()
            if renumber_later is not None:
                renumber_later.update(
                    sid for sid in (series_id, moved_from_series) if sid is not None)

            # published only AFTER the commit -- a client that reacts to
            # the event by re-querying the API will already find the data
            # there
            if moved_from_series is not None and moved_from_series != series_id:
                # Off one series' grid and onto another's.
                events.publish({"type": "item_removed", "item_id": item_id, "series_id": moved_from_series})
                if left_series_removed:
                    events.publish({"type": "series_removed", "series_id": moved_from_series})
                if series_appeared and series_id is not None:
                    events.publish({"type": "series_added", "series_id": series_id})
                events.publish({"type": "item_added", "item_id": item_id, "series_id": series_id})
            elif is_new_item:
                if series_appeared and series_id is not None:
                    events.publish({"type": "series_added", "series_id": series_id})
                events.publish({"type": "item_added", "item_id": item_id, "series_id": series_id})
            else:
                # A file that changed on disk was silently invisible to any
                # open screen: only new items published anything, so a
                # volume whose cover or title had just been rewritten kept
                # showing the old one until a reload. Separate event type
                # from item_added because the two mean different things to
                # the client -- nothing was ADDED here, so there is nothing
                # to announce when the affected series isn't even on
                # screen; it is purely "redraw this if you are showing it".
                events.publish({"type": "item_updated", "item_id": item_id, "series_id": series_id})

        except Exception as exc:  # never lets 1 broken file halt the whole scan
            logger.exception("Failed to process %s", file_path)
            # Nothing of the import is kept: committing here used to save
            # whatever had been written before the failure -- an item row
            # with no cover and no event, which the next scan then took for
            # an unchanged file and never parsed again.
            await db.rollback()
            await _replace_error(db, library_id, str(file_path), str(exc))
            await db.commit()

            return False
    return True


async def _series_holds_reader_state(db, series_id: int) -> bool:
    """Whether anything about this series would be LOST by deleting the row.

    A series with no items left used to be deleted outright, because an
    empty one would otherwise sit on the home page forever. That is still
    true of an empty series, but deleting the ROW is a bigger hammer than
    the problem: `series_status`, `tracker_links` and `text_replacements`
    all cascade off it, so reading a series to the end and clearing the
    files took the tracker link with them -- and with it
    `last_pushed_chapter`/`last_pushed_volume`, so the next push starts
    asking the service from scratch. Everything edited on the series page
    (the name, an uploaded cover, the description, the blur, the
    publication status, a manual order) went the same way.

    What makes keeping the row worthwhile is that it can be FOUND again:
    `UNIQUE(library_id, folder_path)` is what `_get_or_create_series`
    looks a series up by, so one chapter landing back in the same folder
    reunites it with everything below. What makes it safe is that an empty
    series is filtered out of the grids (see `_series_with_items` in
    library/router.py), so nothing hangs around on screen.

    So: keep it when somebody invested something in it, and delete it when
    it holds nothing but what the scanner itself created -- the same
    "unknown is not a no" rule the reconciliation pass already follows,
    applied to the reader's own work instead of to the filesystem.
    """
    row = await (
        await db.execute(
            """
            SELECT name, folder_path, summary, cover_overridden, blur_cover,
                   manual_sort, publication_status,
                   EXISTS(SELECT 1 FROM series_status WHERE series_id = series.id) AS has_status,
                   EXISTS(SELECT 1 FROM tracker_links WHERE series_id = series.id) AS has_link,
                   EXISTS(SELECT 1 FROM text_replacements WHERE series_id = series.id) AS has_rules
              FROM series WHERE id = ?
            """,
            (series_id,),
        )
    ).fetchone()
    if row is None:
        return False
    # The name has no "overridden" flag and needs none: nothing rewrites it
    # after creation, and the automatic value is the folder's own last
    # component -- so a name that no longer matches it was typed by hand.
    renamed = row["name"] != row["folder_path"].rstrip("/").rsplit("/", 1)[-1]
    return bool(
        row["has_status"] or row["has_link"] or row["has_rules"]
        or row["summary"] or row["cover_overridden"] or row["blur_cover"]
        or row["manual_sort"] or row["publication_status"] != "ongoing"
        or renamed
    )


async def _remember_removed_item(db, item_id: int) -> None:
    """Keeps what was read of an item that is about to go -- see Removed
    item in GLOSSARY.md and removed_items in schema.sql.

    Only for readers who spent time on it, one row each: an item nobody
    read leaves nothing behind. Asked per user rather than grouped over
    page_reads, because (user_id, item_id) is the index that table has --
    grouping by item alone would read every span ever recorded, on every
    deletion, and a scan can delete hundreds.
    """
    users = [r["id"] for r in await (await db.execute("SELECT id FROM users")).fetchall()]
    reads = []
    for user_id in users:
        spent = await (await db.execute(
            "SELECT SUM(MIN(seconds, cap_seconds)) AS seconds, COUNT(*) AS pages,"
            "       MAX(started_at) AS last_at"
            "  FROM page_reads WHERE user_id = ? AND item_id = ?",
            (user_id, item_id),
        )).fetchone()
        if spent["pages"]:
            reads.append((user_id, spent))
    if not reads:
        return
    cursor = await db.execute(
        """
        INSERT INTO removed_items (library_id, folder_path, series_name, title, file_format,
                                   text_size, page_count)
        SELECT items.library_id, series.folder_path, series.name, items.title, items.file_format,
               items.text_size, items.page_count
          FROM items
     LEFT JOIN series ON series.id = items.series_id
         WHERE items.id = ?
        """,
        (item_id,),
    )
    removed_id = cursor.lastrowid
    for user_id, spent in reads:
        await db.execute(
            """
            INSERT INTO removed_item_reads (user_id, removed_item_id, seconds, pages, last_at,
                                            status, progress_percent, completed_from_percent)
            SELECT ?, ?, ?, ?, ?, reading_progress.status, reading_progress.progress_percent,
                   reading_progress.completed_from_percent
              FROM (SELECT 1)
         LEFT JOIN reading_progress
                ON reading_progress.user_id = ? AND reading_progress.item_id = ?
            """,
            (user_id, removed_id, spent["seconds"] or 0, spent["pages"], spent["last_at"],
             user_id, item_id),
        )


async def _delete_item(db, item_id: int, *, recompute: bool = True,
                       expected_path: str | None = None) -> int | None:
    """Permanently removes an item: reading progress disappears along with
    it via ON DELETE CASCADE, after what was read of it is kept as a
    removed item (_remember_removed_item); the cover thumbnail on
    disk is deleted separately (not managed by SQLite). If the series ends
    up with no items left after this, the series is removed too --
    without this, an empty series would keep showing up on the home
    page.

    `recompute=False` leaves the series' cover, numbering and modified
    time to the caller, which is the reconciliation: it removes a whole
    folder's items in one pass and then revisits every series once.
    Recomputed per item, emptying a 1000-item folder held the write lock
    for 16.1 s (quadratic: each removal renumbered what was left), against
    1.5 s. Returns the series the item was in, when that series is still
    on the shelf."""
    cursor = await db.execute("SELECT series_id, cover_path, file_path FROM items WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    if row is None or (expected_path is not None and row["file_path"] != expected_path):
        return None
    series_id = row["series_id"]
    cover_path = row["cover_path"]

    await _remember_removed_item(db, item_id)
    await db.execute("DELETE FROM items WHERE id = ?", (item_id,))

    if cover_path:
        try:
            (settings.covers_dir / cover_path).unlink(missing_ok=True)
        except OSError:
            pass  # thumbnail already gone or no permission -- not a reason to fail deleting the item

    series_removed = await _settle_series_after_leaving(db, series_id, recompute=recompute)

    # Published here (before the caller's commit) instead of waiting -- for
    # this app's scale/usage (self-hosted, no real concurrency) the
    # difference is irrelevant in practice, and it avoids duplicating
    # "publish after commit" in each of the several places that call
    # _delete_item (watcher, full scan, manual dismissal in
    # /library/issues).
    events.publish({"type": "item_removed", "item_id": item_id, "series_id": series_id})
    if series_removed:
        events.publish({"type": "series_removed", "series_id": series_id})
    return None if series_removed else series_id


async def _settle_series_after_leaving(db, series_id: int | None, *, recompute: bool = True,
                                      renumber: bool = True) -> bool:
    """What a series becomes once an item has left it -- deleted, or moved
    to another folder. True when it is off the shelf now. `renumber=False`:
    the caller renumbers it later (a scan, see process_file)."""
    if series_id is None:
        return False
    cursor = await db.execute("SELECT COUNT(*) AS n FROM items WHERE series_id = ?", (series_id,))
    remaining = (await cursor.fetchone())["n"]
    if remaining == 0:
        # Off the grids either way (they list only series that have
        # items), so the caller publishes series_removed whatever happens
        # to the row -- what differs is whether the row, and everything
        # hanging off it, is still there when a chapter lands back in that
        # folder.
        if await _series_holds_reader_state(db, series_id):
            # Kept: the automatic cover was the departing item's and is
            # gone with it, so clear the pointer rather than leave the
            # series naming a thumbnail that no longer exists. An
            # uploaded one is the series' own file and stays.
            await _recompute_series_cover(db, series_id)
        else:
            # A hand-uploaded cover is the series' own file, so nothing
            # else will ever clean it up -- _delete_item only unlinks the
            # departing ITEM's thumbnail.
            series_cover = await (
                await db.execute("SELECT cover_path FROM series WHERE id = ?", (series_id,))
            ).fetchone()
            await db.execute("DELETE FROM series WHERE id = ?", (series_id,))
            if series_cover:
                delete_custom_series_cover(series_cover["cover_path"])
        return True
    if recompute:
        # Numbering is a property of the series as a whole (see
        # resolve_series_numbers), so removing an item can change what
        # the remaining ones should be numbered by -- e.g. the only
        # file that carried a chapter marker just left.
        if renumber:
            await _renumber_series(db, series_id)
        # The series cover may have been exactly the departed item's --
        # recomputed from what's left, after the numbers it is ordered by.
        await _recompute_series_cover(db, series_id)
        await _touch_series(db, series_id)
    return False


async def _renumber_series(db, series_id: int) -> bool:
    """Recomputes number_in_series for every item of one series at once.

    This is a series-level decision, not a per-file one, which is the whole
    reason it lives here instead of in the parsers. A name like
    "[Group] Series - c012 (v02)" carries two numbers, and which of them
    orders the collection depends on something no single file knows:
    whether this library stores one file per chapter or one per volume.
    Both exist. resolve_series_numbers() answers that from the series as a
    whole -- see its docstring for how, and tests/test_naturalsort.py for
    the cases it has to get right.

    Called from the same places as _recompute_series_cover(), for the same
    reason: both answers depend on which items the series currently holds.

    Only rows whose value actually changes are written, so re-running this
    on an unchanged series costs one SELECT and nothing else. Returns
    whether any item's number changed."""
    cursor = await db.execute(
        "SELECT id, title, file_path, number_in_series FROM items WHERE series_id = ?",
        (series_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return False

    candidates = [
        (row["id"], extract_candidates(row["title"], Path(row["file_path"]).stem)) for row in rows
    ]
    numbering = resolve_series_numbers(candidates)
    changed = False
    for row in rows:
        number = numbering.numbers.get(row["id"])
        if number != row["number_in_series"]:
            changed = True
            await db.execute(
                "UPDATE items SET number_in_series = ? WHERE id = ?", (number, row["id"])
            )
    # Which kind those numbers came from is a property of the SERIES, and
    # this is the only place that knows it. Nothing but a tracker reads it
    # (see app/tracking/), but it has to be written here or the answer is
    # gone -- recomputing it later would mean re-deriving candidates from
    # every item's title and path again.
    await db.execute(
        "UPDATE series SET number_kind = ? WHERE id = ? AND number_kind IS NOT ?",
        (numbering.kind, series_id, numbering.kind),
    )
    return changed


async def backfill_number_in_series(report_progress: bool = False) -> int:
    """Recomputes number_in_series for the WHOLE library, series by series,
    from the title and file path already saved in the database -- doesn't
    reopen or reparse any file, so it's fast even on a large library.

    Exists because process_file() only reprocesses a file when its hash
    changes -- so when the numbering gains new behavior (roman numerals,
    chapter markers, reading the filename when an EPUB's dc:title doesn't
    name the volume), already-imported items keep their old value until
    they're touched again for some other reason. Running "Rescan" alone
    doesn't fix that -- this does.

    Standalone items (no series) are numbered individually: there is no
    series to compare them against, so the best a single name can offer is
    all there is.

    `report_progress` is off by default because the run that matters for it
    is the one a user just asked for. This also runs on every start (see
    main.py), where nobody is watching -- and job_status deliberately KEEPS
    a finished job so a page opening right after one can still say it
    happened, so reporting the startup run would leave a "Finished"
    progress bar on the maintenance page of every single boot, for a job
    the user never triggered."""
    async with write_transaction() as db:
        cursor = await db.execute(
            "SELECT id, name FROM series WHERE id IN (SELECT DISTINCT series_id FROM items) "
            "ORDER BY sort_name"
        )
        series_rows = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT id, title, file_path, number_in_series FROM items WHERE series_id IS NULL"
        )
        standalone = await cursor.fetchall()

        # One unit per series, plus one for the whole standalone batch (it
        # is a single pass over items that belong to no series, so it can't
        # be reported series by series). Counted up front, like the scan's
        # file list, so the bar is a real "N of M" from the first event
        # instead of a spinner that cannot say how far along it is.
        total_units = len(series_rows) + (1 if standalone else 0)
        if report_progress:
            job_status.start(job_status.RENUMBER_KEY, "renumber", "series", total_units)
        try:
            for row in series_rows:
                if report_progress:
                    job_status.advance(job_status.RENUMBER_KEY, row["name"])
                await _renumber_series(db, row["id"])

            if standalone:
                if report_progress:
                    job_status.advance(job_status.RENUMBER_KEY, "Items with no series")
                for row in standalone:
                    number = extract_number_in_series(row["title"], Path(row["file_path"]).stem)
                    if number != row["number_in_series"]:
                        await db.execute(
                            "UPDATE items SET number_in_series = ? WHERE id = ?", (number, row["id"])
                        )
        finally:
            # In a finally for the same reason the scan's is: a run that
            # dies part-way must stop claiming to be running, rather than
            # leaving the bar frozen until the process restarts.
            if report_progress:
                job_status.finish(job_status.RENUMBER_KEY)

        cursor = await db.execute("SELECT COUNT(*) AS n FROM items")
        total = (await cursor.fetchone())["n"]
        await db.commit()
        return total


async def mark_missing(library_id: int, file_path: str) -> None:
    """Permanently removes an item as soon as the watcher detects that its
    file disappeared from disk -- without waiting for manual confirmation or
    the next full scan (which does the same thing, see the reconciliation
    in scan_library()).

    Matching is by exact file_path, which is always the real file inside
    the watched folder -- so every deletion the watcher sees matches. (This
    used to have a gap: an item imported through the old CBR->CBZ
    conversion was recorded against the converted copy outside the watched
    folder, so deleting the original never matched here. That conversion is
    gone -- see _check_readable_archive.)
    """
    async with write_transaction() as db:
        cursor = await db.execute("SELECT id FROM items WHERE file_path = ?", (file_path,))
        row = await cursor.fetchone()
        if row is None:
            return  # not a known item -- nothing to do
        await _delete_item(db, row["id"])
        await db.commit()


async def mark_folder_missing(library_id: int, folder: str) -> None:
    """mark_missing for a whole folder, when the watcher sees the FOLDER go.

    Moving a series folder out of the library (or to the desktop's trash)
    is reported by inotify as the folder deleted, with no event for any
    file in it -- so its cards stayed on the home grid, pointing at files
    that were gone, until the nightly scan. Only items confidently gone
    are removed ("unknown is not a no"); each series is settled once.
    """
    prefix = folder.rstrip(os.sep) + os.sep
    async with read_connection() as rdb:
        rows = await (await rdb.execute(
            "SELECT id, file_path FROM items WHERE library_id = ? AND substr(file_path, 1, ?) = ?",
            (library_id, len(prefix), prefix))).fetchall()
    gone = set(await asyncio.to_thread(_missing, [row["file_path"] for row in rows]))
    if not gone:
        return
    async with write_transaction() as db:
        touched: set[int] = set()
        gone = set(await asyncio.to_thread(_missing, list(gone)))
        for row in rows:
            if row["file_path"] not in gone:
                continue
            left = await _delete_item(db, row["id"], recompute=False,
                                      expected_path=row["file_path"])
            if left is not None:
                touched.add(left)
        for series_id in touched:
            await _renumber_series(db, series_id)
            await _recompute_series_cover(db, series_id)
            await _touch_series(db, series_id)
        await db.commit()


def _walk(root: Path) -> tuple[list[Path], list[OSError]]:
    """Every importable file under `root`, and every folder that could not
    be listed. Blocking -- a stat per entry, which on a network share or a
    cold SD card is seconds for a library; run in a thread.

    `os.walk(onerror=)` rather than `Path.rglob()`: rglob swallows a folder
    it may not enter (owner/group differ and no execute bit for others,
    common when another tool made it), so the folder silently did not
    exist. Here each one becomes an error on /library/issues.
    `followlinks=True` is the same class of "whole folder silently
    missing"."""
    errors: list[OSError] = []
    found: list[Path] = []
    # Each real directory once. Following links means following a link
    # back up the tree too: one `up -> ..` turned a single file into 41
    # imports, until the path grew too long to open.
    visited: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, onerror=errors.append, followlinks=True):
        try:
            st = os.stat(dirpath)
        except OSError as exc:
            errors.append(exc)
            dirnames[:] = []
            continue
        if (st.st_dev, st.st_ino) in visited:
            dirnames[:] = []
            continue
        visited.add((st.st_dev, st.st_ino))
        dirnames[:] = [name for name in dirnames if not _ignored_folder(name)]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.is_file() and _is_eligible(path):
                found.append(path)
    return found, errors


def _missing(paths: list[str]) -> list[str]:
    """Those of `paths` confidently gone. "Could not tell" (see
    _path_exists) is not gone: a wrong answer here DELETES an item."""
    return [path for path in paths if _path_exists(Path(path)) is False]


def _folder_listable(folder: Path) -> bool:
    """Whether `folder` is a directory that can be listed right now."""
    try:
        with os.scandir(folder):
            return True
    except OSError:
        return False


def _empty_folders(folders: dict[int, Path]) -> tuple[list[int], list[int]]:
    """Which of these series folders hold no importable file, and which
    could not be listed at all -- the second is NOT the first, see
    _folder_has_eligible_file."""
    empty, unreadable = [], []
    for series_id, folder in folders.items():
        has_file = _folder_has_eligible_file(folder)
        if has_file is None:
            unreadable.append(series_id)
        elif not has_file:
            empty.append(series_id)
    return empty, unreadable


# Libraries being scanned now, each mapped to whether another scan was
# asked for meanwhile. A second Rescan -- or the scheduled scan landing on
# a manual one -- used to run a second full pass beside the first, and the
# first to finish cleared the progress the other was still reporting.
_scanning: dict[int, bool] = {}


async def scan_library(library_id: int, root_path: str) -> dict:
    """Complete, idempotent scan. Permanently removes items whose file
    disappeared from disk (same as mark_missing(), for when the deletion
    happened while the watcher wasn't running -- e.g. the container was
    stopped at the time).

    One at a time per library. Asked for while one runs, it runs once more
    after it rather than beside it: whatever made someone ask may have
    changed files the running pass had already walked past."""
    if library_id in _scanning:
        _scanning[library_id] = True
        return {"library_id": library_id, "queued": True}
    _scanning[library_id] = False
    try:
        while True:
            result = await _scan_once(library_id, root_path)
            if not _scanning[library_id]:
                return result
            _scanning[library_id] = False
    finally:
        del _scanning[library_id]


async def _plainly_unchanged(library_id: int, paths: list[Path]) -> set[str]:
    """The paths a scan need not look at again -- see _scan_once."""
    async with read_connection() as rdb:
        known = {
            row["file_path"]: row
            for row in await (await rdb.execute(
                "SELECT file_path, file_size, file_mtime_ns, cover_path FROM items WHERE library_id = ?",
                (library_id,))).fetchall()
        }
        erroring = {
            row["file_path"]
            for row in await (await rdb.execute(
                "SELECT file_path FROM scan_events WHERE library_id = ? AND event_type = 'error'",
                (library_id,))).fetchall()
        }

    def decide() -> set[str]:
        same = set()
        for path in paths:
            key = str(path)
            row = known.get(key)
            if row is None or key in erroring:
                continue
            cover = row["cover_path"]
            if cover and not cover.lower().endswith(f".{THUMBNAIL_EXTENSION}"):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if row["file_size"] == st.st_size and row["file_mtime_ns"] == st.st_mtime_ns:
                same.add(key)
        return same

    return await asyncio.to_thread(decide)


async def _scan_once(library_id: int, root_path: str) -> dict:
    root = Path(root_path)
    seen_paths: set[str] = set()

    # Collected first, then processed: the walk is the cheap half, and
    # knowing the file count before the expensive half starts is what lets
    # the progress below be a real "N of M".
    eligible_paths, permission_errors = await asyncio.to_thread(_walk, root)

    # What is plainly unchanged is decided here for the whole library at
    # once: same size and modification time as recorded, no error on file,
    # and a cover already in the current format (an older one is converted
    # by process_file's unchanged path, see _record_unchanged). Asked file
    # by file, that was a pooled connection, two queries and a thread hop
    # each -- 7.9 s for 8,500 files that had not changed; one query and one
    # pass of stats decided 2,000 in 13 ms.
    unchanged = await _plainly_unchanged(library_id, eligible_paths)

    scan = job_status.scan_key(library_id)
    job_status.start(scan, "scan", "files", len(eligible_paths))
    renumber_later: set[int] = set()
    failed_import = False
    try:
        for path in eligible_paths:
            job_status.advance(scan, path.name)
            if str(path) not in unchanged:
                if not await process_file(library_id, root_path, path, renumber_later):
                    failed_import = True
            seen_paths.add(str(path))
    finally:
        # In a finally so a scan that dies part-way stops claiming to be
        # running -- otherwise the UI would show a scan stuck at whatever
        # file it failed on until the process restarts.
        job_status.finish(scan)
    await _settle_series(renumber_later)
    # A failed destination might be a moved item whose old path is gone.
    # Until it can be identified, pruning missing rows could discard its
    # reading state. Reconcile on the next successful scan instead.
    if failed_import:
        return {"library_id": library_id, "files_seen": len(seen_paths), "skipped": True}

    # Nothing below may run when the library itself could not be seen.
    # Reconciliation deletes every item it does not find, and a library
    # whose root is missing (a share or USB drive not mounted at 05:00) or
    # empty (a mount point with nothing mounted on it) finds NONE: every
    # file reads as confidently gone, and the whole library -- items,
    # reading progress -- went with one scheduled scan. "Unknown is not a
    # no" (AGENTS.md): an absent or empty root is far likelier to be an
    # unmounted disk than a library someone emptied by hand, and the cost
    # of being wrong the other way is one manual fix, not everything read.
    # Recorded under the ROOT's path, and cleared by the next scan that
    # does see it -- the gone_errors pass below would otherwise delete it
    # in the same breath, since the root is exactly what does not exist.
    root_listable = await asyncio.to_thread(_folder_listable, root)
    if not root_listable or not eligible_paths:
        async with read_connection() as rdb:
            has_items = (await (await rdb.execute(
                "SELECT EXISTS(SELECT 1 FROM items WHERE library_id = ?) AS n", (library_id,)
            )).fetchone())["n"]
        if not root_listable or has_items:
            async with write_transaction() as db:
                await _replace_error(
                    db, library_id, str(root),
                    ("The library folder could not be listed" if not root_listable
                     else "The library folder is empty")
                    + " -- is the disk or share mounted? Nothing was removed; "
                    "the library is reconciled again by the next scan that finds its files.",
                )
                await db.commit()
            return {"library_id": library_id, "files_seen": len(seen_paths), "skipped": True}
    async with write_transaction() as db:
        await _clear_errors_for(db, str(root))
        await db.commit()

    # Reconciliation. What is on disk is asked in threads, BEFORE the write
    # lock: a stat per item and a listing per series folder used to run on
    # the event loop inside the lock, freezing the app for the length of
    # the pass (237 ms for 20,000 files on a warm local disk; seconds on a
    # share). Anything that looks deletable is asked again inside the lock,
    # because a file can arrive in between and deleting its series would
    # take the new item with it.
    async with read_connection() as rdb:
        items = await (await rdb.execute(
            "SELECT id, file_path FROM items WHERE library_id = ?", (library_id,))).fetchall()
        # An error about a file that is no longer there has nothing left to
        # say, and a file the user DELETED is never reprocessed to clear it.
        errors = await (await rdb.execute(
            "SELECT id, file_path FROM scan_events WHERE library_id = ? AND event_type = 'error'",
            (library_id,))).fetchall()
        series_rows = await (await rdb.execute(
            "SELECT id, folder_path FROM series WHERE library_id = ?", (library_id,))).fetchall()

    unseen = {row["file_path"]: row["id"] for row in items if row["file_path"] not in seen_paths}
    gone_items = await asyncio.to_thread(_missing, list(unseen))
    # Imported from a folder no library includes any more (a trash folder,
    # before those were skipped): there, but not part of the library.
    gone_items += [path for path in unseen
                   if path not in gone_items and _inside_ignored_folder(Path(path), root)]
    error_paths = {row["id"]: row["file_path"] for row in errors}
    gone_errors = set(await asyncio.to_thread(_missing, list(set(error_paths.values()))))
    folders = {row["id"]: root / row["folder_path"] for row in series_rows}
    empty, unreadable = await asyncio.to_thread(_empty_folders, folders)

    # Series an item left, whose modified time moves. Cover and numbering
    # are redone below for every series anyway, once, instead of once per
    # item removed (see _delete_item's `recompute`).
    touched: set[int] = set()
    async with write_transaction() as db:
        still_gone = set(await asyncio.to_thread(_missing, gone_items))
        for path in gone_items:
            if path not in still_gone and not _inside_ignored_folder(Path(path), root):
                continue  # arrived back in between
            left = await _delete_item(db, unseen[path], recompute=False, expected_path=path)
            if left is not None:
                touched.add(left)

        for error_id, path in error_paths.items():
            # A FOLDER's error (not a file's) is found again by every scan
            # that still cannot list it, so every one is replaced by what
            # this scan found: they used to be appended -- two more rows a
            # scan, kept after the permissions were fixed (measured: 6 rows
            # after three scans, still 6 after the fix and a rescan).
            folder_error = not _is_eligible(Path(path)) and path != str(root)
            if path in gone_errors or folder_error:
                await db.execute("DELETE FROM scan_events WHERE id = ?", (error_id,))

        folder_problems = {
            err.filename or str(root):
                f"Could not access this folder: {err.strerror or err}. "
                "Check its owner/permissions (needs execute permission for whoever runs the container)."
            for err in permission_errors
        }
        for series_id in unreadable:
            # Could not even LIST the folder. Left exactly as it is -- as
            # "no file" it would delete an intact series -- and surfaced.
            folder_problems.setdefault(
                str(folders[series_id]),
                "Could not list this series folder while reconciling the library; "
                "left untouched. Check its owner/permissions (needs execute "
                "permission for whoever runs the container).")
        for path, detail in folder_problems.items():
            await _log_error(db, library_id, path, detail)

        still_empty, _ = await asyncio.to_thread(
            _empty_folders, {series_id: folders[series_id] for series_id in empty})
        for series_id in still_empty:
            # Emptied on disk: its items go, and the series with them
            # unless the reader invested something in it -- the same
            # question _delete_item asks when a last item disappears.
            cursor = await db.execute("SELECT id FROM items WHERE series_id = ?", (series_id,))
            for item_row in await cursor.fetchall():
                await _delete_item(db, item_row["id"], recompute=False)
            if not await _series_holds_reader_state(db, series_id):
                await db.execute("DELETE FROM series WHERE id = ?", (series_id,))

        # The series an item left. Only those: every series used to be
        # redone here on every scan, changed or not -- 400 series held the
        # write lock 651 ms with nothing to do. A series' cover and numbers
        # depend on its own items alone, and every path that changes those
        # settles it; a change in how numbers are worked out reaches the
        # rest through backfill_number_in_series at startup.
        # Read again: removing a last item can remove its series too.
        remaining = {row["id"] for row in await (await db.execute(
            "SELECT id FROM series WHERE library_id = ?", (library_id,))).fetchall()}
        renumbered = []
        for series_id in sorted(touched & remaining - set(still_empty) - set(unreadable)):
            if await _renumber_series(db, series_id):
                renumbered.append(series_id)
            await _recompute_series_cover(db, series_id)
            await _touch_series(db, series_id)

        await db.commit()
    _announce_renumbered(renumbered)

    return {"library_id": library_id, "files_seen": len(seen_paths)}


async def _settle_series(series_ids: set[int]) -> None:
    """Numbers, then cover, for each series a scan's imports deferred (see
    process_file's `renumber_later`): once per series, each in its own
    short write transaction, so a progress save never waits behind them
    all."""
    renumbered = []
    for series_id in sorted(series_ids):
        async with write_transaction() as db:
            if await _renumber_series(db, series_id):
                renumbered.append(series_id)
            await _recompute_series_cover(db, series_id)
            await db.commit()
    _announce_renumbered(renumbered)


def _announce_renumbered(series_ids) -> None:
    # Numbers decide the order of an open series' grid, which item_updated
    # refreshes (and the home card with it).
    for series_id in series_ids:
        events.publish({"type": "item_updated", "series_id": series_id})


async def backfill_text_size() -> int:
    """Fills in items.text_size for EPUBs imported before that column
    existed; returns how many were measured.

    Necessary for the same reason as backfill_number_in_series above:
    process_file() returns early for a file whose hash has not changed, so
    a "Rescan" never revisits an item that is simply sitting there. Without
    this, every book already in the library would keep no text size, and
    the reading-time estimate would have nothing to scale by for exactly
    the items the user already owns.

    Unlike that backfill, this one has to OPEN each file -- but only the
    zip's central directory, never the content -- so it is bounded by the
    number of EPUBs missing the value, which is the whole library once and
    nothing on every start after that.

    A file that cannot be read is left NULL and skipped rather than failing
    the start: it will be picked up whenever it next changes, and until
    then that one book simply has no estimate.
    """
    measured = 0
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, file_path FROM items "
            "WHERE file_format = 'epub' AND text_size IS NULL"
        )
        pending = await cursor.fetchall()
    finally:
        await db.close()
    if not pending:
        return 0

    sizes = []
    for row in pending:
        try:
            sizes.append((await asyncio.to_thread(epub_parser.measure_text_size, row["file_path"]), row["id"]))
        except Exception:
            logger.warning("Could not measure text size for item %s", row["id"])
    if not sizes:
        return 0

    async with write_transaction() as wdb:
        await wdb.executemany("UPDATE items SET text_size = ? WHERE id = ?", sizes)
        await wdb.commit()
        measured = len(sizes)
    logger.info("Measured text size for %s book(s)", measured)
    return measured


async def backfill_metadata() -> int:
    """Re-reads the embedded metadata of every item whose `metadata` was
    produced by older rules (`metadata_version` NULL or below
    epub.METADATA_VERSION); returns how many were rewritten.

    Necessary for the same reason as backfill_text_size above:
    process_file() never parses a file again while its contents stay the
    same, so a change to what the parsers read -- every author instead of
    the first dc:creator, a description with its HTML taken out -- would
    otherwise reach only the books added after it.

    Opens each pending file for its OPF or ComicInfo.xml alone. The whole
    library once per bump of METADATA_VERSION, nothing on every start after.

    A file that cannot be read keeps what it had and stays pending: it is
    tried again on the next start, and re-read anyway whenever it changes.
    """
    db = await get_connection()
    try:
        cursor = await db.execute(
            "SELECT id, file_path, file_format FROM items "
            "WHERE (metadata_version IS NULL OR metadata_version < ?)",
            (epub_parser.METADATA_VERSION,),
        )
        pending = await cursor.fetchall()
    finally:
        await db.close()
    if not pending:
        return 0

    rows = []
    for row in pending:
        read = epub_parser.read_metadata if row["file_format"] == "epub" else cbz_parser.read_metadata
        try:
            metadata = await asyncio.to_thread(read, row["file_path"])
        except Exception:
            logger.warning("Could not read metadata for item %s", row["id"])
            continue
        rows.append((json.dumps(metadata), epub_parser.METADATA_VERSION, row["id"]))
    if not rows:
        return 0

    async with write_transaction() as wdb:
        await wdb.executemany("UPDATE items SET metadata = ?, metadata_version = ? WHERE id = ?", rows)
        await wdb.commit()
    logger.info("Re-read metadata for %s item(s)", len(rows))
    return len(rows)


async def backfill_sort_title() -> int:
    """Recomputes items.sort_title from each item's filename; returns how
    many rows changed.

    sort_title used to be the natural sort key of the TITLE (an EPUB's
    dc:title), and process_file never revisits an unchanged file, so the
    rows imported before it became the filename's keep the old key -- and
    an old key compared against a new one orders by nothing meaningful.
    No file is opened: the path is in the row. Writes only the rows whose
    value differs, so every start after the first writes nothing.
    """
    async with write_transaction() as db:
        rows = await (await db.execute("SELECT id, file_path, sort_title FROM items")).fetchall()
        changed = [
            (key, row["id"]) for row in rows
            if (key := natural_sort_key(Path(row["file_path"]).stem)) != row["sort_title"]
        ]
        if changed:
            await db.executemany("UPDATE items SET sort_title = ? WHERE id = ?", changed)
            await db.commit()
    if changed:
        logger.info("Re-keyed the sort order of %s item(s) by filename", len(changed))
    return len(changed)
