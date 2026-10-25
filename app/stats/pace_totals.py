"""Derived valid word/time evidence, retained globally when content disappears."""

PACE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS page_reads_pace_totals
AFTER INSERT ON page_reads
WHEN NEW.visit_id IS NOT NULL AND NEW.kind IN ('epub','novel')
 AND NEW.pace_words>0 AND NEW.pace_seconds>0 AND NEW.pace_samples>0
BEGIN
 INSERT INTO account_pace_totals(user_id,words,seconds,samples)
 VALUES(NEW.user_id,NEW.pace_words,NEW.pace_seconds,NEW.pace_samples)
 ON CONFLICT(user_id) DO UPDATE SET words=words+excluded.words,
 seconds=seconds+excluded.seconds,samples=samples+excluded.samples;
 INSERT INTO item_pace_totals(user_id,item_id,words,seconds,samples)
 SELECT NEW.user_id,NEW.item_id,NEW.pace_words,NEW.pace_seconds,NEW.pace_samples
 WHERE NEW.kind='epub' AND NEW.item_id IS NOT NULL
 ON CONFLICT(user_id,item_id) DO UPDATE SET words=words+excluded.words,
 seconds=seconds+excluded.seconds,samples=samples+excluded.samples;
 INSERT INTO novel_pace_totals(user_id,novel_id,words,seconds,samples)
 SELECT NEW.user_id,NEW.novel_id,NEW.pace_words,NEW.pace_seconds,NEW.pace_samples
 WHERE NEW.kind='novel' AND NEW.novel_id IS NOT NULL
 ON CONFLICT(user_id,novel_id) DO UPDATE SET words=words+excluded.words,
 seconds=seconds+excluded.seconds,samples=samples+excluded.samples;
END;
"""


async def rebuild_pace_totals(db):
    """The caller owns the transaction; history and its original limits stay intact."""
    eligible = "visit_id IS NOT NULL AND kind IN ('epub','novel') AND pace_words>0 AND pace_seconds>0 AND pace_samples>0"
    for table in ('account_pace_totals', 'item_pace_totals', 'novel_pace_totals'):
        await db.execute('DELETE FROM ' + table)
    await db.execute(f'''INSERT INTO account_pace_totals(user_id,words,seconds,samples)
        SELECT user_id,SUM(pace_words),SUM(pace_seconds),SUM(pace_samples)
        FROM page_reads WHERE {eligible} GROUP BY user_id''')
    for table, column, kind, source in (
            ('item_pace_totals', 'item_id', 'epub', 'items'),
            ('novel_pace_totals', 'novel_id', 'novel', 'novels')):
        await db.execute(f'''INSERT INTO {table}(user_id,{column},words,seconds,samples)
            SELECT user_id,{column},SUM(pace_words),SUM(pace_seconds),SUM(pace_samples)
            FROM page_reads WHERE {eligible} AND kind='{kind}'
              AND {column} IN (SELECT id FROM {source}) GROUP BY user_id,{column}''')


async def ensure_pace_totals(db):
    # A cache-specific receipt avoids a history scan on every startup, including
    # installations that have no valid word samples yet. The receipt and backfill
    # commit together; an interrupted transaction safely retries.
    state = await (await db.execute("SELECT version FROM derived_stats_state WHERE name='text-pace'")).fetchone()
    if state is None or state[0] != 1:
        await rebuild_pace_totals(db)
        await db.execute("INSERT OR REPLACE INTO derived_stats_state(name,version) VALUES('text-pace',1)")
