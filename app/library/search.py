"""Server-side search over item titles.

The series half of the home page's search box is client-side -- every
series card is already in the DOM -- so only items are searched here.

Why this exists: every search in this app was a client-side filter over the
nodes already rendered. That works for the home page, which renders every
series card, but no page lists every item, so there was no way at all to
find a volume by name without first knowing which series it was in.

Why LIKE and not FTS5: measured on a database shaped like a real library
before choosing. The item query below, join and ordering included, runs in
0.9-1.2 ms over 8000 items and 3.3-4.1 ms over 32000 (four times the size
of the library this was written for). An FTS5 index would buy a fraction of
a millisecond in exchange for a virtual table that has to be kept in sync
with every insert, update and delete -- a whole new way for the app to be
subtly wrong (an index that has drifted returns confidently incomplete
results). Not a trade worth making at this size.

Matching notes:

- Terms are AND-ed, so "kimetsu 07" finds "Kimetsu no Yaiba v07" the way a
  person expects, rather than requiring one exact substring.
- The comparison is against `title`, NOT `sort_title`: that holds
  natural_sort_key's output, which zero-pads digit runs
  ("Serie v07" is stored as "serie v0000000007"), so any query containing a
  number would silently fail to match.
- SQLite's LIKE folds ASCII case only, so "punpun" finds "Punpun" but
  "cronicas" does not find "Crônicas". Accent folding would need a custom
  collation or a second normalized column; left out deliberately rather
  than half-done.
"""


# Enough to be useful without turning a stray keystroke into a full dump of
# the library; the UI says when it clipped the list.
RESULT_LIMIT = 48

# A guard against a pasted paragraph becoming dozens of LIKE clauses per row.
MAX_TERMS = 6


def _terms(query: str) -> list[str]:
    return [t for t in (query or "").split() if t][:MAX_TERMS]


def _escape_like(term: str) -> str:
    """% and _ are LIKE's own wildcards, so a query containing one has to be
    escaped or it stops meaning what was typed -- searching for "100%" would
    match every title, and a lone "%" would return the whole library. The
    backslash itself goes first, or it would escape the escapes.

    Same ESCAPE '\\' convention already used by the converted-path cleanup
    in database.py."""
    for char in ("\\", "%", "_"):
        term = term.replace(char, f"\\{char}")
    return term


def _clauses(column: str, terms: list[str]) -> tuple[str, list[str]]:
    return (
        " AND ".join(f"{column} LIKE ? ESCAPE '\\'" for _ in terms),
        [f"%{_escape_like(t)}%" for t in terms],
    )


async def search_items(db, query: str, *, limit: int = RESULT_LIMIT) -> list[dict]:
    """Items whose own title matches. Deliberately not "items whose SERIES
    matches": the series card already answers that, and expanding it into
    every one of its volumes would bury the item hits under it.

    Asks for limit+1 rows so the caller can say it clipped the list without
    a second COUNT(*) over the whole match set."""
    terms = _terms(query)
    if not terms:
        return []
    where, params = _clauses("i.title", terms)
    cursor = await db.execute(
        f"""
        SELECT i.id, i.title, i.cover_path, i.page_count, i.file_format,
               i.series_id, s.name AS series_name,
               COALESCE(s.blur_cover, 0) AS blur_cover
          FROM items i
          LEFT JOIN series s ON s.id = i.series_id
         WHERE {where}
         ORDER BY (s.sort_name IS NULL), s.sort_name,
                  (i.number_in_series IS NULL), i.number_in_series, i.sort_title
         LIMIT ?
        """,
        (*params, limit + 1),
    )
    return [dict(row) for row in await cursor.fetchall()]
