"""
Reads specific content from inside an EPUB/CBZ without unpacking everything
to disk. Used by the web reader (comic and epub).
"""

import threading
import zipfile
from collections import OrderedDict

from PIL import Image

from app.library.parsers.cbz import page_names_from

IMAGE_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "webp": "image/webp", "gif": "image/gif"}
TEXT_MIME = {"xhtml": "application/xhtml+xml", "html": "text/html", "css": "text/css",
             "js": "application/javascript", "ncx": "application/x-dtbncx+xml",
             "opf": "application/oebps-package+xml"}


def mime_for(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    return IMAGE_MIME.get(ext) or TEXT_MIME.get(ext) or "application/octet-stream"


# The page order of the comics being read, by (path, file hash): putting
# a CBZ's names in natural order was 0.32 of the 1.17 ms a page request
# took on a 200-page volume, redone for every page and every preload
# (about four per turn) of an order that cannot change while the file
# does not. The hash is in the key, so a replaced file is listed afresh.
# Only the NAMES are kept, not the open archive: closing an evicted one
# under a read in another thread is the kind of bug this is not worth.
_PAGE_ORDER_SIZE = 16
_page_order: OrderedDict = OrderedDict()
_page_order_lock = threading.Lock()


def _pages_of(zf: zipfile.ZipFile, file_path: str, file_hash: str | None) -> list[str]:
    if file_hash is None:
        return page_names_from(zf.namelist())
    key = (file_path, file_hash)
    with _page_order_lock:
        pages = _page_order.get(key)
        if pages is not None:
            _page_order.move_to_end(key)
            return pages
    pages = page_names_from(zf.namelist())
    with _page_order_lock:
        _page_order[key] = pages
        while len(_page_order) > _PAGE_ORDER_SIZE:
            _page_order.popitem(last=False)
    return pages


def comic_page_bytes(file_path: str, page_index: int,
                     file_hash: str | None = None) -> tuple[bytes, str]:
    """One page's bytes + mime, opening the archive exactly ONCE.

    This is the hottest path in the app (every page turn hits it). It used
    to list the pages and then read one, via two helpers that EACH opened
    the CBZ and re-read its central directory -- measured at ~36% of the
    total time for a 200-page volume, spent purely on that second open.
    """
    with zipfile.ZipFile(file_path) as zf:
        pages = _pages_of(zf, file_path, file_hash)
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page out of range")
        name = pages[page_index]
        return zf.read(name), mime_for(name)


def comic_all_page_dimensions(file_path: str) -> list[tuple[int, int]]:
    """
    Dimensions (width, height) of ALL pages in the CBZ, in order.

    Runs once (the result is cached in the database by the router). Uses
    streamed reading (`zf.open`) instead of `zf.read` -- PIL only needs the
    first few bytes of the file to figure out the size (the JPEG/PNG header
    up to the dimensions marker), without decoding the pixels or unpacking
    the whole entry; matters on weak hardware/SBCs, which is the project's
    stated target.
    """
    dimensions: list[tuple[int, int]] = []
    with zipfile.ZipFile(file_path) as zf:
        # Same single-open reasoning as comic_page_bytes above.
        for name in page_names_from(zf.namelist()):
            try:
                with zf.open(name) as f, Image.open(f) as im:
                    dimensions.append(im.size)
            except Exception:
                # corrupted page/unexpected format -- the frontend falls
                # back to the estimated-height placeholder just for this page
                dimensions.append((0, 0))
    return dimensions


def epub_resource_bytes(file_path: str, resource_path: str) -> tuple[bytes, str]:
    with zipfile.ZipFile(file_path) as zf:
        data = zf.read(resource_path)
    return data, mime_for(resource_path)
