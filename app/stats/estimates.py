"""Remaining reading time from matched text evidence or comic page history.

Text uses validated words/time from reading visits, never lifetime time divided
by absolute progress. Exact source inventories locate the saved block/UTF-16
anchor; XHTML bytes are not words. Inventory parsing is lazy for the requested
cards and runs outside the event loop. Comic rates retain their existing rules.
"""
import asyncio
import logging
from pathlib import Path
import re
import zipfile

from app.stats import estimate_snapshots as snapshots

from lxml.etree import XMLSyntaxError

from app import text_replacements
from app.stats.text_inventory import inventory_epub, inventory_html, inventory_epub_total, inventory_html_total

from collections import namedtuple

# Comic size is pages; EPUB sizes never feed text pace or remaining words.
ESTIMATED_FORMATS = ("epub", "cbz")

# The one format whose rate is learned PER SERIES rather than across the
# library, and the reason the comic estimate can exist at all.
#
# Seconds per page is meaningless averaged over a whole shelf: a wordless
# art book and a dialogue-heavy series differ by more than an order of
# magnitude per page, so one global number describes neither and would be
# confidently wrong on every card. Within ONE series the lettering density
# is near enough constant -- it is the same author, letterer and format
# throughout -- so "how long a page of THIS takes" is a question the
# reader's own history with that series can answer.
#
# The cost of that scope is that a series nobody has read yet has no rate
# of its own. It borrows COMIC_FALLBACK_PERCENTILE of the others until it
# does.
COMIC_FORMAT = "cbz"

# What a comic series with no rate of its own is timed at: this percentile
# of the per-series rates the reader DOES have, one sample per series.
#
# Per series and not per volume, because the spread worth describing is
# between series (see COMIC_FORMAT) -- counted by volume, the one series
# read forty volumes deep would be most of the distribution. And the slow
# end rather than the median: an unread series could be as dense as the
# densest one read so far, and an estimate that runs out with pages still
# to go is the more misleading of the two errors. With one series read it
# is simply that series' rate; with two, most of the way to the slower.
#
# Only ever a stand-in. The first volume read far enough to be a sample
# (MIN_SAMPLE_FRACTION, MIN_SAMPLE_SECONDS) gives the series a rate of its
# own, and that replaces this on every card of it.
COMIC_FALLBACK_PERCENTILE = 0.95

# Existing comic sample thresholds remain unchanged.
MIN_SAMPLE_FRACTION = 0.05
MIN_SAMPLE_SECONDS = 60

# Preserve comic live-percent compatibility; text clients fetch authoritative ETA.
Estimate = namedtuple("Estimate", "remaining total rest", defaults=(0.0,))


# Same evidence floor as the words-per-minute statistic. The prior represents
# 3,000 previously measured words; local evidence replaces it gradually.
MIN_PACE_WORDS = 1000
MIN_PACE_SECONDS = 300
MIN_PACE_SAMPLES = 3
PRIOR_WORDS = 3000


log = logging.getLogger(__name__)

STATE_LABELS = {
    'collecting': 'Collecting data',
    'calculating': 'Calculating…',
    'position_unavailable': 'Position unavailable',
    'content_unavailable': 'Content unavailable · retry',
    'temporary_failure': 'Estimate failed · retry',
    'complete': '',
    'available': '',
}
STATE_DETAILS = {
    'collecting': 'Not enough valid reading samples yet (1,000 words, 5 minutes, 3 samples).',
    'calculating': 'The estimate is being calculated after the page opens.',
    'position_unavailable': 'The saved position cannot be matched to the text. Resume reading and save a new position.',
    'content_unavailable': 'The content could not be read. Check the file or chapter download, then retry.',
    'temporary_failure': 'The estimate request failed. Activate to retry.',
    'complete': '',
    'available': 'Based on measured reading pace.',
}


