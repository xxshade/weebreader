"""The display preferences the SERVER has to know before it renders.

Three settings have to be on the document BEFORE the first paint, not
after: a
theme applied late flashes the default surface, a thumbnail size applied
late paints the grid at the stylesheet's 140px first, and a chapter list
filtered late shows a screenful of read rows for a frame (measured, on the
novel page: 365/365 rows visible at the first animation frame).

They lived in `localStorage`, which the server cannot see, so each one
needed an inline `<script>` in `<head>` racing the parser. A cookie is sent
with the document request, so the server renders the attribute already
correct and no script is involved at all.

WHERE it renders them is the other half. The theme is the browser's
choice, so it goes on `<html>`; the other two are per PAGE, so they go on
`<main>` -- inside what an `hx-boost`-style navigation replaces, where
`<html>` and the whole of `<head>` are not (measured boosting home ->
/series/1: the grid stayed at the home page's 140px under its own key,
against 300px on a full load).

Everything here is attacker-controlled: it is whatever the request's own
cookie says, and it lands in an HTML attribute or a CSS custom property.
So each value is validated against what the UI can actually produce rather
than echoed back. That is cheap, and the alternative is a cookie of
`1px; background: url(...)` painting itself into every page.

The writer of all three is `static/js/ui-prefs.js`; the names have to agree
and a test pins that they do.
"""
from urllib.parse import unquote

THEME_COOKIE = "app-theme"
THUMB_COOKIE = "thumbnail-size"
UNREAD_COOKIE = "novel-chapters-unread-only"

# "dark" is the stylesheet's own default and is expressed as the ABSENCE of
# data-app-theme (see appearance.js), so it is a valid choice that renders
# nothing. Keep in step with the <select> in settings.html.
THEMES = frozenset({"dark", "amoled"})

# The slider's own bounds (_thumb_size_control.html: min=110 max=400) and
# the stylesheet's fallback, which is what "no inline style" already means.
THUMB_MIN = 110
THUMB_MAX = 400


def _cookie(request, name: str) -> str | None:
    """One cookie, by the name the WRITER meant, not the one that arrived.

    A cookie NAME is an RFC 6265 token, and `:` is a separator -- so the
    per-page thumbnail key `thumbnail-size:series` cannot be sent
    literally. ui-prefs.js percent-encodes it (as every cookie writer
    must) and it arrives as `thumbnail-size%3Aseries`; Starlette does not
    decode names, so a lookup by the literal string missed every time and
    the series page silently fell back to the HOME size. Measured on
    /series/1 with 300px chosen there and 260px on the home page: `<main>`
    came back from the server with no `style` at all and the grid jumped
    260px -> 300px the moment the deferred script caught up -- exactly the
    flash moving these to cookies was meant to end. The home key has no
    escapable character in it, which is why that page was always right and
    this went unnoticed.

    Decoded rather than re-encoded here: `encodeURIComponent` and Python's
    `quote` disagree about `!*'()`, and what arrived is not a guess.
    The exact name still wins, so a plain key costs nothing.

    The VALUE is decoded too, because the writer encodes both. None of the
    three holds an escapable character today, so that is symmetry rather
    than a fix -- and the validation below is what stands behind it either
    way, a decoded value being no more trusted than the one that arrived.
    """
    value = request.cookies.get(name)
    if value is None:
        value = next((sent_value for sent_name, sent_value in request.cookies.items()
                      if unquote(sent_name) == name), None)
    return unquote(value) if value is not None else None


def theme(request) -> str:
    """The value for `data-app-theme`, or "" when nothing should be set."""
    chosen = _cookie(request, THEME_COOKIE) or ""
    if chosen not in THEMES or chosen == "dark":
        return ""
    return chosen


def thumb_size(request, key: str) -> int | None:
    """The px for `--thumb-size` on a page whose own cookie is `key`.

    The first cookie that EXISTS wins, exactly as thumbnail-size.js reads
    it: a page never adjusted on its own inherits the home page's size
    rather than jumping back to the default. A present-but-unusable value
    returns None and the stylesheet's 140px stands, which is the same
    answer the script's `|| DEFAULT_SIZE` gives.
    """
    raw = _cookie(request, key)
    if raw is None:
        raw = _cookie(request, THUMB_COOKIE)
    if raw is None:
        return None
    try:
        px = int(raw)
    except ValueError:
        return None
    return px if THUMB_MIN <= px <= THUMB_MAX else None


def unread_only(request, novel_id: int) -> bool:
    """Whether this novel's chapter list opens filtered.

    Per novel, under `novel-chapters-unread-only:<id>`: which slice of a
    novel you want is a fact about that novel, the rule a series' filter
    already follows. It was one cookie for every novel, so turning it on
    for one opened all the others narrowed to what was unread. The writer
    only ever sets "1" and deletes the cookie to turn it off, so novels
    left unfiltered cost nothing on each request."""
    return _cookie(request, f"{UNREAD_COOKIE}:{novel_id}") == "1"


def register(templates) -> None:
    """Expose the three to templates, on one Jinja env.

    Called beside register_asset_versioning() in every router that builds
    its own Jinja2Templates. base.html calls ui_theme() and ui_thumb_size()
    on EVERY page, so an env that skips this raises at render time rather
    than quietly dropping the preference -- and a test walks the routers so
    the failure is found before a browser does.
    """
    templates.env.globals["ui_theme"] = theme
    templates.env.globals["ui_thumb_size"] = thumb_size
    templates.env.globals["ui_unread_only"] = unread_only
