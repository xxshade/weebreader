"""What the htmx 4 upgrade has to keep true.

The vendored library is htmx 4 (see app/static/src/vendor/README.md). Most
of that migration was mechanical -- attribute and event renames the
upgrade checker shipped in the tarball lists for you -- and none of the
mechanical part is what these pin. What they pin is the three things the
checker cannot see, each of which was found by driving the real app in a
browser and each of which fails SILENTLY:

1. `detail.successful` no longer exists. A listener renamed to the htmx 4
   event keeps reading it, gets `undefined`, and simply stops doing its
   job -- no error anywhere.
2. Every response now swaps, not just 2xx. This app answers a refusal with
   `raise HTTPException(...)`, which FastAPI renders as JSON, so the
   default would paint `{"detail": "..."}` into whatever was clicked.
3. htmx binds its listeners before this app's deferred scripts do, so a
   `submit` handler can no longer stop a request with
   stopImmediatePropagation. Measured: htmx 2 issued 0 requests for a
   searchable-select holding text that matched no option, htmx 4 issued 1.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
VENDOR = APP / "static/src/vendor/htmax.min.js"
VENDOR_README = APP / "static/src/vendor/README.md"
BASE = APP / "templates/base.html"
JS_DIR = APP / "static/js"


def code_of(path: Path) -> str:
    """The script with whole-line `//` comments blanked out.

    These are scans for CODE, and several of the comments below explain a
    rule by naming the thing it forbids -- prose that reads as a breach of
    the rule it is documenting.
    """
    return "\n".join("" if line.lstrip().startswith("//") else line
                     for line in path.read_text().splitlines())


class TheVendoredLibraryIsHtmx4(unittest.TestCase):
    def test_the_file_really_is_version_4(self):
        # Read out of the file itself, not out of the README beside it: the
        # README is a claim, the bundle is the thing that ships.
        source = VENDOR.read_text()
        version = re.search(r'version\s*[:=]\s*"([^"]+)"', source)
        self.assertIsNotNone(version, "no version string in the vendored bundle")
        self.assertTrue(version.group(1).startswith("4."),
                        f"vendored htmx is {version.group(1)}")

    def test_the_readme_agrees_with_the_file(self):
        readme = VENDOR_README.read_text()
        self.assertIn("**Version**: 4.0.0", readme)
        self.assertIn("htmax.min.js", readme)
        # The recorded sha256 is what makes the provenance checkable at all.
        import hashlib
        digest = hashlib.sha256(VENDOR.read_bytes()).hexdigest()
        self.assertIn(digest, readme,
                      "the README's sha256 does not match the vendored file")

    def test_it_is_the_fetch_rewrite_and_not_the_xhr_one(self):
        # htmx 4 is built on fetch(); htmx 2 was XMLHttpRequest. A bundle
        # that still says XMLHttpRequest is a 2.x file with a 4 in the
        # README.
        source = VENDOR.read_text()
        self.assertNotIn("XMLHttpRequest", source)


class ErrorResponsesAreNotSwapped(unittest.TestCase):
    """htmx 4 swaps every status but 204/304. Nothing here answers an
    error with HTML, so a 404 would put FastAPI's JSON on screen."""

    def test_the_config_meta_tag_is_there(self):
        html = BASE.read_text()
        self.assertIn('name="htmx-config"', html)
        content = re.search(r'name="htmx-config"\s+content=\'([^\']+)\'', html)
        self.assertIsNotNone(content, "htmx-config meta has no content attribute")
        config = json.loads(content.group(1))
        self.assertEqual(config["noSwap"], [204, 304, "4xx", "5xx"])

    def test_it_is_loaded_before_htmx_runs(self):
        # A config htmx has already read is a config that does nothing.
        html = BASE.read_text()
        self.assertLess(html.index('name="htmx-config"'), html.index("vendor/htmax.min.js"))

    def test_the_endpoints_it_protects_really_can_refuse(self):
        # The guard is only worth having while something can 4xx. If every
        # raise disappeared from the routers this test should be the thing
        # that says the meta tag is now decoration.
        raising = [p.name for p in (APP).rglob("*.py")
                   if "HTTPException(4" in p.read_text() or "HTTPException(5" in p.read_text()]
        self.assertTrue(raising, "no router raises a 4xx/5xx any more")