class ReadingEstimator:
    cached_only = False
    def __init__(self, *, spent_by_item, rate_by_series, comic_fallback_rate=None,
                 db=None, user_id=None, samples=None):
        self.spent_by_item = spent_by_item
        self.rate_by_series = rate_by_series
        self.comic_fallback_rate = comic_fallback_rate
        self.db, self.user_id = db, user_id
        self.cache_account = user_id
        self.samples = samples or {}
        self.item_estimates, self.novel_estimates = {}, {}
        self.completed_items, self.pending_novels = set(), set()
        self.item_states, self.novel_states = {}, {}
        self.account_sample = tuple(sum(v[i] for v in self.samples.values()) for i in range(3))

    def text_rate(self, kind, identity):
        all_words, all_seconds, all_samples = self.account_sample
        if (all_words < MIN_PACE_WORDS or all_seconds < MIN_PACE_SECONDS
                or all_samples < MIN_PACE_SAMPLES):
            return None
        words, seconds, _ = self.samples.get((kind, identity), (0, 0, 0))
        other_words, other_seconds = all_words - words, all_seconds - seconds
        if not words:
            return all_seconds / all_words
        if other_words <= 0:
            return seconds / words
        prior = min(PRIOR_WORDS, other_words)
        return (seconds + prior * other_seconds / other_words) / (words + prior)

    async def prepare_items(self, items):
        """Parse only requested text items; warm inventory work is cache hits."""
        ids = {item['id'] for item in items if item['file_format'] == 'epub'}
        requested = list(ids - self.item_estimates.keys())
        rows = {}
        for start in range(0, len(requested), 500):
            batch = requested[start:start + 500]
            placeholders = ','.join('?' for _ in batch)
            fetched = await (await self.db.execute(
                f"""SELECT items.*, reading_progress.status, reading_progress.current_page,
                          reading_progress.current_location, reading_progress.progress_percent
                     FROM items LEFT JOIN reading_progress ON reading_progress.item_id=items.id
                      AND reading_progress.user_id=? WHERE items.id IN ({placeholders})""",
                (self.user_id, *batch))).fetchall()
            rows.update((row['id'], row) for row in fetched)
        for identity in requested:
            self.item_estimates[identity] = None
            rate = self.text_rate('epub', identity)
            self.item_states[identity] = 'collecting' if rate is None else 'calculating'
            row = rows.get(identity)
            if row is None:
                continue
            if row['status'] == 'completed':
                self.completed_items.add(identity)
                self.item_states[identity] = 'complete'
                continue
            if rate is None:
                continue
            section, block, offset = row['current_page'] or 0, 0, 0
            if row['current_location']:
                anchor = re.fullmatch(r'elidx:(\d+)(?::(\d+))?', row['current_location'])
                if anchor is None:
                    self.item_states[identity] = 'position_unavailable'
                    continue
                block, offset = int(anchor[1]), int(anchor[2] or 0)
            elif row['progress_percent']:
                self.item_states[identity] = 'position_unavailable'
                continue  # Old percentages cannot identify remaining words.
            rules = await text_replacements.rules_for(self.db, self.user_id, series_id=row['series_id'])
            scope = (self.cache_account, 'item', identity)
            try:
                stat = await asyncio.to_thread(Path(row['file_path']).stat)
                fingerprint = snapshots.signature((row['file_path'], row['file_hash'], stat.st_size,
                    stat.st_mtime_ns, section, block, offset, rules, 1))
                if self.cached_only:
                    saved = await asyncio.to_thread(snapshots.load, scope, fingerprint)
                    if saved is not None:
                        self.item_estimates[identity] = Estimate(*saved)
                        self.item_states[identity] = 'available'
                        continue
                    continue  # HTML renders never wait for a cold inventory.
                if section == block == offset == 0:
                    total = remaining = await asyncio.to_thread(inventory_epub_total, row['file_path'], row['file_hash'], rules)
                else:
                    inventory = await asyncio.to_thread(inventory_epub, row['file_path'], row['file_hash'], rules)
                    total, remaining = inventory.total_words, inventory.remaining(section, block, offset)
            except (OSError, ValueError, KeyError, AttributeError, IndexError, XMLSyntaxError, zipfile.BadZipFile) as exc:
                self.item_states[identity] = 'content_unavailable'
                log.info('reading_estimate user=%s item=%s state=content_unavailable error=%s',
                         self.user_id, identity, type(exc).__name__)
                continue
            if remaining is not None:
                self.item_estimates[identity] = Estimate(remaining * rate, total * rate, 0)
                self.item_states[identity] = 'available'
                await asyncio.to_thread(snapshots.save, scope, fingerprint, self.item_estimates[identity])
            else:
                self.item_states[identity] = 'position_unavailable'

    async def prepare_novel(self, novel_id, volume_id=None):
        key = novel_id, volume_id
        if key in self.novel_estimates:
            return
        self.novel_estimates[key] = None
        self.novel_states[key] = 'complete'
        rate = self.text_rate('novel', novel_id)
        position = await (await self.db.execute(
            'SELECT chapter_id,paragraph_index,character_offset,chapter_percent FROM novel_reading_progress WHERE user_id=? AND novel_id=?',
            (self.user_id, novel_id))).fetchone()
        scope = ' AND chapters.volume_id=?' if volume_id is not None else ''
        params = (self.user_id, novel_id, volume_id) if volume_id is not None else (self.user_id, novel_id)
        query = """FROM chapters LEFT JOIN novel_read_chapters AS marked
                 ON marked.chapter_id=chapters.id AND marked.user_id=?
                WHERE chapters.novel_id=? AND marked.chapter_id IS NULL""" + scope
        pending = await (await self.db.execute('SELECT COUNT(*) ' + query, params)).fetchone()
        if not pending[0]:
            return
        self.pending_novels.add(key)
        self.novel_states[key] = 'collecting' if rate is None else 'calculating'
        if rate is None:
            return
        rules = await text_replacements.rules_for(self.db, self.user_id, novel_id=novel_id)
        revision = await (await self.db.execute(
            'SELECT revision FROM novel_content_revisions WHERE novel_id=?', (novel_id,))).fetchone()
        identities = await (await self.db.execute('SELECT chapters.id ' + query + ' ORDER BY chapters.id', params)).fetchall()
        scope_key = (self.cache_account, 'novel', novel_id, volume_id)
        fingerprint = snapshots.signature((revision[0] if revision else 0,
            [row[0] for row in identities], tuple(position) if position else None, rules, 1))
        if self.cached_only:
            saved = await asyncio.to_thread(snapshots.load, scope_key, fingerprint)
            if saved is not None:
                self.novel_estimates[key] = Estimate(*saved)
                self.novel_states[key] = 'available'
                return
            return  # Small scopes can also contain expensive chapter text.
        remaining = 0
        async with self.db.execute('SELECT chapters.id,chapters.content_html ' + query, params) as cursor:
            while batch := await cursor.fetchmany(16):
                for chapter in batch:
                    if chapter['content_html'] is None:
                        self.novel_states[key] = 'content_unavailable'
                        log.info('reading_estimate user=%s novel=%s chapter=%s state=content_unavailable', self.user_id, novel_id, chapter['id'])
                        return  # A missing chapter is never guessed from byte averages.
                    try:
                        if position is not None and chapter['id'] == position['chapter_id']:
                            if position['paragraph_index'] is None and position['chapter_percent']:
                                self.novel_states[key] = 'position_unavailable'
                                return
                            inventory = await asyncio.to_thread(inventory_html, chapter['content_html'], rules)
                            words = inventory.remaining(0, position['paragraph_index'] or 0, position['character_offset'] or 0)
                            if words is None:
                                self.novel_states[key] = 'position_unavailable'
                                return
                        else:
                            words = await asyncio.to_thread(inventory_html_total, chapter['content_html'], rules)
                    except (ValueError, OSError) as exc:
                        self.novel_states[key] = 'content_unavailable'
                        log.info('reading_estimate user=%s novel=%s state=content_unavailable error=%s', self.user_id, novel_id, type(exc).__name__)
                        return
                    remaining += words
        self.novel_estimates[key] = Estimate(remaining * rate, remaining * rate, 0)
        self.novel_states[key] = 'available'
        await asyncio.to_thread(snapshots.save, scope_key, fingerprint, self.novel_estimates[key])

    def for_novel(self, novel_id, volume_id=None):
        return self.novel_estimates.get((novel_id, volume_id))

    def novel_collecting(self, novel_id, volume_id=None):
        return self.novel_states.get((novel_id, volume_id)) == 'collecting'

    def is_collecting(self, item):
        return self.item_states.get(item['id']) == 'collecting'

    def item_without_estimate(self, item):
        return (item['file_format'] == 'epub' and item['id'] not in self.completed_items
                and self.item_estimates.get(item['id']) is None)

    @staticmethod
    def _total_from_own_pace(spent, fraction):
        if spent < MIN_SAMPLE_SECONDS or fraction < MIN_SAMPLE_FRACTION:
            return None
        return spent / fraction

    @staticmethod
    def _estimate(total, fraction, rest=0.0):
        return Estimate(remaining=rest + total * (1 - fraction), total=total, rest=rest)

    def for_item(self, item, progress):
        if item['file_format'] == 'epub':
            if progress is not None and progress['status'] == 'completed':
                return None
            return self.item_estimates.get(item['id'])
        if item['file_format'] != COMIC_FORMAT:
            return None
        fraction = 0.0
        if progress is not None:
            if progress['status'] == 'completed':
                return None
            fraction = min(1.0, max(0.0, (progress['progress_percent'] or 0) / 100))
        if fraction >= 1:
            return None
        total = self._total_from_own_pace(self.spent_by_item.get(item['id'], 0), fraction)
        if total is None:
            total = self._total_from_rate(item)
        return self._estimate(total, fraction) if total is not None else None

    def _total_from_rate(self, item):
        rate = self._comic_rate(item)
        return rate * item['page_count'] if rate and item['page_count'] else None

    def _comic_rate(self, item):
        return self.rate_by_series.get(item['series_id']) or self.comic_fallback_rate

    def total_remaining(self, items):
        total = None
        for item in items:
            estimate = self.for_item(item, item)
            if estimate is None:
                continue
            total = (total or 0.0) + estimate.remaining
        return total

    def missing_items(self, items):
        return sum(self.item_without_estimate(item) for item in items)

    def series_state(self, items):
        states = [self.item_states.get(item['id'], 'calculating') for item in items if self.item_without_estimate(item)]
        return next((state for state in ('content_unavailable', 'position_unavailable', 'temporary_failure',
                                        'calculating', 'collecting') if state in states), 'available')

    def can_estimate(self, item):
        if item['file_format'] == COMIC_FORMAT:
            return bool(self._comic_rate(item))
        return item['file_format'] == 'epub' and self.text_rate('epub', item['id']) is not None


