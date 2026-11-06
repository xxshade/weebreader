"""Daily activity range resolution shared by requests and preparation."""
from datetime import date, timedelta

from fastapi import HTTPException
from app.stats import queries


MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _format_day(day: date) -> str:
    return f"{MONTH_ABBR[day.month - 1]} {day.day}, {day.year}"


def resolve_window(days: int | None, start: str | None, end: str | None) -> dict:
    """What window the daily charts should cover, from the query string.

    Two shapes, and the explicit one wins: `from`/`to` are a custom range,
    anything else is the last `days` days. Returns the resolved dates, the
    preset that produced them (None for a custom range, which is what the
    control highlights), and the label the heading shows.

    A date that does not parse is a 400 rather than a silent fall back to
    the default: the chart would then be answering a different question
    than the URL asks, and there is no way to see that from the page.
    Everything else is CLAMPED instead, because those are all questions
    with a sensible nearest answer -- a range running into the future ends
    today (there is no reading yet to draw), a backwards range is the same
    range read the other way round, and one longer than MAX_WINDOW_DAYS is
    cut at its start, keeping the recent end the reader asked from.
    """
    today = date.today()
    if start or end:
        try:
            first = date.fromisoformat(start) if start else today
            last = date.fromisoformat(end) if end else today
        except ValueError:
            raise HTTPException(400, "Expected ISO dates (YYYY-MM-DD)")
        if first > last:
            first, last = last, first
        last = min(last, today)
        first = min(first, last)
        first = max(first, last - timedelta(days=queries.MAX_WINDOW_DAYS - 1))
        return {"start": first, "end": last, "preset": None,
                "label": f"{_format_day(first)} – {_format_day(last)}"}

    window = days or queries.DAILY_WINDOW_DAYS
    window = max(1, min(window, queries.MAX_WINDOW_DAYS))
    return {"start": today - timedelta(days=window - 1), "end": today,
            "preset": window if window in queries.DAILY_WINDOW_PRESETS else None,
            "label": f"Last {window} days"}

