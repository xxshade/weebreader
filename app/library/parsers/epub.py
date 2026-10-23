"""
Extraction of metadata, spine, and cover from EPUB files.

Parses the OPF directly via lxml, without going through
`ebooklib.epub.read_epub()`. Reason: EbookLib eagerly loads the *content* of
every manifest item when opening the book -- a single item with a broken
href (common in "real-world" EPUBs coming from web-novel converters/scrapers:
a referenced but missing cover, an orphan chapter, a missing image) takes
down parsing of the entire book with a KeyError, even though the rest of the
file is perfectly readable. Here we only read the OPF's text (always
required and always present in a valid EPUB) and try to read the cover under
an isolated `try/except` -- a manifest that's broken somewhere else doesn't
keep the book from showing up in the library.
"""

import posixpath
import zipfile
from urllib.parse import unquote

from lxml import etree

from app.library.parsers.parsed import ParsedItem
from app.library.parsers.plain_text import plain_text


CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
OPF_NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    container = etree.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(".//c:rootfile", CONTAINER_NS)
    return rootfile.attrib["full-path"]


def _opf_dir(opf_path: str) -> str:
    return opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""


def _resolve(base_dir: str, href: str) -> str:
    """Resolves a manifest href (relative to the .opf's directory) into the
    full path inside the zip -- normalizes '../' and decodes %20 etc."""
    href = unquote(href)
    path = posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else posixpath.normpath(href)
    return path


def _manifest_items(opf_root) -> dict:
    """id -> {href, media-type, properties}"""
    items = {}
    for item in opf_root.findall(".//opf:manifest/opf:item", OPF_NS):
        items[item.get("id")] = {
            "href": item.get("href"),
            "media-type": item.get("media-type"),
            "properties": item.get("properties", "") or "",
        }
    return items


def _find_cover_item_id(opf_root, manifest: dict) -> str | None:
    # EPUB3: manifest item with properties="cover-image"
    for item_id, item in manifest.items():
        if "cover-image" in item["properties"]:
            return item_id
    # EPUB2: <meta name="cover" content="ITEM_ID"/>
    meta_cover = opf_root.find(".//opf:metadata/opf:meta[@name='cover']", OPF_NS)
    if meta_cover is not None and meta_cover.get("content") in manifest:
        return meta_cover.get("content")
    # fallback: first image in the manifest
    for item_id, item in manifest.items():
        if (item["media-type"] or "").startswith("image/"):
            return item_id
    return None


# Bumped whenever _metadata() starts reading something differently, so
# the startup backfill (scanner.backfill_metadata) re-reads the books
# imported under the old rules -- process_file never revisits a file whose
# contents have not changed. Shared with the CBZ parser: one number for
# "how items.metadata was produced".
METADATA_VERSION = 1

OPF = "{http://www.idpf.org/2007/opf}"


def _authors(opf_root) -> str | None:
    """Every dc:creator credited as the author, in the file's order.

    A creator's role is `opf:role` in EPUB 2 and a `<meta refines="#id"
    property="role">` in EPUB 3 -- both are out there, so both are read.
    One with no role at all is taken as the author, which is what most
    files that bother to list only one creator mean. Illustrators,
    translators and editors are not authors (see GLOSSARY.md), which is
    what reading only the FIRST dc:creator got wrong whenever the
    illustrator happened to be listed first.
    """
    refined_roles = {}
    for meta in opf_root.findall(".//opf:metadata/opf:meta[@property='role']", OPF_NS):
        target = (meta.get("refines") or "").lstrip("#")
        if target and meta.text:
            refined_roles.setdefault(target, meta.text.strip().lower())

    names = []
    for creator in opf_root.findall(".//dc:creator", OPF_NS):
        name = " ".join("".join(creator.itertext()).split())
        if not name or name in names:
            continue
        role = (creator.get(f"{OPF}role") or refined_roles.get(creator.get("id") or "") or "").strip().lower()
        if role in ("", "aut"):
            names.append(name)
    return ", ".join(names) or None


def _dc_text(opf_root, tag: str) -> str | None:
    el = opf_root.find(f".//dc:{tag}", OPF_NS)
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def _metadata(opf_root) -> dict:
    description = opf_root.find(".//dc:description", OPF_NS)
    return {
        k: v
        for k, v in {
            "author": _authors(opf_root),
            "publisher": _dc_text(opf_root, "publisher"),
            "language": _dc_text(opf_root, "language"),
            # itertext, not .text: a converter that wrote the HTML as child
            # elements rather than escaping it leaves .text empty or holding
            # only what comes before the first tag.
            "description": plain_text("".join(description.itertext())) if description is not None else None,
            "published_date": _dc_text(opf_root, "date"),
            "isbn": _dc_text(opf_root, "identifier"),
        }.items()
        if v
    }


def read_metadata(file_path: str) -> dict:
    """The embedded metadata alone, reading nothing but the OPF -- for the
    startup backfill, which has no use for the cover or the spine."""
    with zipfile.ZipFile(file_path) as zf:
        return _metadata(etree.fromstring(zf.read(_find_opf_path(zf))))


