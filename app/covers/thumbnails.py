"""Cover thumbnail generation. Runs on a separate thread (Pillow is synchronous/CPU-bound)."""

import asyncio
import hashlib
import io
import logging

from PIL import Image

from app.config import settings

logger = logging.getLogger("covers")

THUMBNAIL_MAX_SIZE = (400, 600)

# WebP rather than JPEG. Measured through this exact function (source
# downscaled to THUMBNAIL_MAX_SIZE, then encoded) over three kinds of cover
# art, against the JPEG q85 this used to write:
#
#     photographic    43.7 KB -> 25.5 KB  (-42%)
#     illustration    60.0 KB -> 36.6 KB  (-39%)
#     line art / scan 104.3 KB -> 70.2 KB (-33%)
#
# The cost is encode time (~1-12ms -> ~30-100ms per cover), which is paid on
# a worker thread during a scan and only for a file that is new or changed.
# AVIF was measured too and compresses a further ~20 points, but it costs 3x
# the encode time and roughly twice the DECODE time -- on a grid of 60 covers
# that lands on the phone rendering the page, which is the worse place to
# spend it for an image this small. WebP is also supported by every browser
# from 2020 on; AVIF only from Safari 16.4.
#
# Existing `.jpg` thumbnails keep serving as they are: the path is stored per
# item in the database, so a mixed directory is normal, and each one becomes
# WebP whenever that file is next processed.
THUMBNAIL_FORMAT = "WEBP"
THUMBNAIL_EXTENSION = "webp"
THUMBNAIL_QUALITY = 80


def _encode_thumbnail_sync(cover_bytes: bytes) -> bytes:
    """Downscale + encode, with no opinion about where the result goes.

    A comic/ebook cover is written to a file under `covers_dir`; a web
    novel's is stored as a BLOB in its own row (it comes from a scraped
    page, not from a file this app owns). Same pipeline either way, so it
    lives here once.
    """
    image = Image.open(io.BytesIO(cover_bytes))
    image = image.convert("RGB")
    image.thumbnail(THUMBNAIL_MAX_SIZE)
    buffer = io.BytesIO()
    image.save(buffer, format=THUMBNAIL_FORMAT, quality=THUMBNAIL_QUALITY, method=4)
    return buffer.getvalue()


async def encode_cover_thumbnail(cover_bytes: bytes) -> bytes:
    """The cover thumbnail, encoded -- written to disk for an item (see
    write_cover_thumbnail), stored in its row for a web novel.

    The web-novel cover used to be kept at whatever size and format the
    source site served -- a full-size JPEG or PNG in a BLOB, read back on
    every cover request -- while comic and ebook covers had been
    downscaled and encoded for years.
    """
    return await asyncio.to_thread(_encode_thumbnail_sync, cover_bytes)


async def write_cover_thumbnail(encoded: bytes, item_id: int, file_hash: str) -> str:
    """Writes an item's thumbnail, already encoded by encode_cover_thumbnail
    (the scanner encodes it BEFORE taking the write lock); returns the
    saved relative path.

    The name carries the source file's hash, not just the item id, so the
    URL changes whenever the cover's CONTENT does. A plain `{item_id}.jpg`
    was reused in two ways that both showed the browser a stale image it
    had already cached under that exact URL:

    - A different item entirely. SQLite reuses a freed rowid when the
      highest row is deleted (no AUTOINCREMENT here), so removing the last
      series and importing a new one hands the new item the departed one's
      id -- and its cover URL. Reproduced in a real browser: the new
      series' card painted the removed series' cover, and only a reload
      fixed it.
    - The same item after its file changed. Reprocessing rewrote
      `{id}.jpg` in place, so a volume whose cover had actually changed on
      disk kept showing the old one.

    Content in the name makes both impossible: a different cover is a
    different URL, and an unchanged one stays cacheable.
    """
    relative_path = f"{item_id}-{file_hash[:12]}.{THUMBNAIL_EXTENSION}"
    dest_path = settings.covers_dir / relative_path

    def write():
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(encoded)

    await asyncio.to_thread(write)
    return relative_path


# A hand-uploaded series cover is a thumbnail like any other -- same size,
# same format, same directory -- but it belongs to the SERIES rather than to
# any of its items, so it needs a name no item can ever produce. An item's is
# `{item_id}-{hash}`, i.e. always starts with a digit; this prefix therefore
# cannot collide, and it is also what tells the two apart later: only a file
# named this way may be deleted when the override is reset or the series goes
# away (deleting an item's thumbnail there would blank that item's cover).
CUSTOM_COVER_PREFIX = "series-"


def is_custom_series_cover(cover_path: str | None) -> bool:
    return bool(cover_path) and cover_path.startswith(CUSTOM_COVER_PREFIX)


async def save_custom_series_cover(cover_bytes: bytes, series_id: int) -> str:
    """Stores an uploaded image as this series' cover; returns its relative path.

    The name carries a hash of the ENCODED bytes for the same reason item
    thumbnails carry the source file's (see write_cover_thumbnail): /covers is
    revalidated privately, and a different URL makes replacements unambiguous.
    Hashing the output rather than the upload also means re-uploading the same
    picture reuses the same name instead of leaving an orphan behind.
    """
    encoded = await encode_cover_thumbnail(cover_bytes)
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    relative_path = f"{CUSTOM_COVER_PREFIX}{series_id}-{digest}.{THUMBNAIL_EXTENSION}"

    def _write() -> None:
        dest = settings.covers_dir / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(encoded)

    await asyncio.to_thread(_write)
    return relative_path


