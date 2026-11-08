"""Validated raw-HTML provenance when replacements change DOM structure."""

from html import unescape
import re
import uuid

from app.reader.source_anchors import utf16

TAG = re.compile(
    r"""<!--[\s\S]*?-->|<![^>]*>|</?[A-Za-z](?:[^'"<>]|"[^"]*"|'[^']*')*>"""
)
ENTITY = re.compile(
    r"&(?:#[xX][0-9a-fA-F]+;?|#[0-9]+;?|[A-Za-z][A-Za-z0-9]*;?)|\r\n?|\0"
)
RAW = re.compile(r"^<(script|style|textarea|title)\b", re.I)


def point(value, edits, back=False):
    shift = 0
    for a, b, c, d in edits:
        if back:
            a, b, c, d = c, d, a, b
        if value < a:
            break
        if value < b:
            return c
        shift = d - b
    return value + shift


def literal(text, rules, version=2):
    from app.replacement_matching import trace
    output, stages = trace(text, rules, raw=True, version=version)
    return output, [[tuple(edit[k] for k in ('a','b','c','d')) for edit in stage] for stage in stages]


def translate(value, stages, back=False):
    for edits in reversed(stages) if back else stages:
        value = point(value, edits, back)
    return value


def decode(value, raw=False):
    pieces = []
    edits = []
    cursor = before = after = 0
    for match in ENTITY.finditer(value):
        gap = value[cursor : match.start()]
        units = utf16(gap)
        a, b, c = before + units, before + units + utf16(match[0]), after + units
        replacement = match[0]
        if replacement[0] == "&":
            if raw:
                continue
            replacement = unescape(replacement)
        elif replacement[0] == "\r":
            replacement = "\n"
        else:
            replacement = "\ufffd" if raw else ""
        pieces.extend((gap, replacement))
        before, after = b, c + utf16(replacement)
        edits.append((a, b, c, after))
        cursor = match.end()
    pieces.append(value[cursor:])
    return "".join(pieces), edits


def cp_at(value, offset):
    return len(
        value.encode("utf-16-le", errors="surrogatepass")[: offset * 2].decode(
            "utf-16-le", errors="ignore"
        )
    )


def lexical(source):
    tokens = []
    cursor = scan = 0
    while match := TAG.search(source, scan):
        if match.start() > cursor:
            tokens.append(
                dict(a=cursor, b=match.start(), text=source[cursor : match.start()])
            )
        tokens.append(dict(a=match.start(), b=match.end(), tag=match[0]))
        cursor = scan = match.end()
        if name := RAW.match(match[0]):
            close = re.search(r"</" + name[1] + r"\s*>", source[cursor:], re.I)
            stop = cursor + close.start() if close else len(source)
            if stop > cursor:
                tokens.append(
                    dict(
                        a=cursor,
                        b=stop,
                        text=source[cursor:stop],
                        raw=name[1].lower() in ("script", "style"),
                    )
                )
            if close:
                end = cursor + close.end()
                tokens.append(dict(a=stop, b=end, tag=source[stop:end]))
                cursor = scan = end
            else:
                cursor = len(source)
                break
    if cursor < len(source):
        tokens.append(dict(a=cursor, b=len(source), text=source[cursor:]))
    return tokens


def transform(source, rules, version=2):
    spans = []
    pieces = []
    cursor = delta = 0
    for m in re.finditer(r"<(script|style)\b[\s\S]*?</\1\s*>|>([^<]+)<", source, re.I):
        if m[2] is None:
            continue
        a, b = m.start() + 1, m.end() - 1
        output, stages = literal(m[2], rules, version)
        c = a + delta
        pieces.extend((source[cursor:a], output))
        spans.append((a, b, c, c + len(output), m[2], output, stages))
        cursor = b
        delta += len(output) - (b - a)
    pieces.append(source[cursor:])
    output = "".join(pieces)

    def mapped(value, back=False):
        shift = 0
        for a, b, c, d, before, after, stages in spans:
            start, end = (c, d) if back else (a, b)
            if value < start:
                break
            if value <= end:
                text, target = (after, before) if back else (before, after)
                local = translate(utf16(text[: value - start]), stages, back)
                return (a if back else c) + cp_at(target, local)
            shift = (b if back else d) - end
        return value + shift

    return output, mapped


