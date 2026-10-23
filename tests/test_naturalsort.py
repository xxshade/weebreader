"""Volume/chapter numbering and natural ordering (app/library/naturalsort.py).

Every case here is one that was either reported as wrong ordering or found
while fixing one. Keeping them is what makes the extraction safe to change:
it is regex-driven pattern matching over messy third-party filenames, so the
only way to know a "small improvement" didn't break another format is to
re-run all of them.

Migrated from scripts/test_numbering.py, which this replaces.
"""
import unittest

from app.library.naturalsort import (
    extract_candidates,
    extract_number_in_series,
    natural_sort_key,
    resolve_series_numbers,
    roman_to_int,
)


def series_order(files: list[tuple[str, str | None]]) -> list[str]:
    """Runs the real resolution over a whole series and returns the titles in
    the order the series page would show them: the same ORDER BY the database
    uses -- numbered items first, then by number, ties broken by sort_title,
    and items with no number last."""
    items = [(i, extract_candidates(title, filename)) for i, (title, filename) in enumerate(files)]
    numbers = resolve_series_numbers(items).numbers
    rows = [(files[i][0], numbers[i], natural_sort_key(files[i][0])) for i, _ in items]
    rows.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0.0, r[2]))
    return [r[0] for r in rows]


def numbers_for(files: list[tuple[str, str | None]]) -> dict[int, float | None]:
    items = [(i, extract_candidates(t, f)) for i, (t, f) in enumerate(files)]
    return resolve_series_numbers(items).numbers


def kind_for(files: list[tuple[str, str | None]]) -> str | None:
    items = [(i, extract_candidates(t, f)) for i, (t, f) in enumerate(files)]
    return resolve_series_numbers(items).kind


class RomanNumerals(unittest.TestCase):
    def test_well_formed(self):
        for text, expected in [
            ("I", 1), ("IV", 4), ("IX", 9), ("XII", 12), ("XL", 40),
            ("MCMXCIV", 1994), ("iii", 3),
        ]:
            with self.subTest(text=text):
                self.assertEqual(roman_to_int(text), expected)

    def test_malformed_and_empty_are_rejected(self):
        for text in ["", "Serie", "IIII", "VV"]:
            with self.subTest(text=text):
                self.assertIsNone(roman_to_int(text))


class NaturalSortKey(unittest.TestCase):
    def test_embedded_numbers_compare_numerically(self):
        # The bug this exists for: plain string ordering puts "Volume 10"
        # before "Volume 2" because "1" < "2" character by character.
        titles = ["Volume 10", "Volume 2", "Volume 1"]
        self.assertEqual(
            sorted(titles, key=natural_sort_key),
            ["Volume 1", "Volume 2", "Volume 10"],
        )

    def test_zero_padded_and_bare_numbers_agree(self):
        # A library mixing "1.jpg" and "001.jpg" must not split into two runs.
        self.assertEqual(
            sorted(["10.jpg", "2.jpg", "1.jpg"], key=natural_sort_key),
            ["1.jpg", "2.jpg", "10.jpg"],
        )

    def test_case_insensitive(self):
        self.assertEqual(natural_sort_key("Serie A"), natural_sort_key("serie a"))