def delete_custom_series_cover(cover_path: str | None) -> None:
    """Removes an uploaded cover's file, if that is what the path names.

    Deliberately a no-op for anything else: the same column holds an item's
    thumbnail whenever the series is on automatic, and that file is owned by
    the item, not by the series.
    """
    if not is_custom_series_cover(cover_path):
        return
    try:
        (settings.covers_dir / cover_path).unlink(missing_ok=True)
    except OSError:
        # Already gone, or no permission -- an orphaned thumbnail is not a
        # reason to fail the reset/removal the caller is performing.
        logger.warning("Could not delete custom series cover %s", cover_path)


async def migrate_legacy_thumbnail(cover_path: str | None) -> str | None:
    """Re-encodes an already-generated `.jpg` thumbnail as WebP, returning the
    new relative path (or None when there is nothing to do).

    Without this the format change would only ever reach files that are new
    or that change on disk -- a library imported before it would keep every
    cover as JPEG forever, which is most of the benefit missed. It runs from
    the scanner's unchanged-file path, so a library converts itself over one
    scan and each cover is touched exactly once.

    It re-encodes the 400x600 thumbnail rather than re-deriving it from the
    source file: that is one small read instead of reopening (and hashing
    the cover out of) an archive that can be hundreds of MB. Measured on a
    real cover, the two produce the same size within 1% (13.6 KB vs 13.7 KB)
    -- the extra generation costs ~2.5 dB of PSNR against the original,
    which at this size is not something you can see. Re-deriving would be
    the better call for a full-size image; for a thumbnail it is not worth
    the I/O.
    """
    if not cover_path or cover_path.lower().endswith(f".{THUMBNAIL_EXTENSION}"):
        return None
    source = settings.covers_dir / cover_path
    destination = source.with_suffix(f".{THUMBNAIL_EXTENSION}")

    def _convert() -> bool:
        if not source.is_file():
            return False
        with Image.open(source) as image:
            image.convert("RGB").save(
                destination, format=THUMBNAIL_FORMAT, quality=THUMBNAIL_QUALITY, method=4
            )
        source.unlink(missing_ok=True)
        return True

    try:
        converted = await asyncio.to_thread(_convert)
    except Exception:
        # A cover that cannot be re-encoded is not a reason to fail the scan
        # of the file it belongs to: the existing .jpg keeps serving.
        logger.exception("Could not convert thumbnail %s to %s", cover_path, THUMBNAIL_EXTENSION)
        return None
    return destination.name if converted else None


# ---------- Avatar ----------
# The user's avatar is an image this app stores like any other: same WebP
# encoder, same content-addressed name, same directory. It lives under
# covers_dir rather than getting a mount of its own because that directory
# is not really "covers" any more -- it is this app's one content-addressed
# image store, with authenticated requests and private revalidation. A second mount, a second config value
# and a second entrypoint chown, all for one file per user, would be more
# machinery than the naming costs.
#
# The `avatar-` prefix does the same job `series-` does: an item's
# thumbnail is `{item_id}-{hash}` and so always starts with a digit, so
# neither prefix can collide with one, and the prefix is what says a file
# may safely be deleted when it is replaced. Deleting an item's thumbnail
# by mistake would blank that item's cover.
AVATAR_PREFIX = "avatar-"

# Square, and small: the only place it renders is a slot in the header a
# couple of dozen pixels tall, plus a preview on the settings page. 128px
# covers both at 2x on a phone with room to spare.
AVATAR_SIZE = (128, 128)


def _encode_avatar_sync(image_bytes: bytes) -> bytes:
    """Centre-cropped to a square, then resized.

    Cropping rather than squashing: an avatar renders in a square slot, so a
    portrait photo scaled to fit would either letterbox (dead space in a slot
    this small) or distort the face. The centre is where the subject of a
    profile picture is; anything smarter needs face detection, which is not
    worth a dependency here.
    """
    image = Image.open(io.BytesIO(image_bytes))
    image = image.convert("RGB")
    side = min(image.width, image.height)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    image = image.resize(AVATAR_SIZE, Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format=THUMBNAIL_FORMAT, quality=THUMBNAIL_QUALITY, method=4)
    return buffer.getvalue()


async def save_avatar(image_bytes: bytes, user_id: int) -> str:
    """Stores an uploaded image as this user's avatar; returns its relative path.

    Hashed over the ENCODED bytes, like save_custom_series_cover: /covers is
    served `immutable`, so a new picture has to mean a new URL or the
    browser keeps painting the old one, and re-uploading the same picture
    resolves to the same name instead of orphaning a file.
    """
    encoded = await asyncio.to_thread(_encode_avatar_sync, image_bytes)
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    relative_path = f"{AVATAR_PREFIX}{user_id}-{digest}.{THUMBNAIL_EXTENSION}"

    def _write() -> None:
        dest = settings.covers_dir / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(encoded)

    await asyncio.to_thread(_write)
    return relative_path


def delete_avatar(avatar_path: str | None) -> None:
    """Removes an avatar's file, if that is what the path names."""
    if not avatar_path or not avatar_path.startswith(AVATAR_PREFIX):
        return
    try:
        (settings.covers_dir / avatar_path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete avatar %s", avatar_path)
