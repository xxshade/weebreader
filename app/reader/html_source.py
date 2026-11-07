"""HTML5 repair for browser/source parity, using Bleach's pinned parser bundle."""

from collections import OrderedDict
import hashlib
import threading

from lxml import html


def parse_html5(source):
    from bleach._vendor import html5lib

    tree = html5lib.parse(source, treebuilder="lxml", namespaceHTMLElements=False)
    normalized = html5lib.serialize(
        tree,
        tree="lxml",
        omit_optional_tags=False,
        quote_attr_values="always",
        inject_meta_charset=False,
    )
    return html.document_fromstring(
        normalized.encode("utf-8"), parser=html.HTMLParser(encoding="utf-8")
    )


# Bound transient repaired text; retain no DOM trees or persisted rule history.

_TEXT_CACHE = OrderedDict()
_TEXT_CACHE_UNITS = 0
_TEXT_CACHE_LIMIT = 500_000
_TEXT_CACHE_ENTRIES = 16
_TEXT_LOCK = threading.Lock()


def repaired_readable(source, skipped):
    global _TEXT_CACHE_UNITS
    key = (hashlib.sha256(source.encode("utf-8")).digest(), skipped)
    with _TEXT_LOCK:
        value = _TEXT_CACHE.get(key)
        if value is not None:
            _TEXT_CACHE.move_to_end(key)
            return value
    doc = parse_html5(source)
    for node in doc.xpath("//" + "|//".join(skipped.split(","))):
        node.drop_tree()
    body = doc.find("body")
    value = " ".join((body if body is not None else doc).text_content().split())
    if len(value) <= _TEXT_CACHE_LIMIT:
        with _TEXT_LOCK:
            previous = _TEXT_CACHE.pop(key, None)
            if previous is not None:
                _TEXT_CACHE_UNITS -= len(previous)
            _TEXT_CACHE[key] = value
            _TEXT_CACHE_UNITS += len(value)
            while (
                len(_TEXT_CACHE) > _TEXT_CACHE_ENTRIES
                or _TEXT_CACHE_UNITS > _TEXT_CACHE_LIMIT
            ):
                _, old = _TEXT_CACHE.popitem(last=False)
                _TEXT_CACHE_UNITS -= len(old)
    return value