class NoScriptReadsTheOldEventDetail(unittest.TestCase):
    """`detail.successful` is gone in htmx 4 and reading it is falsy
    forever, which turns a feature off without an error."""

    def scripts(self):
        return {path: code_of(path) for path in sorted(JS_DIR.glob("*.js"))}

    def test_nothing_reads_detail_successful(self):
        for path, source in self.scripts().items():
            self.assertNotIn("detail.successful", source, f"{path.name} reads detail.successful")
            self.assertNotIn("successful", source, f"{path.name} mentions successful")

    def test_the_two_that_used_it_read_the_status_instead(self):
        # Named, because these are the two the browser found: the "Saved"
        # flash on the settings page and the add-a-rule form clearing
        # itself. Both look fine on inspection and both stop working.
        for name in ("settings-save.js", "text-replacements.js"):
            source = code_of(JS_DIR / name)
            self.assertIn("ctx?.response", source, f"{name} does not read the response status")
            self.assertIn("htmx:after:request", source)

    def test_no_htmx_2_event_names_survive(self):
        old = ("htmx:afterRequest", "htmx:afterSwap", "htmx:afterSettle", "htmx:load",
               "htmx:beforeRequest", "htmx:configRequest", "htmx:beforeSwap",
               "htmx:responseError", "htmx:sendError", "htmx:afterOnLoad")
        for path, source in self.scripts().items():
            for name in old:
                self.assertNotIn(name, source, f"{path.name} still listens for {name}")


class RemovedAttributesAndApis(unittest.TestCase):
    """The renames and removals from the upgrade guide, kept out for good.

    None of these is in the app today; the point is that adding one back
    would be writing against htmx 2's manual.
    """

    def sources(self):
        out = {}
        for path in list((APP / "templates").rglob("*.html")) + list(JS_DIR.glob("*.js")):
            out[path] = path.read_text()
        return out

    def test_no_removed_attributes(self):
        # hx-disable means something DIFFERENT in htmx 4 (it disables
        # elements during a request; stopping htmx is hx-ignore now), so it
        # is not in this list -- a future use of it would be legitimate.
        gone = ("hx-vars=", "hx-params=", "hx-disinherit=", "hx-inherit=",
                "hx-disabled-elt=", "hx-request=", "hx-ext=", 'hx-history="false"')
        for path, source in self.sources().items():
            for attr in gone:
                self.assertNotIn(attr, source, f"{path.name} uses {attr}")

    def test_no_removed_js_api(self):
        gone = ("htmx.defineExtension", "htmx.addClass", "htmx.removeClass",
                "htmx.toggleClass", "htmx.takeClass", "htmx.off(", "htmx.values(")
        for path, source in self.sources().items():
            for call in gone:
                self.assertNotIn(call, source, f"{path.name} calls {call}")

    def test_no_removed_response_headers(self):
        for path in (APP).rglob("*.py"):
            source = path.read_text()
            for header in ("HX-Trigger-After-Swap", "HX-Trigger-After-Settle"):
                self.assertNotIn(header, source, f"{path.name} sends {header}")


class NothingStopsHtmxByListenerOrder(unittest.TestCase):
    """The regression that cost the most to find.

    searchable-select.js blocked an invalid submit with preventDefault plus
    stopImmediatePropagation, which only ever worked because its listener
    was registered before htmx's. htmx 4 binds first, so the request went
    out and the validation message was set on an input the user never saw.

    The replacement is native constraint validation kept current on every
    change: htmx 4 calls form.reportValidity() before issuing a request and
    gives up when it fails, so an already-invalid input stops it without
    anything here knowing htmx exists.
    """

    def test_no_script_tries_to_out_order_htmx(self):
        for path in sorted(JS_DIR.glob("*.js")):
            self.assertNotIn("stopImmediatePropagation", code_of(path),
                             f"{path.name} depends on beating another listener to an event")

    def test_the_combobox_keeps_its_validity_current(self):
        source = code_of(JS_DIR / "searchable-select.js")
        self.assertIn("function syncValidity()", source)
        # Every path that can change what the box says has to re-ask, or
        # the state and the validity drift apart.
        self.assertGreaterEqual(source.count("syncValidity()"), 5)
        self.assertIn('input.setCustomValidity("Pick one from the list.")', source)

    def test_it_no_longer_decides_at_submit_time(self):
        source = code_of(JS_DIR / "searchable-select.js")
        self.assertNotIn('addEventListener("submit"', source)


if __name__ == "__main__":
    unittest.main()
