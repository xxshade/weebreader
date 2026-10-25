"""Word inventories keyed by source identity, with the reader's block offsets.

Call these synchronous parsers in a worker thread. Cached inventories contain
only immutable word offsets, never open archives or a user's source content.
"""
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import zipfile
import zlib

from lxml import html

from app.library.parsers.epub import list_spine_resources
from app.stats.activity import word_starts


WHOLE = frozenset('p h1 h2 h3 h4 h5 h6 pre table figure hr details'.split())
COUNTED = frozenset('li blockquote dd dt'.split())
BLOCK_LEVEL = WHOLE | COUNTED | frozenset(
    'address article aside caption center dialog div dl fieldset figcaption footer form header '
    'hgroup legend main menu nav ol section summary ul'.split())
MEDIA = frozenset('img svg video audio picture canvas object embed iframe math'.split())
SKIPPED = frozenset('script style link meta title template base noscript'.split())
TEXT_SKIPPED = frozenset('script style noscript template svg math video audio canvas iframe object embed'.split())


@dataclass(frozen=True)
class BlockWords:
    positions: tuple[int, ...]
    length: int


@dataclass(frozen=True)
class Inventory:
    sections: tuple[tuple[BlockWords, ...], ...]
    total_words: int

    def remaining(self, section_index=0, block_index=0, offset=0):
        """Words from a saved UTF-16 anchor, or None for an invalid anchor.

        A word belongs to the fragment containing its start, exactly as in
        reading-activity accounting. No percentage is used to infer words.
        """
        if any(type(value) is not int or value < 0 for value in (section_index, block_index, offset)):
            return None
        if section_index >= len(self.sections):
            return None
        section = self.sections[section_index]
        if block_index == len(section) and offset == 0:
            here = 0
        elif block_index >= len(section) or offset > section[block_index].length:
            return None
        else:
            block = section[block_index]
            here = len(block.positions) - bisect_left(block.positions, offset)
            here += sum(len(other.positions) for other in section[block_index + 1:])
        return here + sum(len(block.positions) for rest in self.sections[section_index + 1:] for block in rest)


def _name(node):
    return node.tag.lower() if isinstance(node.tag, str) else ''


def _children(node):
    if node.text:
        yield node.text
    for child in node:
        yield child
        if child.tail:
            yield child.tail


def _chunks(node, readable=True):
    if isinstance(node, str):
        yield node, readable
    elif _name(node) and _name(node) != 'template':
        # HTMLTemplateElement stores descendants in a detached .content
        # fragment, outside childNodes/textContent and pagination's text map.
        for child in _children(node):
            yield from _chunks(child, readable and _name(node) not in TEXT_SKIPPED)


def _has_content(node):
    if isinstance(node, str):
        return bool(node.strip())
    name = _name(node)
    if not name or name in SKIPPED:
        return False
    return name in MEDIA or any(_has_content(child) for child in _children(node))


def _holds_blocks(node):
    for child in node:
        name = _name(child)
        if not name or name in SKIPPED or name in MEDIA:
            continue
        if name in BLOCK_LEVEL or _holds_blocks(child):
            return True
    return False


def _block(nodes):
    # Saved anchors use pagination's full textContent map. Skipped SVG/math/
    # script text contributes source offsets, but never contributes words.
    text, offsets, length = [], [], 0
    for node in nodes:
        for chunk, readable in _chunks(node):
            for char in chunk:
                if readable:
                    text.append(char)
                    offsets.append(length)
                length += 2 if ord(char) > 0xffff else 1
    return BlockWords(tuple(offsets[index] for index in word_starts(''.join(text))), length)


def _walk(container, blocks):
    run = []

    def flush():
        # Match readerBlocks' trimming of text/comments, keeping empty elements.
        first, last = 0, len(run) - 1
        def quiet(node):
            return (isinstance(node, str) or not _name(node)) and not _has_content(node)
        while first <= last and quiet(run[first]):
            first += 1
        while last >= first and quiet(run[last]):
            last -= 1
        nodes = run[first:last + 1]
        if any(_has_content(node) for node in nodes):
            blocks.append(_block(nodes))
        run.clear()

    for node in _children(container):
        name = '' if isinstance(node, str) else _name(node)
        if not name:
            run.append(node)
        elif name in SKIPPED:
            continue
        elif name in WHOLE:
            flush()
            blocks.append(_block((node,)))
        elif name in MEDIA:
            run.append(node)
        elif _holds_blocks(node):
            flush()
            _walk(node, blocks)
        elif name in BLOCK_LEVEL and (name in COUNTED or _has_content(node)):
            flush()
            blocks.append(_block((node,)))
        else:
            run.append(node)
    flush()


