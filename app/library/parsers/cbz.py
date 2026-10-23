"""Extraction of metadata, cover, and page count from CBZ files."""

import zipfile
from xml.etree import ElementTree

from app.library.naturalsort import natural_sort_key
from app.library.parsers.parsed import ParsedItem
from app.library.parsers.plain_text import plain_text

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def page_names_from(namelist: list[str]) -> list[str]:
    """Image entries from an ALREADY-READ namelist, in reading order.

    Takes the namelist instead of a path so a caller that already has the
    archive open doesn't have to open it a second time just to enumerate
    pages -- reader/streaming.py is where this matters most (every single
    page turn used to open the same CBZ twice: once to list, once to read).

    Ordering is NATURAL, not plain lexicographic. Real bug this fixes: a
    huge share of manga scans number their pages without zero-padding
    ("1.jpg", "2.jpg", ... "10.jpg"), and sorted() compares those character
    by character -- "10.jpg" lands right after "1.jpg" because "0" < ".",
    so page 2 of the reader served page 10, page 3 served page 11, and so
    on for the whole volume. natural_sort_key (already used for
    items.sort_title / series.sort_name -- see naturalsort.py) zero-pads
    embedded digit runs so string comparison matches numeric comparison.
    Archives that ARE zero-padded ("001.jpg") sort identically under both,
    so they're completely unaffected by this change.
    """
    names = [
        n for n in namelist
        if not n.endswith("/") and "/".join(n.split("/")[:-1]).find("__MACOSX") == -1
        and ("." + n.rsplit(".", 1)[-1].lower()) in IMAGE_EXTENSIONS
    ]
    return sorted(names, key=natural_sort_key)


def _parse_comicinfo(zf: zipfile.ZipFile) -> dict:
    if "ComicInfo.xml" not in zf.namelist():
        return {}
    try:
        root = ElementTree.fromstring(zf.read("ComicInfo.xml"))
    except ElementTree.ParseError:
        return {}

    def text(tag: str) -> str | None:
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text else None

    metadata = {
        "author": text("Writer"),
        "publisher": text("Publisher"),
        "genre": text("Genre"),
        "description": plain_text(text("Summary")),
        "language": text("LanguageISO"),
    }
    return {k: v for k, v in metadata.items() if v}


def read_metadata(file_path: str) -> dict:
    """The embedded metadata alone, reading nothing but ComicInfo.xml --
    for the startup backfill (see epub.METADATA_VERSION)."""
    with zipfile.ZipFile(file_path) as zf:
        return _parse_comicinfo(zf)


def parse_cbz(file_path: str) -> ParsedItem:
    filename = file_path.rsplit("/", 1)[-1]
    with zipfile.ZipFile(file_path) as zf:
        metadata = _parse_comicinfo(zf)
        # The archive is already open right here, so the namelist is free;
        # nothing needs to reopen it just to enumerate pages.
        pages = page_names_from(zf.namelist())

        cover_bytes = None
        if pages:
            cover_bytes = zf.read(pages[0])

    # A CBZ's title is its filename -- ComicInfo.xml only ever feeds the
    # metadata fields above (author/publisher/...), never the title. The
    # volume/chapter NUMBER is not decided here: it depends on the rest of
    # the series, so the scanner does it (see scanner._renumber_series).
    title = filename.rsplit(".", 1)[0]

    return ParsedItem(
        title=title,
        metadata=metadata,
        cover_bytes=cover_bytes,
        page_count=len(pages),
    )
