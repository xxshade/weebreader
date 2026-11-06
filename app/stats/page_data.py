"""Only the aggregates rendered by Statistics; shared query contracts stay intact."""
from datetime import date

from app import request_timing
from app.stats import queries

PHASES = {
    'daily_activity': 'stats_daily',
    'seconds_per_page_by_kind': 'stats_by_kind',
    'text_pace': 'stats_pace',
    'recent_items': 'stats_recent',
    'series_by_status': 'stats_series_status',
    'items_by_status': 'stats_item_status',
    'novel_totals': 'stats_novels',
    'library_totals': 'stats_libraries',
    'reading_totals': 'stats_totals',
}


async def reading_overview(db, user_id):
    """The page uses only history existence and positive-time calendar days."""
    row = (await queries._rows(db, """
        SELECT COUNT(DISTINCT CASE WHEN MIN(seconds,cap_seconds)>0
                    THEN date(started_at,'localtime') END) AS days,
               EXISTS(SELECT 1 FROM page_reads WHERE user_id=?) AS pages
          FROM page_reads WHERE user_id=?
        """, (user_id, user_id)))[0]
    return dict(row)


async def comic_visits(db, user_id):
    """Only comics have the page-visit average displayed by Statistics."""
    return [dict(row) for row in await queries._rows(db, f"""
        SELECT kind, COALESCE({queries.COUNTED},0) AS seconds,
               {queries.VISITS} AS pages
          FROM page_reads WHERE user_id=? AND kind='cbz'
          GROUP BY kind ORDER BY kind
        """, (user_id,))]


async def load(db, name, user_id, start=None, end=None):
    if name == 'daily_activity':
        return await queries.daily_activity(db, user_id, start=start, end=end, comic_only=True)
    if name == 'reading_totals':
        return await reading_overview(db, user_id)
    if name == 'seconds_per_page_by_kind':
        return await comic_visits(db, user_id)
    function = getattr(queries, name)
    return await function(db) if name == 'library_totals' else await function(db, user_id)


async def collect(read, user_id, window, *, partial=False):
    """read is the cache/SQL seam; derived labels and HTML remain per request."""
    out = {}
    for name, phase in PHASES.items():
        if partial and name != 'daily_activity':
            continue
        start, end = (window['start'], window['end']) if name == 'daily_activity' else (None, None)
        with request_timing.stats_phase(phase):
            out[name] = await read(name, user_id, start, end)
    return out


def calendar_token():
    # SQLite localtime uses the process timezone, just like the range controls.
    import os
    import time
    return date.today().isoformat(), os.environ.get('TZ'), time.tzname