def _rules(rules):
    return tuple((rule['find'], rule.get('replace') or '') for rule in rules if rule.get('find'))


def _section(source, rules):
    if not source.strip():
        return ()
    if rules:
        def replace(match):
            value = match.group(2)
            if value is None:
                return match.group(0)
            for find, replacement in rules:
                value = value.replace(find, replacement)
            return '>' + value + '<'
        source = re.sub(r'<(script|style)\b[\s\S]*?</\1\s*>|>([^<]+)<', replace, source, flags=re.I)
    doc = html.document_fromstring(source.encode('utf-8'), parser=html.HTMLParser(encoding='utf-8'))
    body = doc.find('body')
    if body is None:
        body = doc
    blocks = []
    _walk(body, blocks)
    if not blocks:
        nodes = [node for node in _children(body) if
                 (isinstance(node, str) and node.strip()) or
                 (not isinstance(node, str) and _name(node) and _name(node) not in SKIPPED)]
        if nodes:
            blocks.append(_block(nodes))
    return tuple(blocks)


_cache = OrderedDict()
_cache_lock = threading.Lock()
_cache_units = 0
_CACHE_ENTRIES = 32
_CACHE_UNITS = 500_000
_CACHE_VERSION = 3
_MAX_DISK_BYTES = 32 * 1024 * 1024
_MAX_CACHE_DISK_BYTES = 256 * 1024 * 1024
_TOTAL_ENTRIES = 4096
_totals = OrderedDict()
_disk_lock = threading.Lock()


def _units(inventory):
    # Empty/media blocks still allocate storage, even without word offsets.
    return inventory.total_words + sum(map(len, inventory.sections))


def _disk_path(key):
    from app.config import settings
    digest = hashlib.sha256(json.dumps((_CACHE_VERSION, key), ensure_ascii=False).encode('utf-8')).hexdigest()
    return settings.data_dir / 'text-inventories' / (digest + '.json.gz')


def _summary_path(path):
    return path.with_name(path.name.removesuffix('.json.gz') + '.total.json')


def _summary_checksum(path, words):
    return hashlib.sha256(f'{path.name}:{words}'.encode('ascii')).hexdigest()


def _summary_data(path, words):
    return json.dumps({'format': _CACHE_VERSION, 'words': words,
                       'checksum': _summary_checksum(path, words)}, separators=(',', ':')).encode('ascii')


def _read_total(path):
    try:
        with _summary_path(path).open('rb') as stream:
            data = stream.read(257)
        if len(data) > 256:
            return None
        saved = json.loads(data)
        words = saved.get('words')
        if (saved.get('format') != _CACHE_VERSION or type(words) is not int or words < 0
                or saved.get('checksum') != _summary_checksum(path, words)):
            return None
        return words
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _read_disk(path):
    try:
        if path.stat().st_size > _MAX_DISK_BYTES:
            return None
        with path.open('rb') as stream, gzip.GzipFile(fileobj=stream) as compressed:
            data = compressed.read(_MAX_DISK_BYTES + 1)
        if len(data) > _MAX_DISK_BYTES:
            return None
        saved = json.loads(data)
        if saved.get('format') != _CACHE_VERSION or not isinstance(saved.get('sections'), list):
            return None
        sections = []
        for section in saved['sections']:
            if not isinstance(section, list):
                return None
            blocks = []
            for block in section:
                if not isinstance(block, list) or len(block) != 2:
                    return None
                length, positions = block
                if (type(length) is not int or length < 0 or not isinstance(positions, list)
                        or any(type(offset) is not int or not 0 <= offset < length for offset in positions)
                        or any(a >= b for a, b in zip(positions, positions[1:]))):
                    return None
                blocks.append(BlockWords(tuple(positions), length))
            sections.append(tuple(blocks))
        return Inventory(tuple(sections), sum(len(block.positions) for section in sections for block in section))
    except (OSError, ValueError, TypeError, AttributeError, EOFError, zlib.error):
        # Corrupt/old caches are disposable; the source remains authoritative.
        return None


