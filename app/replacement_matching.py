"""Literal replacement matching; ordinary/NBSP equivalence is version 2.

Raw HTML and decoded text are different domains: visible "&nbsp;" must not
be mistaken for a space after the HTML has already been decoded.
"""

from functools import lru_cache
import re

VERSION = 2
SPACE = r"[ \u00a0]"
RAW_SPACE = (
    SPACE
    + r"|&(?:nbsp;?|NonBreakingSpace;|#0*160(?![0-9]);?|#[xX]0*[aA]0(?![0-9a-fA-F]);?)"
)
HTML_TEXT = re.compile(r"<(script|style)\b[\s\S]*?</\1\s*>|>([^<]+)<", re.I)


@lru_cache(maxsize=512)
def pattern(find, raw=True, version=VERSION):
    if version == 1:
        return re.compile(re.escape(find))
    spaces = re.compile(RAW_SPACE if raw else SPACE)
    parts, cursor = [], 0
    for match in spaces.finditer(find):
        parts.extend(
            (re.escape(find[cursor : match.start()]), "(?:" + spaces.pattern + ")")
        )
        cursor = match.end()
    parts.append(re.escape(find[cursor:]))
    return re.compile("".join(parts))


def affected(rules):
    return any(
        re.search(RAW_SPACE, rule["find"] if isinstance(rule, dict) else rule[0])
        for rule in rules
    )


def cache_basis(rules):
    return (rules, "matching-spaces-v2") if affected(rules) else rules


def replace(text, find, replacement, *, raw=True, version=VERSION):
    if not find:
        return text
    if version == 1 or not re.search(RAW_SPACE if raw else SPACE, find):
        return text.replace(find, replacement)
    return pattern(find, raw, version).sub(lambda _: replacement, text)


def replace_html(source, rules, *, version=VERSION):
    def segment(match):
        text = match[2]
        if text is None:
            return match[0]
        for rule in rules:
            text = replace(
                text, rule["find"], rule.get("replace") or "", version=version
            )
        return ">" + text + "<"

    return HTML_TEXT.sub(segment, source)


def units(text):
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def trace(text, rules, *, raw=False, version=VERSION):
    stages = []
    for rule in rules:
        find, replacement = rule.get("find"), rule.get("replace") or ""
        edits, cursor, before, after, parts = [], 0, 0, 0, []
        if not find:
            stages.append(edits)
            continue
        for match in pattern(find, raw, version).finditer(text):
            gap = text[cursor : match.start()]
            a, c = before + units(gap), after + units(gap)
            b, d = a + units(match[0]), c + units(replacement)
            edits.append(dict(a=a, b=b, c=c, d=d, match=match[0]))
            parts.extend((gap, replacement))
            before, after, cursor = b, d, match.end()
        parts.append(text[cursor:])
        text = "".join(parts)
        stages.append(edits)
    return text, stages


def translate(point, stages, *, backwards=False):
    for edits in reversed(stages) if backwards else stages:
        shift = 0
        for edit in edits:
            keys = ("c", "d", "a", "b") if backwards else ("a", "b", "c", "d")
            a, b, c, d = (edit[k] for k in keys)
            if point < a:
                break
            if point < b:
                point, shift = c, 0
                break
            shift = d - b
        point += shift
    return point


def migrate(text, rules, point, *, raw=True):
    """Translate old displayed coordinates, retaining unchanged inserted text."""
    _, old = trace(text, rules, raw=raw, version=1)
    _, new = trace(text, rules, raw=raw)
    inside = {}
    for index in reversed(range(len(old))):
        for edit in old[index]:
            if edit["c"] <= point < edit["d"]:
                inside[index] = (point - edit["c"], edit["match"])
                break
        point = translate(point, [old[index]], backwards=True)
    for index, edits in enumerate(new):
        preserved = next(
            (
                edit
                for edit in edits
                if edit["a"] == point
                and index in inside
                and edit["match"] == inside[index][1]
            ),
            None,
        )
        if preserved:
            point = preserved["c"] + min(
                inside[index][0], preserved["d"] - preserved["c"]
            )
        else:
            point = translate(point, [edits])
    return point
