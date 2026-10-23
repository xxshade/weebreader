"""Rendering one status badge on its own, for the endpoint that changed it.

The badge is a <select> wearing the colour of the value it holds (see
templates/_status_badges.html), so the COLOUR is part of what it says --
and a <select> keeps the class it was rendered with until something
replaces it. The endpoints therefore answer an htmx request with the badge
itself rather than the 204 they used to, and the page swaps it in.

Going through the template's own macro is the point: the colour maps stay
in one place, in a template, where tests/test_css_classes.py can see the
literal class names. Assembling the same markup in Python here would put
a second copy of them somewhere that check cannot read.
"""
from fastapi.responses import HTMLResponse

from app.templating import templates as _templates

_macros = _templates.env.get_template("_status_badges.html").module


def tracking_badge(action: str, current, labels) -> HTMLResponse:
    return HTMLResponse(str(_macros.tracking_select(action, current, labels)))


def publication_badge(action: str, current, labels, empty_label=None,
                      source_word=None) -> HTMLResponse:
    return HTMLResponse(
        str(_macros.publication_select(action, current, labels, empty_label, source_word))
    )