# Comic page-rate history uses the recorded completion fraction, not a
# declaration that an unfinished book was read. Text never uses this ratio.
_PER_ITEM_FRACTION = """
    SELECT item_read_totals.item_id AS id,
           item_read_totals.seconds AS seconds,
           CASE WHEN reading_progress.status = 'completed'
                THEN MIN(1.0, MAX(0.0,
                     COALESCE(reading_progress.completed_from_percent, 100) / 100.0))
                ELSE MIN(1.0, MAX(0.0,
                     COALESCE(reading_progress.progress_percent, 0) / 100.0))
           END AS fraction
      FROM item_read_totals
 LEFT JOIN reading_progress
        ON reading_progress.item_id = item_read_totals.item_id
       AND reading_progress.user_id = item_read_totals.user_id
     WHERE item_read_totals.user_id = ?
"""


async def _fetch_all(db, sql, params=()):
    return await (await db.execute(sql, params)).fetchall()


async def build_estimator(db, user_id: int, *, cached_only=False) -> ReadingEstimator:
    spent_by_item = {
        r["item_id"]: r["seconds"]
        for r in await _fetch_all(
            db, "SELECT item_id, seconds FROM item_read_totals WHERE user_id = ?", (user_id,))
    }
    # Comic rates remain per-series seconds per page actually read. Keep
    # lifetime item totals and removed-volume history separate from text pace.
    rate_by_series = {}
    totals = {}
    for row in await _fetch_all(db, f"""
        SELECT items.series_id AS series_id,
               SUM(per_item.seconds) AS seconds,
               SUM(items.page_count * per_item.fraction) AS size
          FROM ({_PER_ITEM_FRACTION}) AS per_item
          JOIN items ON items.id = per_item.id
         WHERE items.file_format='cbz' AND items.page_count>0
           AND per_item.fraction >= ? AND per_item.seconds >= ?
      GROUP BY items.series_id
    """, (user_id, MIN_SAMPLE_FRACTION, MIN_SAMPLE_SECONDS)):
        if not row["size"]:
            continue
        key = ('series', row['series_id'])
        if key[1] is None:
            continue
        seconds, size = totals.get(key, (0.0, 0.0))
        totals[key] = (seconds + row["seconds"], size + row["size"])
    # Deleted series still teach the conservative fallback; if its folder
    # returns, retained volumes teach that series again under its current ID.
    for row in await _fetch_all(db, """
        SELECT fmt, series_id, library_id, folder_path,
               SUM(seconds) AS seconds, SUM(size * fraction) AS size
          FROM (SELECT removed_items.file_format AS fmt,
                       series.id AS series_id,
                       removed_items.library_id AS library_id,
                       removed_items.folder_path AS folder_path,
                       removed_item_reads.seconds AS seconds,
                       removed_items.page_count AS size,
                       CASE WHEN removed_item_reads.status = 'completed'
                            THEN MIN(1.0, MAX(0.0,
                                 COALESCE(removed_item_reads.completed_from_percent, 100) / 100.0))
                            ELSE MIN(1.0, MAX(0.0,
                                 COALESCE(removed_item_reads.progress_percent, 0) / 100.0))
                       END AS fraction
                  FROM removed_item_reads
                  JOIN removed_items ON removed_items.id = removed_item_reads.removed_item_id
             LEFT JOIN series ON series.library_id = removed_items.library_id
                             AND series.folder_path = removed_items.folder_path
                 WHERE removed_item_reads.user_id = ?)
         WHERE fmt = 'cbz' AND size > 0 AND fraction >= ? AND seconds >= ?
      GROUP BY fmt, series_id, library_id, folder_path
    """, (user_id, MIN_SAMPLE_FRACTION, MIN_SAMPLE_SECONDS)):
        if not row["size"]:
            continue
        if row["series_id"] is not None:
            key = ("series", row["series_id"])
        elif row["folder_path"] is not None:
            key = ("gone", (row["library_id"], row["folder_path"]))
        else:
            continue
        seconds, size = totals.get(key, (0.0, 0.0))
        totals[key] = (seconds + row["seconds"], size + row["size"])

    # A series no longer on disk has a rate that only the fallback uses.
    gone_series_rates = []
    for (kind, scope), (seconds, size) in totals.items():
        if not size:
            continue
        if kind == "gone":
            gone_series_rates.append(seconds / size)
        else:
            rate_by_series[scope] = seconds / size

    samples = {}
    global_row = await (await db.execute(
        'SELECT words,seconds,samples FROM account_pace_totals WHERE user_id=?', (user_id,))).fetchone()
    for kind, table, column in (('epub','item_pace_totals','item_id'), ('novel','novel_pace_totals','novel_id')):
        for row in await _fetch_all(db, f'SELECT {column} AS identity,words,seconds,samples FROM {table} WHERE user_id=?', (user_id,)):
            samples[(kind, row['identity'])] = (row['words'], row['seconds'], row['samples'])
    if global_row is not None:
        # Deleted content still contributes historical account pace, but cannot
        # attach its own pace to a newly inserted item reusing an old ID.
        samples[('history', None)] = tuple(global_row[i] - sum(v[i] for v in samples.values()) for i in range(3))

    estimator = ReadingEstimator(
        spent_by_item=spent_by_item, rate_by_series=rate_by_series,
        db=db, user_id=user_id, samples=samples,
        comic_fallback_rate=percentile(list(rate_by_series.values()) + gone_series_rates,
                                       COMIC_FALLBACK_PERCENTILE),
    )
    account = await (await db.execute('SELECT password_hash,created_at FROM users WHERE id=?', (user_id,))).fetchone()
    estimator.cache_account = (user_id, snapshots.signature(tuple(account) if account else None))
    estimator.cached_only = cached_only
    return estimator


def percentile(values, q: float) -> "float | None":
    """The `q` quantile of `values`, interpolating between the two samples
    either side of it (numpy's default, "linear"), or None for no samples.

    Interpolated rather than nearest-rank because the samples here are few
    -- one per comic series read -- and nearest-rank on a handful of them
    is just the maximum, jumping to a new series' rate outright the moment
    it is added.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    below = int(position)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (position - below)
