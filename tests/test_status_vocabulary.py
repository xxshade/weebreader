"""The status vocabularies are defined once and repeated five times.

`status_labels.py` holds the labels, but the same set of VALUES is also
written out in schema.sql's CHECK constraints, in the colour-slot map on
the statistics page, and in the badge-colour maps -- twice, once in Jinja
and once in JS, because the frozen stylesheet needs every class name
written out literally (see AGENTS.md).

None of that is accidental duplication that a lookup table would remove:
what is stored is a stable CODE and the label lives in Python, which is
why renaming "Completed" to "Finished" was a one-line diff and no data
migration. A status is not a row of data either -- each one carries
behaviour in code (a colour slot, a badge colour, whether the home page
hides it), and a row in a table cannot carry that.

What the duplication really costs is this: adding a status to the dict
would be ACCEPTED by an upgraded database and REJECTED by a fresh one
(SQLite cannot add a CHECK by ALTER TABLE, so the constraint only exists
where schema.sql created the table), and it would render with no colour
in two places. Nothing said so. This does.
"""
import re
import unittest
from pathlib import Path

from app.stats.router import ITEM_STATUS_LABELS, STATUS_VIZ_SLOT
from app.status_labels import PUBLICATION_STATUS_LABELS, STATUS_LABELS

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "app/schema.sql").read_text()
# The two badge colour maps moved out of the home template when a
# series' and a novel's own page started wearing the same badge: one
# place in Jinja, and still literal class names in a TEMPLATE so
# test_css_classes.py can see them.
BADGES_TEMPLATE = (ROOT / "app/templates/_status_badges.html").read_text()
EVENTS_JS = (ROOT / "app/static/js/library-events.js").read_text()


def check_values(schema_text, table, column):
    """The values a column's CHECK(... IN (...)) allows, or None."""
    block = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
                      schema_text, re.S)
    if not block:
        return None
    listed = re.search(rf"CHECK\({column} IN \(\s*(.*?)\s*\)\s*\)", block.group(1), re.S)
    if not listed:
        return None
    return frozenset(re.findall(r"'([^']+)'", listed.group(1)))


def literal_map(text, marker):
    """The {key: value} pairs of an object/dict literal following `marker`.

    Reads both the Jinja/Python form ('key': 'value') and the JS one
    (key: "value"), because the badge colours exist in both.
    """
    start = text.index(marker) + len(marker)
    depth, end = 0, None
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = text[start:end]
    return {k: v for k, v in re.findall(
        r"['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*:\s*['\"]([^'\"]+)['\"]", body)}


class SchemaAgreesWithTheLabels(unittest.TestCase):
    def test_the_series_tracking_status_column(self):
        self.assertEqual(check_values(SCHEMA, "series_status", "status"),
                         frozenset(STATUS_LABELS))

    def test_the_novel_tracking_status_column(self):
        # Its own table rather than a widened series_status, so its own
        # CHECK -- and one that has to stay in step with the other.
        self.assertEqual(check_values(SCHEMA, "novel_status", "status"),
                         frozenset(STATUS_LABELS))

    def test_the_publication_status_column(self):
        self.assertEqual(check_values(SCHEMA, "series", "publication_status"),
                         frozenset(PUBLICATION_STATUS_LABELS))

    def test_the_per_item_read_state(self):
        # A third vocabulary, and deliberately not the tracking one: an
        # item is unread/reading/completed, with no "plan to read".
        self.assertEqual(check_values(SCHEMA, "reading_progress", "status"),
                         frozenset(ITEM_STATUS_LABELS))