class SingleItemExtraction(unittest.TestCase):
    """One file on its own, with no series to compare it against."""

    def test_shapes(self):
        for text, expected in [
            ("XXXXXXXYYYYYSSSSS123V12", 12.0),
            ("Serie A v03", 3.0),
            ("Series Name 03", 3.0),
            ("Volume 10", 10.0),
            ("Serie Vol. IV", 4.0),
            ("Livro III - Subtitulo", 3.0),
            ("Serie III", 3.0),
            ("Nome da Serie", None),
            # chapter beats volume when the file is judged in isolation
            ("[Grupo] Serie - c012 (v02) [scan]", 12.0),
            # A bracketed group is a tag. Reported from a real library:
            # the year in the first one was read as the chapter, and sent
            # to both trackers as "2026 chapters read".
            ("Nome da Serie 032 (2026) (Digital) (Grupo)", 32.0),
            ("Nome da Serie 032 [2026] {Grupo}", 32.0),
            ("Nome da Serie III (2026)", 3.0),
            # With no number outside a tag, a tag may still hold the index,
            # but never as a year.
            ("Nome da Serie (01)", 1.0),
            ("Nome da Serie (2019) (Digital)", None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(extract_number_in_series(text), expected)


class SeriesWideResolution(unittest.TestCase):
    """The number is decided per SERIES, not per file: which marker orders the
    collection depends on whether the library stores one file per chapter or
    one per volume, and a single file cannot know that."""

    def test_chapter_per_file_beats_the_repeated_volume(self):
        files = [(f"[Grupo] Serie - c{n:03d} (v{(n - 1) // 5 + 1:02d}) [scan]", None)
                 for n in (1, 4, 5, 6, 9, 10, 11, 14)]
        self.assertEqual(
            sorted(numbers_for(files).values()),
            [1.0, 4.0, 5.0, 6.0, 9.0, 10.0, 11.0, 14.0],
        )

    def test_one_file_missing_its_volume_tag(self):
        # Regression: this used to read the volume for the tagged files
        # (1, 2, ...) and the bare trailing number for the untagged one (10),
        # so c010 jumped to the end of the series.
        files = [(f"[Grupo] Serie - c{n:03d} (v{(n - 1) // 5 + 1:02d}) [scan]", None)
                 for n in (1, 4, 5, 6, 9)]
        files.append(("[Grupo] Serie - c010 [scan]", None))
        self.assertEqual(series_order(files), [
            "[Grupo] Serie - c001 (v01) [scan]",
            "[Grupo] Serie - c004 (v01) [scan]",
            "[Grupo] Serie - c005 (v01) [scan]",
            "[Grupo] Serie - c006 (v02) [scan]",
            "[Grupo] Serie - c009 (v02) [scan]",
            "[Grupo] Serie - c010 [scan]",
        ])

    def test_volume_per_file_with_no_chapter_marker(self):
        self.assertEqual(
            series_order([(f"Serie v{n:02d}", None) for n in (3, 1, 10, 2)]),
            ["Serie v01", "Serie v02", "Serie v03", "Serie v10"],
        )

    def test_epub_whose_dc_title_never_names_the_volume(self):
        # The EPUB parser's title is dc:title, and real EPUBs routinely title
        # every volume identically -- so all three used to come out with
        # number=None AND an identical sort_title, leaving the order up to
        # whatever the database returned first. The volume is in the filename.
        files = [("Nome da Serie", "Nome da Serie v03.epub"),
                 ("Nome da Serie", "Nome da Serie v01.epub"),
                 ("Nome da Serie", "Nome da Serie v02.epub")]
        self.assertEqual(numbers_for(files), {0: 3.0, 1: 1.0, 2: 2.0})

    def test_a_year_in_the_title_does_not_become_the_index(self):
        files = [("Trilogia (2020)", "Trilogia v01.epub"),
                 ("Trilogia (2020)", "Trilogia v02.epub"),
                 ("Trilogia (2020)", "Trilogia v03.epub")]
        self.assertEqual(numbers_for(files), {0: 1.0, 1: 2.0, 2: 3.0})

    def test_a_year_tag_on_every_chapter_does_not_become_the_index(self):
        # The reported case, as a series: every file carries the same year
        # tag after its chapter number and no marker at all. The index is
        # the unmarked number before the tags.
        files = [(f"Nome da Serie {n:03d} (2026) (Digital) (Grupo)", None)
                 for n in (31, 32, 33)]
        self.assertEqual(numbers_for(files), {0: 31.0, 1: 32.0, 2: 33.0})

    def test_roman_numbered_volumes_order_past_eight(self):
        # Alphabetical order is wrong from IX on: I II III IV IX V VI VII VIII
        romans = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]
        self.assertEqual(
            series_order([(f"Serie Vol. {r}", None) for r in reversed(romans)]),
            [f"Serie Vol. {r}" for r in romans],
        )

    def test_mixed_series_is_decided_by_coverage(self):
        # A folder holding both chapter files and volume files is genuinely
        # contradictory -- no numbering satisfies both. The rule is coverage
        # first (use the marker present on the most files), which is what
        # keeps the REAL cases right. Pinned here so the tie-break cannot be
        # changed without noticing it moves this case too; a series this
        # ambiguous is what the drag-and-drop manual order is for.
        files = [(f"[Grupo] Serie - c{n:03d} (v{(n - 1) // 5 + 1:02d}) [scan]", None) for n in (1, 6)]
        files += [("Serie v03", None), ("Serie v04", None)]
        self.assertEqual(numbers_for(files), {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0})

    def test_an_unnumbered_extra_sorts_last(self):
        self.assertEqual(
            series_order([("Serie v02", None), ("Serie - Extras", None), ("Serie v01", None)]),
            ["Serie v01", "Serie v02", "Serie - Extras"],
        )

    def test_empty_series_resolves_to_nothing(self):
        self.assertEqual(resolve_series_numbers([]), ({}, None))



class TheKindItPicked(unittest.TestCase):
    """`resolve_series_numbers` always decided this; it used to throw it
    away. It is stored on `series.number_kind` now because a tracker has
    to know whether "7" means seven chapters or seven volumes -- the two
    are separate counts on both MangaUpdates and MyAnimeList, so guessing
    puts the number in the wrong field for half a library."""

    def test_one_file_per_volume_is_volumes(self):
        self.assertEqual(kind_for([("Serie v01", None), ("Serie v02", None)]), "volume")

    def test_one_file_per_chapter_is_chapters(self):
        self.assertEqual(
            kind_for([("Serie c001", None), ("Serie c002", None), ("Serie c003", None)]),
            "chapter",
        )

    def test_chapters_win_over_the_volume_they_sit_in(self):
        # The case the whole resolver exists for: both markers cover every
        # file, but the volume repeats and the chapter does not.
        self.assertEqual(
            kind_for([
                ("Serie - c012 (v02)", None),
                ("Serie - c013 (v02)", None),
                ("Serie - c014 (v03)", None),
            ]),
            "chapter",
        )

    def test_a_series_with_no_markers_at_all_has_no_kind(self):
        self.assertIsNone(kind_for([("Some Book", None), ("Another Book", None)]))

    def test_an_empty_series_has_no_kind(self):
        self.assertIsNone(resolve_series_numbers([]).kind)


if __name__ == "__main__":
    unittest.main()
