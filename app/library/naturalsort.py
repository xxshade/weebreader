"""
Natural ordering: 'Volume 2' < 'Volume 10' (not lexicographic, where '10' <
'2' because '1' < '2' char by char). Used for items' sort_title and series'
sort_name -- avoids relying on regex-based number extraction (fragile,
covers only some filename formats) as the primary ordering source.

This module also owns the volume/chapter NUMBER extraction used to order
items within a series (items.number_in_series). See extract_candidates()
and resolve_series_numbers() below for why that is decided per series
rather than per file.
"""

import re
from dataclasses import dataclass
from typing import NamedTuple

_DIGIT_RUN = re.compile(r"(\d+)")


def natural_sort_key(text: str) -> str:
    parts = _DIGIT_RUN.split(text.lower())
    return "".join(p.zfill(10) if p.isdigit() else p for p in parts)


# ---------------------------------------------------------------------------
# Roman numerals
# ---------------------------------------------------------------------------
# Some collections number volumes as "Vol. IV" / "Livro III". Without this
# they extracted no number at all, so every volume landed in the "no number"
# bucket together and fell back to sort_title -- which sorts them
# ALPHABETICALLY, and that is wrong from IX onwards ('ix' > 'iv' but
# 'ix' < 'v', so IX lands between IV and V). Measured before adding this:
#   correct: I II III IV V VI VII VIII IX X XI XII
#   actual:  I II III IV IX V VI VII VIII X XI XII

_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
# Strict: matches only well-formed numerals, so a word that happens to be
# made of roman letters (e.g. "MIX", "DILL", "CIVIC") can't be read as one.
_ROMAN_RE = re.compile(
    r"^m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$", re.IGNORECASE
)


def roman_to_int(text: str) -> int | None:
    text = text.strip()
    if not text or not _ROMAN_RE.match(text):
        return None
    total = 0
    previous = 0
    for char in reversed(text.lower()):
        value = _ROMAN_VALUES[char]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total or None


# ---------------------------------------------------------------------------
# Number candidates
# ---------------------------------------------------------------------------
# A filename like "[Group] Series - c012 (v02) [scan]" carries TWO numbers,
# and which one orders the series depends on something a single file cannot
# know: whether this library stores one file per chapter or one per volume.
# Both exist. So extraction no longer returns "the" number -- it returns
# every candidate it can see, tagged with what kind of marker produced it,
# and resolve_series_numbers() below picks the kind once per series.
#
# KIND_BARE is a number with no marker at all ("Series 03"), kept last
# because it is the only one that can pick up something that isn't an
# index at all -- the 1984 in "The Year of 1984". (A year in a TAG,
# "Trilogy (2020)", is no longer read: see _BRACKETED.) It is still
# needed: plenty of files have no marker whatsoever.

KIND_CHAPTER = "chapter"
KIND_VOLUME = "volume"
KIND_BARE = "bare"

# Lower sorts first when coverage and distinctness tie: prefer the finer
# granularity, and prefer any explicit marker over a bare number.
_KIND_PRIORITY = {KIND_CHAPTER: 0, KIND_VOLUME: 1, KIND_BARE: 2}

_NUMBER = r"(\d+(?:\.\d+)?)"
_ROMAN = r"([mdclxvi]+)"

# Spelled-out markers may be followed by a dot and/or spaces ("Vol. 3",
# "Chapter 7"). Single-letter ones may NOT ("c012", "v02") -- requiring the
# digits to be glued to the letter is what keeps a title like "Series C
# 2020" or a lone "V" from being read as a marker.
_MARKER_PATTERNS: tuple[tuple[str, str], ...] = (
    (KIND_CHAPTER, rf"\b(?:chapter|chap|cap[ií]tulo|cap)\.?\s*{_NUMBER}"),
    (KIND_VOLUME, rf"\b(?:volume|vol|tomo|livro|book)\.?\s*{_NUMBER}"),
    (KIND_CHAPTER, rf"\bc{_NUMBER}"),
    (KIND_VOLUME, rf"\bv{_NUMBER}"),
    (KIND_VOLUME, rf"#\s*{_NUMBER}"),
    (KIND_CHAPTER, rf"\b(?:chapter|chap|cap[ií]tulo|cap)\.?\s*{_ROMAN}\b"),
    (KIND_VOLUME, rf"\b(?:volume|vol|tomo|livro|book)\.?\s*{_ROMAN}\b"),
)
_COMPILED_MARKERS = tuple((kind, re.compile(p, re.IGNORECASE)) for kind, p in _MARKER_PATTERNS)

