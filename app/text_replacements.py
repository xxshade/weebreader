"""Per-series / per-novel find-and-replace for the text readers.

Display only: the substitution happens while a chapter is being rendered
(static/js/reader-text.js), so nothing here ever writes to the EPUB on
disk or to `chapters.content_html`. Deleting a rule puts the original text
back with no re-import and no re-sync.

The rules travel to the browser as JSON on the reader root, rather than
being applied server-side, for two reasons. An EPUB chapter is served
straight from the archive as bytes, so rewriting it on the way through
would mean parsing and re-serialising XHTML on every request to change a
few words. And doing it in the reader is what makes "visual only"
structural rather than a promise: the file is never in the path at all.
"""
import json

MAX_RULES_PER_TARGET = 50


async def rules_for(db, user_id: int, *, series_id=None, novel_id=None) -> list[dict]:
    """Every rule for one series or one novel, oldest first.

    Oldest first because the reader applies them in order and a later rule
    sees the earlier one's output -- so the order has to be stable and
    visible in the settings list, not whatever the query happened to
    return.
    """
    column = "series_id" if novel_id is None else "novel_id"
    target = series_id if novel_id is None else novel_id
    if target is None:
        # A comic, or an item with no series. SQL would answer this the
        # same way (`= NULL` matches nothing), so this is a short-circuit,
        # not the thing that makes it safe.
        return []
    cursor = await db.execute(
        f"SELECT find_text, replace_text FROM text_replacements "
        f"WHERE user_id = ? AND {column} = ? ORDER BY id",
        (user_id, target),
    )
    return [{"find": row["find_text"], "replace": row["replace_text"]}
            for row in await cursor.fetchall()]


async def rules_json(db, user_id: int, *, series_id=None, novel_id=None) -> str:
    """The same rules, as the JSON the reader templates put on #reader-root.

    Always a valid JSON array, "[]" when there is nothing -- the reader
    parses this unconditionally, and an empty attribute is not JSON.
    """
    return json.dumps(await rules_for(db, user_id, series_id=series_id, novel_id=novel_id))


async def list_all(db, user_id: int) -> list[dict]:
    """Every rule this user has, with the name of what it applies to --
    what the settings page lists."""
    cursor = await db.execute(
        """
        SELECT text_replacements.id, text_replacements.find_text, text_replacements.replace_text,
               series.name AS series_name, novels.title AS novel_title
          FROM text_replacements
     LEFT JOIN series ON series.id = text_replacements.series_id
     LEFT JOIN novels ON novels.id = text_replacements.novel_id
         WHERE text_replacements.user_id = ?
      ORDER BY COALESCE(series.name, novels.title), text_replacements.id
        """,
        (user_id,),
    )
    return [
        {
            "id": row["id"],
            "find_text": row["find_text"],
            "replace_text": row["replace_text"],
            "target_name": row["series_name"] or row["novel_title"],
        }
        for row in await cursor.fetchall()
    ]


def parse_target(raw: str) -> tuple[int | None, int | None]:
    """"series:12" / "novel:3" -> the pair of ids the table stores.

    One <select> rather than two, because a rule belongs to exactly one of
    them and two controls would let you pick both. Anything unrecognised
    comes back as (None, None), which the caller rejects -- the value is
    submitted by a form, so it can be anything.
    """
    kind, _, raw_id = (raw or "").partition(":")
    if not raw_id.isdigit():
        return None, None
    if kind == "series":
        return int(raw_id), None
    if kind == "novel":
        return None, int(raw_id)
    return None, None
