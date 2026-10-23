"""Per-series / per-novel find-and-replace.

The rules themselves are trivial rows; what is worth pinning is the shape
they reach the reader in (a valid JSON array, in a stable order) and the
two ways a target can be named, since both come from a submitted form.
"""
import json
import tempfile
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app import text_replacements
from app.config import settings
from app.database import get_connection, init_db


class ReplacementCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        await init_db()
        self.db = await get_connection()
        self.addCleanup(self._close)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x')")
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (2, 'v', 'x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'B', '/b', 'ebook')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path) "
            "VALUES (1, 1, 'Serie', 'serie', 'Serie')")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url) VALUES (1, 'Novel', 'https://x/1')")
        await self.db.commit()

    async def _close(self):
        await self.db.close()

    async def add(self, find, replace, *, user_id=1, series_id=None, novel_id=None):
        await self.db.execute(
            "INSERT INTO text_replacements (user_id, series_id, novel_id, find_text, replace_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, series_id, novel_id, find, replace),
        )
        await self.db.commit()


class Rules(ReplacementCase):
    async def test_a_series_rule_and_a_novel_rule_do_not_leak_into_each_other(self):
        await self.add("A", "B", series_id=1)
        await self.add("C", "D", novel_id=1)
        self.assertEqual(await text_replacements.rules_for(self.db, 1, series_id=1),
                         [{"find": "A", "replace": "B"}])
        self.assertEqual(await text_replacements.rules_for(self.db, 1, novel_id=1),
                         [{"find": "C", "replace": "D"}])

    async def test_another_users_rules_are_not_applied(self):
        await self.add("A", "B", user_id=2, series_id=1)
        self.assertEqual(await text_replacements.rules_for(self.db, 1, series_id=1), [])

    async def test_rules_come_back_in_the_order_they_were_added(self):
        # The reader applies them in sequence, each seeing the previous
        # one's output, so the order is part of what a rule MEANS -- and it
        # has to be the same order the settings list shows.
        await self.add("one", "two", series_id=1)
        await self.add("two", "three", series_id=1)
        self.assertEqual([r["find"] for r in await text_replacements.rules_for(self.db, 1, series_id=1)],
                         ["one", "two"])

    async def test_the_reader_always_gets_a_parseable_array(self):
        # #reader-root's data-replacements is parsed unconditionally; an
        # empty attribute is not JSON.
        self.assertEqual(json.loads(await text_replacements.rules_json(self.db, 1, series_id=1)), [])
        await self.add("A", "B", series_id=1)
        self.assertEqual(json.loads(await text_replacements.rules_json(self.db, 1, series_id=1)),
                         [{"find": "A", "replace": "B"}])

    async def test_no_target_means_no_rules_rather_than_every_rule(self):
        # A comic has no series-wide text to substitute and an item with no
        # series has no id to match; returning everything would apply one
        # book's rules to another. Pins the behaviour, not the guard --
        # SQL's `= NULL` matches nothing on its own too.
        await self.add("A", "B", series_id=1)
        self.assertEqual(await text_replacements.rules_for(self.db, 1, series_id=None), [])

    async def test_the_settings_list_names_what_each_rule_applies_to(self):
        await self.add("A", "B", series_id=1)
        await self.add("C", "D", novel_id=1)
        listed = await text_replacements.list_all(self.db, 1)
        self.assertEqual({(r["find_text"], r["target_name"]) for r in listed},
                         {("A", "Serie"), ("C", "Novel")})

    async def test_the_row_keys_survive_a_template_attribute_lookup(self):
        # Jinja resolves an attribute with getattr BEFORE the mapping, and
        # `str.replace` exists -- a key named "replace" would render as a
        # bound method, silently. Same trap the finish-time keys were named
        # around.
        await self.add("A", "B", series_id=1)
        for key in (await text_replacements.list_all(self.db, 1))[0]:
            self.assertFalse(hasattr("", key), f"{key!r} collides with a str attribute")


class ParseTarget(unittest.TestCase):
    def test_it_reads_both_kinds(self):
        self.assertEqual(text_replacements.parse_target("series:12"), (12, None))
        self.assertEqual(text_replacements.parse_target("novel:3"), (None, 3))

    def test_anything_else_names_nothing(self):
        # The value arrives from a submitted form, so it can be anything;
        # the caller rejects (None, None) rather than inserting a row that
        # violates the table's own CHECK.
        for raw in ["", "series:", "series:x", "nope:1", "12", None, "series:1:2"]:
            self.assertEqual(text_replacements.parse_target(raw), (None, None), raw)


class TheRenderedList(unittest.TestCase):
    """The count and the height cap, which live INSIDE the swap target.

    Adding, editing and removing a rule each replace `#replacement-rules`
    with a freshly rendered copy of this partial and nothing else on the
    page, so a count rendered in settings.html would go stale on the first
    add and a cap applied out there would never switch on when the list
    grew past the threshold without a reload. Rendering the partial on its
    own is exactly what those endpoints do.
    """

    @staticmethod
    def render(count):
        env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
        rules = [{"id": i, "find_text": f"a{i}", "replace_text": f"b{i}",
                  "target_name": "Novel"} for i in range(count)]
        return env.get_template("_replacement_rules.html").render(replacement_rules=rules)

    def test_it_counts_the_rules_it_rendered(self):
        self.assertIn("7 rules", self.render(7))
        self.assertIn("14 rules", self.render(14))

    def test_one_rule_is_not_called_rules(self):
        html = self.render(1)
        self.assertIn("1 rule", html)
        self.assertNotIn("1 rules", html)

    def test_an_empty_list_says_nothing_rather_than_zero_rules(self):
        self.assertNotIn("0 rule", self.render(0))

    def test_a_short_list_is_not_capped(self):
        self.assertNotIn("list-scroll-cap", self.render(6))

    def test_a_long_one_is(self):
        self.assertIn("list-scroll-cap", self.render(7))

    def test_the_cap_is_on_the_list_and_not_on_the_swap_target(self):
        # Capping #replacement-rules itself would scroll the count line out
        # of view with the rules, and htmx replaces that element outright.
        html = self.render(14)
        target = html[html.index('id="replacement-rules"'):]
        self.assertNotIn("list-scroll-cap", target[:target.index(">")])
