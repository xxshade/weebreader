"""Stable chapter links and the numeric destinations retained before repair."""


async def revision(db, novel_id):
    row = await (
        await db.execute(
            "SELECT revision FROM novel_reader_revisions WHERE novel_id=?", (novel_id,)
        )
    ).fetchone()
    return row[0] if row else ""


async def resolve_number(db, novel_id, volume, number):
    row = await (
        await db.execute(
            """SELECT c.id FROM novel_chapter_aliases a
        JOIN chapters c ON c.id=a.chapter_id AND c.novel_id=a.novel_id
        WHERE a.novel_id=? AND a.volume_number=? AND a.chapter_number=?""",
            (novel_id, volume, number),
        )
    ).fetchone()
    if row:
        return row[0]
    row = await (
        await db.execute(
            """SELECT chapters.id,chapters.title,chapters.order_index
        FROM chapters JOIN volumes ON volumes.id=chapters.volume_id
        WHERE chapters.novel_id=? AND volumes.number=? AND chapters.number=?""",
            (novel_id, volume, number),
        )
    ).fetchone()
    return row[0] if row else None


async def capture_numbers(db, novel_id):
    # One statement for unambiguous numbers. The uncommon ambiguous keys use
    # the exact old lookup so the alias retains its existing resolution.
    await db.execute(
        """INSERT OR IGNORE INTO novel_chapter_aliases
        SELECT c.novel_id,v.number,c.number,MIN(c.id) FROM chapters c
        JOIN volumes v ON v.id=c.volume_id
        WHERE c.novel_id=? AND c.number IS NOT NULL
        GROUP BY v.number,c.number HAVING COUNT(*)=1""",
        (novel_id,),
    )
    repeated = await (
        await db.execute(
            """SELECT v.number,c.number FROM chapters c
        JOIN volumes v ON v.id=c.volume_id WHERE c.novel_id=? AND c.number IS NOT NULL
        GROUP BY v.number,c.number HAVING COUNT(*)>1""",
            (novel_id,),
        )
    ).fetchall()
    for volume, number in repeated:
        identity = await resolve_number(db, novel_id, volume, number)
        await db.execute(
            "INSERT OR IGNORE INTO novel_chapter_aliases VALUES(?,?,?,?)",
            (novel_id, volume, number, identity),
        )
