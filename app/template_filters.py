"""Jinja filters shared by more than one router.

Same shape as asset_versioning.register: a router builds its own
Jinja2Templates instance, so anything every template should be able to use
has to be installed on each one. Kept here rather than in whichever module
happened to need it first, so a second caller does not have to import from
another router to reach it.
"""

import json
import math


def format_duration(seconds) -> str:
    """A duration a person reads at a glance, not a precise one.

    The unit adapts to the size because the same filter renders both "how
    long does one page take" (seconds) and "how much longer will this book
    take" (hours) -- showing either in the other's unit is unreadable.
    Rounded rather than truncated, so 119s is "2m" and not "1m".
    """
    try:
        total = float(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    if total <= 0:
        return "0s"
    # Half up, the way library-events.js's Math.round does -- NOT Python's
    # round(), which rounds half to even: 150s was "2m" here and "3m" there,
    # so a card changed its wording when it was refreshed live (122 of the
    # first 14,400 seconds disagreed). test_duration_parity runs both.
    if total < 60:
        return f"{_round_half_up(total)}s"
    minutes = total / 60
    if minutes < 60:
        return f"{_round_half_up(minutes)}m"
    hours, rest = divmod(_round_half_up(minutes), 60)
    return f"{hours}h" if rest == 0 else f"{hours}h {rest}m"


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def format_number(value) -> str:
    """A volume or chapter number as a person writes it.

    They are stored as REAL because a chapter can be 45.5, so a whole one
    arrives as 7.0 and renders that way unless something says otherwise --
    and "volume 7.0 read" reads like a bug in the page. The fractional ones
    keep their fraction, since that IS the number: 45.5 is a real chapter.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return str(int(number)) if number == int(number) else f"{number:g}"


def embedded_metadata(value) -> dict:
    """`items.metadata` (a JSON object, or NULL) as a dict to read from.

    Anything else -- NULL, an unparseable string, a JSON value that is not
    an object -- is an item that says nothing about itself, which is what
    the card already shows for one.
    """
    try:
        decoded = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def register(templates) -> None:
    templates.env.filters["duration"] = format_duration
    templates.env.filters["number"] = format_number
    templates.env.filters["embedded_metadata"] = embedded_metadata
