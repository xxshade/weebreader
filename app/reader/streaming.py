"""
Reads specific content from inside an EPUB/CBZ without unpacking everything
to disk. Used by the web reader (comic and epub).
"""

import asyncio
import threading
import zipfile
from collections import OrderedDict

from PIL import Image
from starlette.responses import StreamingResponse

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


class ZipResource:
    """Own one archive and entry until streaming finishes or disconnects."""
    def __init__(self, archive, entry, info):
        self.archive, self.entry, self.info = archive, entry, info
        self._lock = threading.Lock()
        self.closed = False

    def read(self, size):
        with self._lock:
            return self.entry.read(size)

    def close(self):
        with self._lock:
            if not self.closed:
                try:
                    self.entry.close()
                finally:
                    self.archive.close()
                    self.closed = True


def _open_resource(file_path, select):
    archive = zipfile.ZipFile(file_path)
    try:
        name = select(archive)
        info = archive.getinfo(name)
        return ZipResource(archive, archive.open(info), info)
    except BaseException:
        archive.close()
        raise


def open_comic_page(file_path, page_index, file_hash=None):
    def select(archive):
        pages = _pages_of(archive, file_path, file_hash)
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page out of range")
        return pages[page_index]
    return _open_resource(file_path, select)


def open_epub_resource(file_path, resource_path):
    return _open_resource(file_path, lambda archive: resource_path)


async def open_in_thread(function, *args):
    # If the request is cancelled while the thread opens the ZIP, close the
    # eventual result rather than abandoning an open file descriptor.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        def close_result(done):
            if not done.cancelled() and done.exception() is None:
                done.result().close()
        task.add_done_callback(close_result)
        raise


class ZipStreamingResponse(StreamingResponse):
    def __init__(self, resource, headers=None):
        self.resource = resource
        async def chunks():
            while True:
                data = await asyncio.to_thread(resource.read, 64 * 1024)
                if not data:
                    break
                yield data
        super().__init__(chunks(), media_type=mime_for(resource.info.filename),
                         headers={'Content-Length': str(resource.info.file_size), **(headers or {})})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Reads can still be finishing in a worker after cancellation;
            # close waits for the resource lock rather than racing the read.
            closing = asyncio.create_task(asyncio.to_thread(self.resource.close))
            await asyncio.shield(closing)


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