_TRAILING_NUMBER = re.compile(rf"{_NUMBER}(?!.*\d)")

# "Part 2" -- one piece of a volume published split in several (see Part in
# GLOSSARY.md). Its number orders the pieces WITHIN their volume and is
# never the series' number, so it is taken out before the trailing number
# is read. Left in, it was the trailing number of "Volume 8 Part 2", and in
# a short series that bare number won the whole numbering: v07, v08 Part 1
# and v08 Part 2 came out 7, 1, 2, with v07 sorted last.
_PART = re.compile(r"\b(?:part|pt)\.?\s*(?:\d+(?:\.\d+)?|[ivx]+)\b", re.IGNORECASE)
# A bracketed group is a TAG, not part of the name: the release year, the
# source, the scanlation group -- "Series 032 (2026) (Digital) (Group)".
# The bare number is read from what is left once they are gone, because
# the trailing number of the whole text is whatever the last tag held.
# That is not hypothetical: a chapter-per-file series named that way was
# numbered 2026 on every file, and MangaUpdates and MyAnimeList were both
# told "2026 chapters read". Only the BARE reading changes -- a marker
# inside a group ("c012 (v02)") is still a marker.
_BRACKETED = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}")
# What a bracketed group may still offer when nothing outside one holds a
# number ("Series (01)"), minus the one thing a group most often holds.
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_ANY_NUMBER = re.compile(_NUMBER)
# A bare roman numeral is only read as one at the very END of the text
# ("Series III"), never mid-string -- too many ordinary words would qualify
# otherwise.
_TRAILING_ROMAN = re.compile(rf"\b{_ROMAN}\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class NumberCandidate:
    kind: str
    value: float


def extract_candidates(*texts: str | None) -> list[NumberCandidate]:
    """Every number that could plausibly be this item's index, tagged by the
    kind of marker that produced it. Accepts several strings because the two
    parsers disagree about where the good information lives: a CBZ's title
    IS its filename, while an EPUB's title is its `dc:title` metadata, which
    very often names only the work and not the volume -- the volume is in
    the filename the parser discarded. Passing both lets the resolver use
    whichever one actually discriminates the series."""
    found: list[NumberCandidate] = []
    seen: set[tuple[str, float]] = set()

    def add(kind: str, raw: str, roman: bool = False) -> None:
        if roman:
            number = roman_to_int(raw)
            value = float(number) if number is not None else None
        else:
            try:
                value = float(raw)
            except ValueError:
                value = None
        if value is None or (kind, value) in seen:
            return
        seen.add((kind, value))
        found.append(NumberCandidate(kind, value))

    for text in texts:
        if not text:
            continue
        for kind, pattern in _COMPILED_MARKERS:
            for match in pattern.finditer(text):
                add(kind, match.group(1), roman=not match.group(1)[:1].isdigit())
        bare = _bare_number(text)
        if bare is not None:
            add(KIND_BARE, *bare)

    return found


def _bare_number(text: str) -> tuple[str, bool] | None:
    """The unmarked number a name ends in, as (digits, is_roman).

    Read OUTSIDE the bracketed groups first (see _BRACKETED). Only when
    the name itself holds no number is a group consulted, and then never
    for a year: "Oneshot (2019) (Digital)" has no index, and 2019 is not
    one -- guessing it would put a year on a tracker as a chapter count."""
    outside = _PART.sub(" ", _BRACKETED.sub(" ", text))
    trailing = _TRAILING_NUMBER.search(outside)
    if trailing:
        return trailing.group(1), False
    roman_match = _TRAILING_ROMAN.search(outside)
    if roman_match:
        return roman_match.group(1), True
    for group in reversed(_BRACKETED.findall(text)):
        numbers = [n for n in _ANY_NUMBER.findall(group) if not _YEAR.match(n)]
        if numbers:
            return numbers[-1], False
    return None


def extract_number_in_series(*texts: str | None) -> float | None:
    """Single best guess for ONE item in isolation, with no series to
    compare against -- used for a standalone item (no series) and as the
    per-item fallback inside resolve_series_numbers(). Prefers an explicit
    marker over a bare number, and a chapter marker over a volume one, which
    is only a guess: which of the two actually orders a collection is what
    resolve_series_numbers() answers from the series as a whole."""
    candidates = extract_candidates(*texts)
    if not candidates:
        return None
    return min(candidates, key=lambda c: _KIND_PRIORITY[c.kind]).value


class SeriesNumbering(NamedTuple):
    """What resolve_series_numbers() decided: the number for each item, and
    the KIND of marker those numbers were read from.

    The kind used to be internal. It is returned because a tracker needs
    it: MangaUpdates and MyAnimeList both keep chapters and volumes as
    separate counts, so "the reader is at 7" is only half an answer --
    `series.number_kind` is where it is stored, and app/tracking/ is what
    reads it. `numbers` first, since ordering the shelf is still what this
    is mostly for."""

    numbers: dict[int, float | None]
    kind: str | None


def resolve_series_numbers(items: list[tuple[int, list[NumberCandidate]]]) -> SeriesNumbering:
    """Picks ONE kind of marker for the whole series, then reads every item's
    number from that kind. Takes (item_id, candidates) pairs and returns
    both the {item_id: number} mapping and the kind it picked.

    The kind is chosen by how well it tells the series' items apart:
    coverage first (how many items have such a number at all), then distinct
    values, then the priority above. That single rule handles the cases that
    made per-file extraction wrong:

      - "c012 (v02)" in a chapter-per-file series: both kinds cover every
        item, but the chapter numbers are all different while the volume
        number repeats across the files that share a volume -- so chapter
        wins, instead of every file in a volume collapsing onto the volume's
        number and relying on sort_title to break the tie.
      - The same names in a volume-per-file series have no chapter marker at
        all, so volume wins on coverage.
      - A stray year outside a tag ("Trilogy 2020") is a bare candidate;
        any real marker present across the series beats it on priority,
        and if the year is the same on every file it also loses on
        distinctness. A year inside one never becomes a candidate at all.

    An item with no number of the winning kind falls back to its own best
    candidate rather than to nothing: None sorts to the very end of the
    series, which is a worse place for a volume that merely labels its
    number differently than its neighbours do."""
    # Per item, per kind, the ONE value that item would actually get from
    # that kind -- not every candidate of it. That distinction is the whole
    # measurement: an item can produce several bare numbers (a year in the
    # title, then the index in the filename), and counting them all made
    # "bare" look more discriminating than it is. Scored on what each item
    # would really be numbered, a year repeated across the series collapses
    # to one distinct value and loses, which is the point.
    per_item: list[tuple[int, dict[str, float]]] = []
    for item_id, candidates in items:
        best: dict[str, float] = {}
        for candidate in candidates:
            best.setdefault(candidate.kind, candidate.value)
        per_item.append((item_id, best))

    coverage: dict[str, int] = {}
    distinct: dict[str, set[float]] = {}
    for _item_id, best in per_item:
        for kind, value in best.items():
            coverage[kind] = coverage.get(kind, 0) + 1
            distinct.setdefault(kind, set()).add(value)

    winner = None
    if coverage:
        winner = min(
            coverage,
            key=lambda k: (-coverage[k], -len(distinct[k]), _KIND_PRIORITY[k]),
        )

    resolved: dict[int, float | None] = {}
    for item_id, best in per_item:
        chosen = best.get(winner)
        if chosen is None and best:
            chosen = best[min(best, key=lambda k: _KIND_PRIORITY[k])]
        resolved[item_id] = chosen
    return SeriesNumbering(resolved, winner)
