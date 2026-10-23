"""Small request-shaped helpers shared across routers.

Lives in its own module rather than in one of the routers so both can use
it: library/router.py imports novels/router.py (for the settings page's
"Web Novels" section and the novel cards on the home grid), so anything
novels/router.py imported back from library/router.py would be a cycle.
"""

from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse


# Headers for bytes the app serves but did not write: a file inside an EPUB,
# a novel cover as a source site sent it. Served from this origin, such a
# file opened as a page (an EPUB's own `target="_blank"` link, or its URL
# typed in) ran its scripts with the reader's session -- measured, both
# browsers: a book's page fetched /settings/security and got 200. `sandbox`
# without `allow-scripts` runs nothing and gives the page an opaque origin;
# the reader is unaffected, because it fetch()es chapters into its own
# sandboxed srcdoc iframe, where a response's CSP does not apply. Sent on a
# 304 too: a revalidation updates the headers the browser has stored.
UNTRUSTED_CONTENT_HEADERS = {
    "Content-Security-Policy": "sandbox",
    "X-Content-Type-Options": "nosniff",
}


def is_htmx(request: Request) -> bool:
    """Whether this request came from htmx rather than a plain browser
    navigation -- routers use it to answer with a partial to swap in place
    instead of a full redirect."""
    return request.headers.get("hx-request") == "true"


def safe_redirect_target(candidate: str | None) -> str | None:
    """A `return_to`/`next` form field, accepted only if it is a path inside
    this app -- the same open-redirect guard as auth/router.py's
    `_safe_next_path`. Never a full URL, and never a protocol-relative
    "//evil.com", which a browser reads as another origin.

    Here rather than in a router for the same reason `is_htmx` is: the
    reader's mark-read forms and the novel page's sync buttons both post a
    place to come back to, and they live on opposite sides of an import
    that cannot be made circular.

    Nor a backslash or a control character: browsers read "/\\evil.com" as
    "//evil.com" (measured: `new URL("/\\evil.com", origin)` is
    https://evil.com/ in Chromium and Firefox).
    """
    if (candidate and candidate.startswith("/") and not candidate.startswith("//")
            and "\\" not in candidate
            and not any(ord(c) < 0x20 or ord(c) == 0x7f for c in candidate)):
        return candidate
    return None



def same_app_path(url: str | None) -> str | None:
    """The path and query of `url` -- the page in this app to come back
    to -- or None when it is not a path this app may redirect to. The
    host is dropped, so a full URL naming another site lands here."""
    if not url:
        return None
    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return safe_redirect_target(path)


def back_target(request: Request, fallback: str) -> str:
    """The page a plain form POST came from (its Referer, as a path of this
    app), else `fallback`. Sixteen routes used to redirect to the raw
    header."""
    return same_app_path(request.headers.get("referer")) or fallback


def redirect_back(request: Request, fallback: str) -> RedirectResponse:
    return RedirectResponse(back_target(request, fallback), status_code=302)

# ---------- Values taken from a request body ----------
#
# The readers POST their own progress as JSON, and until these existed the
# values went from the wire into the database untouched. SQLite is
# dynamically typed, so a non-numeric percentage was not rejected by the
# REAL column -- it was STORED, as text. Every screen that renders a
# percentage then raised on it, and the home page and the "Reading" strip
# answered 500 until the row was repaired by hand. Measured exactly that
# way: one POST of {"progress_percent": "abc"} and `GET /` was 500 from
# then on, while /stats and /settings stayed up, which is what made it look
# like a rendering bug rather than a write that should never have landed.
#
# So the rule is: a value that decides what is stored is checked HERE, at
# the boundary, and a bad one is a 400 naming the field rather than a 500
# somewhere downstream -- or, worse, a row that poisons a page.

def json_object(body) -> dict:
    """The parsed JSON body, which must be an object.

    `await request.json()` raises on a malformed body, and a valid JSON
    body can still be a string or a list -- both of which reach `.get`
    as an AttributeError. Callers pass what they parsed (inside their own
    try/except for the parse itself).
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    return body


def percent(value, field: str) -> float:
    """A 0-100 percentage. Clamped, not rejected, at the edges.

    Clamping is right here and rejecting is right for `ago` in
    stats/recording.py, and the difference is what the value decides: a
    percentage out of range is a miscount of something that did happen, so
    the nearest sensible answer is the true one, while `ago` decides WHICH
    DAY a session is filed under and has no nearest sensible answer.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(400, f"{field} must be a number")
    return min(100.0, max(0.0, float(value)))


def optional_index(value, field: str) -> int | None:
    """A non-negative position (a page, a paragraph), or nothing at all."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(400, f"{field} must be an integer")
    return max(0, value)


def one_of(value, allowed, field: str) -> str:
    """A value from a fixed vocabulary -- the CHECK constraint, enforced
    where a bad value can still be answered with a 400.

    Reaching the constraint instead produces an IntegrityError, which is a
    500: the right outcome for the database (the row is refused) arriving
    in the wrong shape for the client.
    """
    if value not in allowed:
        raise HTTPException(400, f"{field} must be one of: {', '.join(sorted(allowed))}")
    return value