def parse_epub(file_path: str) -> ParsedItem:
    with zipfile.ZipFile(file_path) as zf:
        opf_path = _find_opf_path(zf)
        opf_dir = _opf_dir(opf_path)
        opf_root = etree.fromstring(zf.read(opf_path))

        title = _dc_text(opf_root, "title") or file_path.rsplit("/", 1)[-1]

        # The volume/chapter number is deliberately NOT decided here.
        # `dc:title` is a poor sole source for it: it frequently names only
        # the work and not the volume (every volume then extracts nothing,
        # and they all collapse into the "no number" bucket with identical
        # sort_titles), and when it does carry a number it may be a year or
        # part of a subtitle rather than an index. The scanner reads this
        # title AND the filename, and resolves the number across the whole
        # series -- see scanner._renumber_series.

        metadata = _metadata(opf_root)

        manifest = _manifest_items(opf_root)
        cover_item_id = _find_cover_item_id(opf_root, manifest)

        cover_bytes = None
        if cover_item_id and cover_item_id in manifest:
            resolved = _resolve(opf_dir, manifest[cover_item_id]["href"])
            try:
                cover_bytes = zf.read(resolved)
            except KeyError:
                # cover listed in the manifest but actually missing from
                # the zip -- proceeds without a cover instead of taking down
                # parsing of the whole book
                pass

        # Computed on the zip this call already has open, off the central
        # directory -- no decompression, so it costs nothing here and saves
        # reopening the archive later just to measure it.
        text_size = spine_text_size(zf, opf_root, opf_dir)

    return ParsedItem(
        title=title, text_size=text_size, metadata=metadata,
        cover_bytes=cover_bytes,
    )


def _spine_entries(zf, opf_root, opf_dir) -> list[dict]:
    """The spine, off an already-open zip. Shared by list_spine_resources
    (the reader's manifest) and parse_epub (which sums the sizes at scan
    time), so the two cannot disagree about what the spine is."""
    manifest = {item.get("id"): item.get("href") for item in opf_root.findall(".//opf:manifest/opf:item", OPF_NS)}
    spine = []
    for itemref in opf_root.findall(".//opf:spine/opf:itemref", OPF_NS):
        idref = itemref.get("idref")
        href = manifest.get(idref)
        if not href:
            continue
        resolved = _resolve(opf_dir, href)
        try:
            size = zf.getinfo(resolved).file_size
        except KeyError:
            size = 0  # orphan entry -- see list_spine_resources' docstring
        spine.append({"id": idref, "href": resolved, "size": size})
    return spine


def spine_text_size(zf, opf_root, opf_dir) -> int:
    """Total uncompressed bytes of the documents in the reading order.

    This is the app's measure of "how much book there is", and it is
    deliberately NOT the file's size on disk. An EPUB is a zip that also
    holds images, fonts and a cover, and it is compressed -- so file size
    reflects illustration and compression ratio far more than prose.
    Measured on two synthetic books:

        ilustrado.epub   file 25,013,163   spine    20,160     2,400 words
        texto.epub       file     18,096   spine 1,927,251   240,000 words

    By file size the art book looks 1382x the novel; by words it is 1/100th
    of it. The spine total ranks them right (~96x), because images and
    fonts are manifest items but not spine itemrefs, and the count is the
    UNCOMPRESSED size, so how well the text squeezed does not enter into it.

    It is the same number the reader already weights the progress
    percentage by (see the manifest's `size`), which is what keeps the
    estimate and the percentage talking about the same book.

    Not a word count: markup overhead rides along, and a chapter that
    embeds an image as a base64 data URI would inflate its own entry. Both
    are roughly proportional across a library, which is all an estimate
    needs.
    """
    return sum(entry["size"] for entry in _spine_entries(zf, opf_root, opf_dir))


def measure_text_size(file_path: str) -> int:
    """spine_text_size for a file on disk. Used by the startup backfill for
    books imported before the column existed; a normal import gets the same
    number out of parse_epub, off the zip it already has open."""
    with zipfile.ZipFile(file_path) as zf:
        opf_path = _find_opf_path(zf)
        return spine_text_size(zf, etree.fromstring(zf.read(opf_path)), _opf_dir(opf_path))


def list_spine_resources(file_path: str) -> list[dict]:
    """Returns the reading order (spine) with the path (relative to the
    zip's root), id and uncompressed size of each chapter -- used by the
    reader's manifest.

    `size` is what lets the reader weight reading progress by how much
    text each entry actually holds instead of counting every spine entry
    the same. It matters because a real book's front matter (cover, title
    page, copyright, TOC) is several spine entries of one line each: with
    equal weights, paging through them alone reported 29% of a 14-entry
    book read before the first word of chapter one. It's the zip's own
    uncompressed byte count, already in the central directory this call
    has open, so it costs nothing to read. Markup overhead makes it only
    roughly proportional to the prose, which is enough for a progress
    bar -- it is not a word count.

    Doesn't validate whether each entry actually exists in the zip: an
    orphan chapter only turns into a one-off 404 when the reader tries to
    open that specific page, instead of keeping the whole book from
    showing up in the library. Such an entry reports size 0, and the
    reader falls back to an average weight for it (see reader-text.js)."""
    with zipfile.ZipFile(file_path) as zf:
        opf_path = _find_opf_path(zf)
        opf_dir = _opf_dir(opf_path)
        opf_root = etree.fromstring(zf.read(opf_path))
        return _spine_entries(zf, opf_root, opf_dir)
