"""Original-text anchors; no replacement history is retained."""

import hashlib
import json
import re

from fastapi import HTTPException


def parse_anchor(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        data = json.loads(value)
        if not isinstance(data, dict) or (
            type(data.get("version")) is not int or data.get("version") != 1
        ):
            return None
        position = data.get("position")
        if not isinstance(position, dict):
            return None
        if any(
            type(n) is not int or not 0 <= n <= 2**53 - 1
            for n in (
                data.get("section"),
                position.get("index"),
                position.get("offset"),
            )
        ):
            return None
        if any(
            not isinstance(data.get(k), str)
            or not re.fullmatch("[0-9a-f]{64}", data[k])
            for k in ("originalDigest", "rulesDigest")
        ):
            return None
        if type(data.get("matchingVersion", 1)) is not int or data.get("matchingVersion", 1) not in (1, 2):
            return None
        if "chapterId" in data and (
            type(data["chapterId"]) is not int or data["chapterId"] < 1
        ):
            return None
        return data
    except (ValueError, TypeError):
        return None


def validate_anchor(value, *, section=None, chapter_id=None):
    if value is None:
        return None
    data = parse_anchor(value)
    if (
        data is None
        or (section is not None and data["section"] != section)
        or (chapter_id is not None and data.get("chapterId") != chapter_id)
    ):
        raise HTTPException(400, "Invalid source anchor")
    return value


def progress_version(body, anchor):
    saved = parse_anchor(anchor)
    version = body.get("replacement_version", saved.get("matchingVersion", 1) if saved else 1)
    if type(version) is not int or version not in (1, 2):
        raise HTTPException(400, "Invalid replacement version")
    if saved and version != saved.get("matchingVersion", 1):
        raise HTTPException(400, "Replacement version does not match the source anchor")
    return version


def utf16(text):
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def mapped_offset(text, offset, rules, *, backwards=False, matching_version=2):
    """UTF-16 mapping through ordered literal edits, in either direction."""
    from app.replacement_matching import trace, translate
    output, stages = trace(text, rules, raw=False, version=matching_version)
    return output, translate(offset, stages, backwards=backwards)


def _convert_position_fast(source, rules, index, offset, *, backwards=False, matching_version=2):
    from app.stats.text_inventory import _section, _chunks, _rules

    def chunks(nodes):
        return tuple(chunk for node in nodes for chunk, _ in _chunks(node))

    original, rendered = (
        _section(source, (), chunks),
        _section(source, _rules(rules), chunks, matching_version=matching_version),
    )
    if len(original) != len(rendered) or index >= len(original):
        return None
    a, b = original[index], rendered[index]
    if len(a) != len(b) or offset > sum(map(utf16, b if backwards else a)):
        return None
    input_start = output_start = 0
    answer = None
    for n, (before, after) in enumerate(zip(a, b)):
        length = utf16(after if backwards else before)
        expected, mapped = mapped_offset(
            before,
            max(0, min(length, offset - input_start)),
            rules,
            backwards=backwards, matching_version=matching_version,
        )
        if expected != after:
            return None
        if answer is None and (offset < input_start + length or n == len(a) - 1):
            answer = output_start + mapped
        input_start += length
        output_start += utf16(before if backwards else after)
    return index, answer or 0


def _convert_position(source, rules, index, offset, *, backwards=False, matching_version=2):
    result = _convert_position_fast(source, rules, index, offset, backwards=backwards, matching_version=matching_version)
    if result is not None:
        return result
    from app.reader.raw_positions import convert_position

    return convert_position(source, rules, index, offset, backwards, matching_version)


def display_position(source, rules, anchor):
    """Fail closed unless the original source and both block maps agree."""
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != anchor["originalDigest"]:
        return None
    return _convert_position(
        source, rules, anchor["position"]["index"], anchor["position"]["offset"]
    )


def capture_anchor(source, rules, section, index, offset, chapter_id=None, matching_version=2):
    if not isinstance(source, str) or any(
        type(v) is not int or not 0 <= v <= 2**53 - 1 for v in (section, index, offset)
    ):
        return None
    position = _convert_position(source, rules, index, offset, backwards=True, matching_version=matching_version)
    if position is None:
        return None
    data = dict(
        version=1,
        section=section,
        position=dict(index=position[0], offset=position[1]),
        originalDigest=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        rulesDigest=hashlib.sha256(
            json.dumps(rules, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    if matching_version != 1:
        data["matchingVersion"] = matching_version
    if chapter_id is not None:
        data["chapterId"] = chapter_id
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def saved_display_position(source, rules, value, legacy=None):
    """Keep exact display offsets when their rule basis is still identifiable."""
    anchor = parse_anchor(value)
    if anchor and hashlib.sha256(source.encode("utf-8")).hexdigest() != anchor["originalDigest"]:
        return None
    digest = hashlib.sha256(json.dumps(rules, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if legacy is not None and (anchor is None or anchor["rulesDigest"] == digest):
        index, offset, version = legacy
        if version == 1 and (index or offset):
            from app.replacement_matching import affected
            if affected(rules):
                from app.reader.raw_positions import migrate_position
                return migrate_position(source, rules, index, offset)
        return index, offset
    return display_position(source, rules, anchor) if anchor else None


def remaining_html(source, rules, value, legacy=None):
    from app.stats.text_inventory import inventory_html
    anchor = parse_anchor(value)
    if anchor and "chapterId" in anchor and hashlib.sha256(source.encode("utf-8")).hexdigest() != anchor["originalDigest"]:
        from app.novels.scraper import tag_paragraphs
        source = tag_paragraphs(source)
    position = saved_display_position(source, rules, value, legacy)
    return inventory_html(source, rules).remaining(0, *position) if position else None


def remaining_epub(path, version, rules, value, legacy=None):
    import zipfile
    from app.library.parsers.epub import list_spine_resources
    from app.stats.text_inventory import inventory_epub
    anchor = parse_anchor(value)
    if anchor is None and legacy is None:
        return None
    section = anchor["section"] if anchor else legacy[0]
    with zipfile.ZipFile(path) as archive:
        spine = list_spine_resources(str(path))
        if section >= len(spine):
            return None
        source = archive.read(spine[section]["href"]).decode("utf-8-sig", errors="replace")
    position = saved_display_position(source, rules, value, legacy[1:] if legacy else None)
    return inventory_epub(path, version, rules).remaining(section, *position) if position else None


def _capture_item(row, rules):
    import zipfile
    from app.library.parsers.epub import list_spine_resources
    from lxml.etree import XMLSyntaxError

    try:
        match = re.fullmatch(r"elidx:(\d+)(?::(\d+))?", row["current_location"] or "")
        section = row["current_page"] or 0
        if match is None or section < 0:
            return None
        spine = list_spine_resources(row["file_path"])
        if section >= len(spine):
            return None
        with zipfile.ZipFile(row["file_path"]) as archive:
            source = archive.read(spine[section]["href"]).decode(
                "utf-8-sig", errors="replace"
            )
        return capture_anchor(source, rules, section, int(match[1]), int(match[2] or 0),
                              matching_version=row["replacement_version"])
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        XMLSyntaxError,
        zipfile.BadZipFile,
    ):
        return None  # An unavailable book does not prevent editing a rule.


async def _legacy_basis(db, user_id, series_id, novel_id):
    from app import text_replacements

    rules = await text_replacements.rules_for(
        db, user_id, series_id=series_id, novel_id=novel_id
    )
    if novel_id is not None:
        rows = await (
            await db.execute(
                """SELECT p.chapter_id,p.paragraph_index,p.character_offset,p.replacement_version,
            (SELECT COUNT(*) FROM chapters earlier WHERE earlier.novel_id=c.novel_id
             AND (earlier.order_index<c.order_index OR (earlier.order_index=c.order_index AND earlier.id<c.id))) AS section_index,
            c.content_html FROM novel_reading_progress p JOIN chapters c ON c.id=p.chapter_id
            WHERE p.user_id=? AND p.novel_id=? AND p.source_anchor IS NULL AND p.paragraph_index IS NOT NULL""",
                (user_id, novel_id),
            )
        ).fetchall()
    else:
        rows = await (
            await db.execute(
                """SELECT p.item_id,p.current_page,p.current_location,p.replacement_version,i.file_path,i.file_hash
            FROM reading_progress p JOIN items i ON i.id=p.item_id WHERE p.user_id=? AND i.series_id=?
            AND i.file_format='epub' AND p.status!='completed' AND p.source_anchor IS NULL
            AND p.current_location IS NOT NULL ORDER BY p.item_id""",
                (user_id, series_id),
            )
        ).fetchall()
    return rules, rows


async def prepare_legacy_target(db, user_id, *, series_id=None, novel_id=None):
    """Prepare without the writer; the mutation validates the whole basis later."""
    import asyncio
    from app.novels.scraper import tag_paragraphs

    rules, rows = await _legacy_basis(db, user_id, series_id, novel_id)
    values = []
    for row in rows:
        if novel_id is not None:
            if row["content_html"] is None:
                continue
            source = await asyncio.to_thread(tag_paragraphs, row["content_html"])
            value = await asyncio.to_thread(
                capture_anchor,
                source,
                rules,
                row["section_index"],
                row["paragraph_index"],
                row["character_offset"],
                row["chapter_id"], row["replacement_version"],
            )
            identity = novel_id
        else:
            value = await asyncio.to_thread(_capture_item, row, rules)
            identity = row["item_id"]
        if value:
            values.append((identity, value))
    return dict(
        user_id=user_id,
        series_id=series_id,
        novel_id=novel_id,
        rules=rules,
        rows=[tuple(row) for row in rows],
        values=values,
    )


async def apply_legacy_target(db, prepared):
    """Atomic validation plus metadata writes; retry preparation on any change."""
    rules, rows = await _legacy_basis(
        db, prepared["user_id"], prepared["series_id"], prepared["novel_id"]
    )
    if rules != prepared["rules"] or [tuple(row) for row in rows] != prepared["rows"]:
        return False
    novel = prepared["novel_id"] is not None
    table, column = (
        ("novel_reading_progress", "novel_id")
        if novel
        else ("reading_progress", "item_id")
    )
    for identity, value in prepared["values"]:
        await db.execute(
            f"UPDATE {table} SET source_anchor=? WHERE user_id=? AND {column}=? AND source_anchor IS NULL",
            (value, prepared["user_id"], identity),
        )
    return True
