"""Every class used in a template or in JS has a rule -- or is a declared hook.

`app/static/src/app.css` is frozen and has no generator, so a class with
no rule there silently does nothing: the markup looks right, the page
renders, and one thing is simply unstyled. That trap has been hit five
times (`shrink-0`, `transition-all`, `w-2`, `inline-flex`, `h-8`), each
time caught by eye in a browser. This makes it fail loudly instead.

The one legitimate case for a class with no rule is a HOOK -- a name that
exists for JS to find (`progress-fill`, `job-bar`, ...). Those are listed
below, and the list is checked in the other direction too: a hook nobody
looks up any more is dead markup, and the test says so.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLESHEETS = [ROOT / "app/static/src/app.css", ROOT / "app/static/src/theme.css"]
TEMPLATES = ROOT / "app/templates"
SCRIPTS = ROOT / "app/static/js"

# Classes with no rule ON PURPOSE: JS finds elements by them. Adding one
# here is a deliberate act; the second test below makes sure it stays true.
HOOK_CLASSES = frozenset({
    "clamp-text", "clamp-toggle",     # read-more.js: the text it measures and the button it reveals
    "fit-btn", "mode-btn",            # reader-comic.js: which button is active
    "job-bar", "job-counts", "job-current", "job-row",   # library-events.js: scan progress
    "progress-fill", "progress-label", "progress-remaining", "reading-eta",  # card updates
    "range-value",                    # range-value.js: the number beside a slider
    "read-toggle",                    # novel-reader.js
    "save-flash",                     # settings-save.js: the word a saved form flashes
    "series-card",                    # home.js / library-events.js: the sort+filter array
})

# At-rules whose braces hold MORE RULES rather than declarations, so what
# precedes the next brace is another selector and not a property list.
GROUPING_AT_RULES = ("@media", "@layer", "@supports", "@container", "@scope")


def defined_classes(css_text):
    """Class names appearing in selector position.

    Brace-aware rather than line-based: app.css is Tailwind's real output,
    where every rule sits inside `@layer utilities { ... }`. Tailwind also
    escapes the characters a class name cannot contain literally
    (`.hover\\:bg-neutral-700`, `.min-w-\\[140px\\]`), so the escapes are
    undone to get back the name as the template writes it.
    """
    css_text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
    names, stack, prelude = set(), [], ""
    for char in css_text:
        if char == "{":
            text = prelude.strip()
            is_group = text.startswith(GROUPING_AT_RULES)
            inside_declarations = bool(stack) and not stack[-1]
            if not is_group and not inside_declarations:
                for match in re.finditer(r"\.((?:[\w-]|\\.)+)", text):
                    names.add(re.sub(r"\\(.)", r"\1", match.group(1)))
            stack.append(is_group)
            prelude = ""
        elif char == "}":
            if stack:
                stack.pop()
            prelude = ""
        else:
            prelude += char
    return names


def _template_class_values(text):
    """The class="..." values of one template, with Jinja resolved enough.

    `{% if %}`/`{% endif %}` are dropped but what they wrap is kept, since
    that text IS classes. Inside `{{ ... }}` only string literals can be
    classes -- except the ones being COMPARED against, which are values
    ("sync_status == 'error'"), not names.
    """
    for match in re.finditer(r'class="([^"]*)"', text):
        value = match.group(1)
        value = re.sub(
            r"\{\{(.*?)\}\}",
            lambda m: " ".join(
                re.findall(r"['\"]([^'\"]*)['\"]",
                           re.sub(r"(==|!=|<=|>=|<|>|\bin\b)\s*['\"][^'\"]*['\"]", " ", m.group(1)))),
            value, flags=re.S)
        value = re.sub(r"\{%.*?%\}", " ", value, flags=re.S)
        yield value


def _js_class_names(text):
    """Class names a script names literally.

    add()/remove() take any number of class names; toggle()/contains()
    take one plus a condition, and that condition is full of strings that
    are not classes (`mode !== "single"`).
    """
    for match in re.finditer(r"classList\.(add|remove)\(([^)]*)\)", text):
        yield from re.findall(r"[\"'`]([^\"'`]+)[\"'`]", match.group(2))
    for match in re.finditer(r"classList\.(?:toggle|contains)\(\s*([^,)]*)", text):
        yield from re.findall(r"[\"'`]([^\"'`]+)[\"'`]", match.group(1))
    for match in re.finditer(r"className\s*=\s*[\"'`]([^\"'`]*)[\"'`]", text):
        yield from match.group(1).split()
    for match in re.finditer(r'class="([^"]*)"', text):
        yield from match.group(1).split()


# A `class="..."` inside a JS template literal can hold an interpolation
# (`class="badge ${BADGE_BG[card.status]}"`); those fragments are not
# class names, and no real class name contains any of these.
_NOT_A_CLASS = re.compile(r"[${}|]")


def used_classes():
    """{class name: {files that use it}} across templates and scripts."""
    used = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        for value in _template_class_values(path.read_text()):
            for name in value.split():
                if not _NOT_A_CLASS.search(name):
                    used.setdefault(name, set()).add(path.relative_to(ROOT))
    for path in sorted(SCRIPTS.rglob("*.js")):
        for name in _js_class_names(path.read_text()):
            if not _NOT_A_CLASS.search(name):
                used.setdefault(name, set()).add(path.relative_to(ROOT))
    return used


class ExtractorCase(unittest.TestCase):
    """The instrument itself.

    Without these, a regex that quietly stopped matching would leave the
    real test green and guarding nothing -- the failure mode this project
    has hit before with tests that reimplemented what they checked.
    """

    def test_it_reads_selectors_through_layers_and_escapes(self):
        css = """
        /* .in-a-comment {} */
        @layer utilities {
          .plain { color: red; }
          .hover\\:bg-neutral-700:hover { color: red; }
          .min-w-\\[140px\\] { min-width: 140px; }
          @media (width >= 48rem) {
            .nested, .also-nested > .child { color: red; }
          }
        }
        """
        self.assertEqual(
            defined_classes(css),
            {"plain", "hover:bg-neutral-700", "min-w-[140px]", "nested", "also-nested", "child"},
        )

    def test_it_does_not_read_declarations_as_selectors(self):
        # `content: ".not-a-class"` and a property value must not become names.
        self.assertEqual(defined_classes('.real { content: ".fake"; background: url(a.b); }'), {"real"})

    def test_it_resolves_the_jinja_a_template_actually_writes(self):
        values = list(_template_class_values(
            '''<div class="a {% if x %}b{% endif %} {{ 'c' if x else 'd' }}">'''
            '''<p class="e {{ 'f' if s == 'error' else 'g' }}">'''))
        self.assertEqual([sorted(v.split()) for v in values], [["a", "b", "c", "d"], ["e", "f", "g"]])

    def test_it_takes_class_names_and_not_conditions_from_js(self):
        names = set(_js_class_names(
            'el.classList.add("one", "two"); el.classList.toggle("three", mode !== "single");'
            'el.className = "four five"; row.innerHTML = \'<b class="six"></b>\';'))
        self.assertEqual(names, {"one", "two", "three", "four", "five", "six"})


class FrozenStylesheetCase(unittest.TestCase):
    def setUp(self):
        self.defined = set()
        for sheet in STYLESHEETS:
            self.defined |= defined_classes(sheet.read_text())
        self.used = used_classes()
        # Floors, so a broken extractor fails here instead of passing the
        # real assertions with two empty sets.
        self.assertGreater(len(self.defined), 200)
        self.assertGreater(len(self.used), 150)
        self.assertIn("thumb-grid", self.defined)
        self.assertIn("bg-neutral-900", self.defined)
        self.assertIn("thumb-grid", self.used)

    def test_every_class_used_has_a_rule_or_is_a_declared_hook(self):
        missing = {
            name: sorted(str(p) for p in files)
            for name, files in self.used.items()
            if name not in self.defined and name not in HOOK_CLASSES
        }
        self.assertEqual(missing, {}, "\n".join(
            ["classes with no rule in app.css/theme.css (add the rule, or list it in "
             "HOOK_CLASSES if JS is what looks it up):"]
            + [f"  {name} -- used in {', '.join(files)}" for name, files in sorted(missing.items())]))

    def test_every_declared_hook_is_still_looked_up_by_js(self):
        scripts = "\n".join(p.read_text() for p in SCRIPTS.rglob("*.js"))
        forgotten = [name for name in sorted(HOOK_CLASSES)
                     if not re.search(r"[\"'`.]" + re.escape(name) + r"\b", scripts)]
        self.assertEqual(forgotten, [], "hook classes no JS looks up any more -- dead markup")


if __name__ == "__main__":
    unittest.main()