class EveryStatusIsDrawable(unittest.TestCase):
    """A status with no colour renders as the fallback grey, silently --
    the same class of failure the frozen stylesheet has, one level up."""

    def test_the_statistics_chart_has_a_slot_for_each(self):
        missing = set(STATUS_LABELS) - set(STATUS_VIZ_SLOT)
        self.assertEqual(missing, set())

    def test_the_badge_has_a_colour_for_each_tracking_status(self):
        self.assertEqual(set(literal_map(BADGES_TEMPLATE, "{% set badge_bg =")),
                         set(STATUS_LABELS))

    def test_the_badge_has_a_colour_for_each_publication_status(self):
        self.assertEqual(set(literal_map(BADGES_TEMPLATE, "{% set pub_bg =")),
                         set(PUBLICATION_STATUS_LABELS))

    def test_the_card_built_in_js_uses_the_same_colours(self):
        # A card arriving live over SSE is built in JS, not by Jinja, so
        # the map exists twice and a colour added to one and not the other
        # shows up only after a reload.
        self.assertEqual(literal_map(EVENTS_JS, "const BADGE_BG ="),
                         literal_map(BADGES_TEMPLATE, "{% set badge_bg ="))
        self.assertEqual(literal_map(EVENTS_JS, "const PUBLICATION_BADGE_BG ="),
                         literal_map(BADGES_TEMPLATE, "{% set pub_bg ="))


class TheTwoVocabulariesStayApart(unittest.TestCase):
    def test_they_are_not_the_same_list(self):
        # They share exactly one value, `completed`, and read differently
        # on screen for it. If they ever became the same set, one of them
        # has been edited into the other by mistake.
        self.assertEqual(set(STATUS_LABELS) & set(PUBLICATION_STATUS_LABELS),
                         {"completed"})
        self.assertNotEqual(STATUS_LABELS["completed"],
                            PUBLICATION_STATUS_LABELS["completed"])


class TheExtractorsThemselves(unittest.TestCase):
    """Tested against inline samples for the same reason
    test_css_classes.py tests its own: a regex that quietly stopped
    matching would leave every test above green and guarding nothing."""

    SAMPLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS thing_status (
    user_id         INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK(status IN (
                        'alpha', 'beta'
                    )),
    other           TEXT NOT NULL CHECK(other IN ('x'))
);

CREATE TABLE IF NOT EXISTS plain (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL DEFAULT 'a' CHECK(kind IN ('a','b','c')),
    free            TEXT
);
"""

    def test_it_reads_a_multi_line_check(self):
        self.assertEqual(check_values(self.SAMPLE_SCHEMA, "thing_status", "status"),
                         frozenset({"alpha", "beta"}))

    def test_it_reads_a_single_line_check_and_the_right_column(self):
        self.assertEqual(check_values(self.SAMPLE_SCHEMA, "plain", "kind"),
                         frozenset({"a", "b", "c"}))
        self.assertEqual(check_values(self.SAMPLE_SCHEMA, "thing_status", "other"),
                         frozenset({"x"}))

    def test_it_says_none_rather_than_empty_when_there_is_no_check(self):
        # An empty set would silently equal a vocabulary that had been
        # emptied; None cannot be mistaken for one.
        self.assertIsNone(check_values(self.SAMPLE_SCHEMA, "plain", "free"))
        self.assertIsNone(check_values(self.SAMPLE_SCHEMA, "missing", "status"))

    def test_it_reads_a_jinja_map(self):
        self.assertEqual(
            literal_map("""{% set badge_bg = {
                    'reading': 'bg-indigo-600',
                    'on_hold': 'bg-neutral-600',
                }.get(card.status, 'bg-neutral-600') %}""", "{% set badge_bg ="),
            {"reading": "bg-indigo-600", "on_hold": "bg-neutral-600"})

    def test_it_reads_a_js_map(self):
        self.assertEqual(
            literal_map("""const BADGE_BG = {
                    reading: "bg-indigo-600",
                    on_hold: "bg-neutral-600",
                };""", "const BADGE_BG ="),
            {"reading": "bg-indigo-600", "on_hold": "bg-neutral-600"})

    def test_it_stops_at_the_end_of_the_literal(self):
        # Not at the first brace it meets on the way out, and not at the
        # next map in the file.
        text = """const A = { x: "1" };\nconst B = { y: "2" };"""
        self.assertEqual(literal_map(text, "const A ="), {"x": "1"})
        self.assertEqual(literal_map(text, "const B ="), {"y": "2"})