def parsed(source):
    from app.stats.text_inventory import _section, _children, _name

    prefix = "\ue000rm" + uuid.uuid4().hex + "_"
    suffix = "\ue001"
    attr = "data-rm" + uuid.uuid4().hex
    tokens = lexical(source)
    pieces = []
    for i, t in enumerate(tokens):
        if "tag" in t:
            value = t["tag"]
            if re.match("<[A-Za-z]", value):
                value = re.sub(r"/?\s*>$", lambda m: f' {attr}="{i}"' + m[0], value)
            pieces.append(value)
        elif re.search(r"\S", t["text"]):
            t["decoded"], t["edits"] = decode(t["text"], t.get("raw", False))
            pieces.append(prefix + str(i) + suffix)
        else:
            pieces.append(t["text"])
    units = _section("".join(pieces), (), lambda nodes: tuple(nodes), force_html5=True)
    actual = _section(source, (), lambda nodes: tuple(nodes), force_html5=True)
    if len(units) != len(actual):
        return None
    marker = re.compile(prefix + r"(\d+)" + suffix)
    ranges = []
    anchors = []
    lengths = []
    seen = set()

    def signature(node):
        if isinstance(node, str):
            return node
        return (_name(node), tuple(signature(child) for child in _children(node)))

    for i, unit in enumerate(units):
        offset = 0

        def walk(node):
            nonlocal offset
            if isinstance(node, str):
                parts = []
                cursor = 0
                for match in marker.finditer(node):
                    gap = node[cursor : match.start()]
                    parts.append(gap)
                    offset += utf16(gap)
                    t = tokens[int(match[1])]
                    parts.append(t["decoded"])
                    ranges.append(
                        dict(
                            a=t["a"],
                            b=t["b"],
                            index=i,
                            offset=offset,
                            length=utf16(t["decoded"]),
                            token=t,
                        )
                    )
                    offset += utf16(t["decoded"])
                    cursor = match.end()
                parts.append(node[cursor:])
                offset += utf16(node[cursor:])
                return "".join(parts)
            identity = node.get(attr) if _name(node) else None
            if identity is not None and identity not in seen:
                t = tokens[int(identity)]
                anchors.append(dict(a=t["a"], b=t["b"], index=i, offset=offset))
                seen.add(identity)
            return (_name(node), tuple(walk(child) for child in _children(node)))

        restored = tuple(walk(node) for node in unit)
        lengths.append(offset)
        if restored != tuple(signature(node) for node in actual[i]):
            return None
    return dict(ranges=ranges, anchors=anchors, lengths=lengths)


def to_raw(mapping, index, offset):
    lengths = mapping["lengths"]
    if index < 0 or index >= len(lengths) or offset < 0 or offset > lengths[index]:
        return None
    rows = [r for r in mapping["ranges"] if r["index"] == index]
    for r in rows:
        if r["offset"] <= offset < r["offset"] + r["length"]:
            t = r["token"]
            local = point(offset - r["offset"], t["edits"], True)
            return r["a"] + cp_at(t["text"], local)
    for r in rows:
        if r["offset"] >= offset:
            return r["a"]
    if rows and offset == lengths[index] and lengths[index]:
        return rows[-1]["b"]
    return next(
        (
            a["a"]
            for a in mapping["anchors"]
            if a["index"] == index and a["offset"] == offset
        ),
        None,
    )


def from_raw(mapping, value):
    for a in mapping["anchors"]:
        if a["a"] <= value < a["b"]:
            return a["index"], a["offset"]
    for r in mapping["ranges"]:
        if r["a"] <= value < r["b"]:
            t = r["token"]
            local = utf16(t["text"][: value - r["a"]])
            return r["index"], r["offset"] + point(local, t["edits"])
    ordered = sorted(mapping["ranges"] + mapping["anchors"], key=lambda r: r["a"])
    for r in ordered:
        if r["a"] >= value:
            return r["index"], r["offset"]
    return (
        (len(mapping["lengths"]) - 1, mapping["lengths"][-1])
        if mapping["lengths"]
        else None
    )


def convert_position(source, rules, index, offset, backwards=False, version=2):
    output, change = transform(source, rules, version)
    original, display = parsed(source), parsed(output)
    if original is None or display is None:
        return None
    a, b = (display, original) if backwards else (original, display)
    raw = to_raw(a, index, offset)
    return from_raw(b, change(raw, backwards)) if raw is not None else None


def migrate_position(source, rules, index, offset):
    """Map old rendered coordinates through the same source's new rendering."""
    from app.replacement_matching import HTML_TEXT, migrate, replace_html

    old, new = replace_html(source, rules, version=1), replace_html(source, rules)
    if old == new:
        return index, offset
    before, after = parsed(old), parsed(new)
    if before is None or after is None:
        return None
    raw = to_raw(before, index, offset)
    if raw is None:
        return None
    old_delta = new_delta = 0
    for match in HTML_TEXT.finditer(source):
        if match[2] is None:
            continue
        a = match.start() + 1
        old_text, _ = literal(match[2], rules, 1)
        new_text, _ = literal(match[2], rules, 2)
        old_start, new_start = a + old_delta, a + new_delta
        if raw < old_start:
            break
        if raw <= old_start + len(old_text):
            units = migrate(match[2], rules, utf16(old_text[:raw - old_start]))
            return from_raw(after, new_start + cp_at(new_text, units))
        old_delta += len(old_text) - len(match[2])
        new_delta += len(new_text) - len(match[2])
    return from_raw(after, raw + new_delta - old_delta)