def _atomic_write(path, data):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
    except OSError:
        # A read-only/full cache directory must not prevent reading a book.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _prune_disk(directory):
    # Prune only our named artifacts, never another process's temporary file.
    # Pair summaries and offsets so old source/replacement versions cannot grow
    # the cache forever. Modification order is sufficient for disposable data.
    try:
        pairs = {}
        for path in directory.iterdir():
            if re.fullmatch(r'[0-9a-f]{64}\.(?:json\.gz|total\.json)', path.name):
                stat = path.stat()
                pairs.setdefault(path.name[:64], []).append((stat.st_mtime_ns, stat.st_size, path))
        entries = [(max(value[0] for value in pair), sum(value[1] for value in pair), pair)
                   for pair in pairs.values()]
        size = sum(entry[1] for entry in entries)
        for _, length, pair in sorted(entries):
            if size <= _MAX_CACHE_DISK_BYTES:
                break
            for _, _, path in pair:
                path.unlink(missing_ok=True)
            size -= length
    except OSError:
        pass


def _write_disk(path, inventory):
    data = json.dumps({'format': _CACHE_VERSION,
                       'sections': [[[block.length, block.positions] for block in section]
                                    for section in inventory.sections]}, separators=(',', ':')).encode('utf-8')
    if len(data) > _MAX_DISK_BYTES:
        return
    with _disk_lock:
        _atomic_write(path, gzip.compress(data, compresslevel=1, mtime=0))
        _atomic_write(_summary_path(path), _summary_data(path, inventory.total_words))
        _prune_disk(path.parent)


def _remember_total(path, words):
    with _cache_lock:
        _totals[str(path)] = words
        _totals.move_to_end(str(path))
        while len(_totals) > _TOTAL_ENTRIES:
            _totals.popitem(last=False)


def _inventory(key, sections):
    global _cache_units
    disk_path = _disk_path(key)
    key = (str(disk_path), key)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    inventory = _read_disk(disk_path)
    if inventory is None:
        content = tuple(sections())
        inventory = Inventory(content, sum(len(block.positions) for section in content for block in section))
        _write_disk(disk_path, inventory)
    _remember_total(disk_path, inventory.total_words)
    units = _units(inventory)
    if units <= _CACHE_UNITS:
        with _cache_lock:
            existing = _cache.pop(key, None)
            if existing is not None:
                _cache_units -= _units(existing)
            _cache[key] = inventory
            _cache_units += units
            while len(_cache) > _CACHE_ENTRIES or _cache_units > _CACHE_UNITS:
                _cache_units -= _units(_cache.popitem(last=False)[1])
    return inventory


def _html_key(source, rules):
    return ('html', hashlib.sha256(source.encode('utf-8')).hexdigest(), rules)


def inventory_html(source, rules=()):
    rules = _rules(rules)
    return _inventory(_html_key(source, rules), lambda: (_section(source, rules),))


def _epub_key(file_path, version, rules):
    path = Path(file_path)
    stat = path.stat()
    return ('epub', str(path.resolve()), version, stat.st_size, stat.st_mtime_ns, rules)


def inventory_epub(file_path, version, rules=()):
    path = Path(file_path)
    rules = _rules(rules)
    key = _epub_key(path, version, rules)
    def sections():
        spine = list_spine_resources(str(path))
        with zipfile.ZipFile(path) as archive:
            for entry in spine:
                yield _section(archive.read(entry['href']).decode('utf-8-sig', errors='replace'), rules)
    return _inventory(key, sections)


def _total(key, build):
    path = _disk_path(key)
    with _cache_lock:
        if str(path) in _totals:
            _totals.move_to_end(str(path))
            return _totals[str(path)]
    words = _read_total(path)
    if words is None:
        words = build().total_words
        # Repair a missing/corrupt summary even if the offset inventory was
        # already in memory or on disk and did not need to be parsed again.
        with _disk_lock:
            _atomic_write(_summary_path(path), _summary_data(path, words))
            _prune_disk(path.parent)
    _remember_total(path, words)
    return words


def inventory_html_total(source, rules=()):
    rules = tuple(rules)
    return _total(_html_key(source, _rules(rules)), lambda: inventory_html(source, rules))


def inventory_epub_total(file_path, version, rules=()):
    rules = tuple(rules)
    return _total(_epub_key(file_path, version, _rules(rules)), lambda: inventory_epub(file_path, version, rules))
