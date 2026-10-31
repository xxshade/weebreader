"""Bounded account-scoped projections of native estimates, never reading history.

SQLite revisions are the authority. Commit notifications only anticipate work;
missed notifications, other workers and restart fall back to validated reads.
"""
import asyncio
from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import sqlite3
from types import MappingProxyType
import weakref

from app import request_timing
from app.config import settings
from app.stats import estimate_snapshots as snapshots

log = logging.getLogger(__name__)
MAX_ACCOUNTS = 4
MAX_ENTRIES = 4096
PREPARED_FILE_BATCH_SIZE = 128


@dataclass(frozen=True, slots=True)
class _SourcePath:
    identity: int
    source: str
    path: Path


@dataclass(frozen=True, slots=True)
class _PreparedFiles:
    rows: tuple[_SourcePath, ...]
    by_id: Mapping[int, _SourcePath]


def _prepare_files(sources, previous=None):
    """Prepare lexical paths off-loop; never cache filesystem metadata."""
    if len(sources) > MAX_ENTRIES:
        return None
    previous = previous.by_id if previous is not None else {}
    index = {}
    for identity, (source, _) in sources.items():
        row = previous.get(identity)
        if row is None or row.source != source:
            path = Path(source)
            # Path defers string conversion: finish it in this worker too,
            # rather than adding that work to the first home validation.
            os.fspath(path)
            row = _SourcePath(identity, source, path)
        index[identity] = row
    return _PreparedFiles(tuple(index.values()), MappingProxyType(index))


def _stat_prepared_files(rows):
    results = {}
    for row in rows:
        try:
            results[row.identity] = row.path.stat()
        except (OSError, ValueError) as error:
            results[row.identity] = error
    return results


@asynccontextmanager
async def _dedicated_connection():
    """Finish opening before closing, including immediate startup cancellation."""
    from app.database import get_connection
    opening = asyncio.create_task(get_connection())
    try:
        db = await asyncio.shield(opening)
    except BaseException:
        try:
            db = await opening
        except Exception:
            pass
        else:
            await db.close()
        raise
    try:
        await db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        await db.close()


@dataclass
class State:
    account: tuple
    revision: tuple
    snapshot_version: tuple
    snapshot_rows: dict
    items: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    file_checks: _PreparedFiles | None = None
    words: dict = field(default_factory=dict)
    item_estimates: dict = field(default_factory=dict)
    item_states: dict = field(default_factory=dict)
    novel_estimates: dict = field(default_factory=dict)
    novel_states: dict = field(default_factory=dict)
    pending_novels: set = field(default_factory=set)
    novel_basis: dict = field(default_factory=dict)
    novel_rows: dict = field(default_factory=dict)
    series: dict = field(default_factory=dict)
    novels: dict = field(default_factory=dict)
    scope_work: dict = field(default_factory=dict)
    comic_estimates: dict = field(default_factory=dict)

    def seed(self, estimator):
        estimator.item_estimates.update(self.item_estimates)
        estimator.item_states.update(self.item_states)
        estimator.novel_estimates.update(self.novel_estimates)
        estimator.novel_states.update(self.novel_states)
        estimator.pending_novels.update(self.pending_novels)
        estimator.comic_estimates.update(self.comic_estimates)
        estimator._ready_state = self


async def revisions(db, uid):
    with request_timing.item_phase('items_query'):
        return await _revisions(db, uid)


async def _snapshot(operation):
    with request_timing.item_phase('items_index'):
        return await asyncio.to_thread(operation)


async def _revisions(db, uid):
    source = await (await db.execute('SELECT epoch,revision FROM estimate_revision WHERE id=1')).fetchone()
    pace = await (await db.execute('SELECT revision FROM estimate_account_revisions WHERE user_id=?', (uid,))).fetchone()
    work = {(r[0], r[1]): r[2] for r in await (await db.execute(
        'SELECT kind,identity,revision FROM estimate_work_revisions WHERE user_id=?', (uid,))).fetchall()}
    if source is None:
        raise sqlite3.DatabaseError('Missing estimate generation')
    return tuple(source), pace[0] if pace else 0, work


class ReadyEstimates:
    def __init__(self):
        self.enabled = False
        self.states = OrderedDict()
        self.locks = weakref.WeakValueDictionary()
        self.warming = set()
        self.task = None
        self.warmup = None
        self.dirty = False
        self.path = None
        self.loop = None
        self.uncacheable = {}

    def _current_path(self):
        path = str(settings.db_path)
        if self.path != path:
            self.states.clear()
            self.locks.clear()
            self.uncacheable.clear()
            self.path = path
        return path

    async def _files(self, state):
        from app.stats.estimates import FILE_STAT_BATCH_SIZE, _stat_epub_files
        if state.file_checks is None:
            # Unprepared states retain the existing bounded check. Oversized
            # libraries bypass this cache entirely and use the native estimator.
            rows = tuple({'id': identity, 'file_path': value[0]}
                         for identity, value in state.sources.items())
            worker, size = _stat_epub_files, FILE_STAT_BATCH_SIZE
        else:
            rows = state.file_checks.rows
            worker, size = _stat_prepared_files, PREPARED_FILE_BATCH_SIZE
        dirty = set()
        for start in range(0, len(rows), size):
            with request_timing.item_phase('items_stat'):
                stats = await asyncio.to_thread(worker, rows[start:start + size])
            for identity, value in stats.items():
                actual = type(value).__name__ if isinstance(value, (OSError, ValueError)) else (value.st_size, value.st_mtime_ns)
                if actual != state.sources[identity][1]:
                    dirty.add(state.items[identity]['series_id'])
        return dirty

    @staticmethod
    def _merge(old, fresh, series_ids, novel_ids):
        if old is None:
            return
        removed_items = {i for i, row in old.items.items() if row['series_id'] in series_ids}
        fresh.ready_items.update((i, row) for i, row in old.items.items() if i not in removed_items)
        fresh.ready_sources.update((i, v) for i, v in old.sources.items() if i not in removed_items)
        fresh.item_estimates.update((i, v) for i, v in old.item_estimates.items() if i not in removed_items)
        fresh.item_states.update((i, v) for i, v in old.item_states.items() if i not in removed_items)
        fresh.novel_estimates.update((key, v) for key, v in old.novel_estimates.items() if key[0] not in novel_ids)
        fresh.novel_states.update((key, v) for key, v in old.novel_states.items() if key[0] not in novel_ids)
        fresh.pending_novels.update(key for key in old.pending_novels if key[0] not in novel_ids)
        fresh.ready_novel_basis.update((key, value) for key, value in old.novel_basis.items() if key[0] not in novel_ids)
        fresh.ready_novels.update((i, row) for i, row in old.novel_rows.items() if i not in novel_ids)
        fresh.word_counts.update((scope, value) for scope, value in old.words.items()
                                 if not (scope[1] == 'item' and scope[2] in removed_items)
                                 and not (scope[1] == 'novel' and scope[2] in novel_ids))

    @staticmethod
    def _retime(state, fresh):
        """Reapply the ORIGINAL per-work pace and ORIGINAL comic calculations."""
        from app.stats.estimates import Estimate
        series, novels = set(), set()
        for identity, row in state.items.items():
            if row['file_format'] != 'epub':
                continue
            rate = fresh.text_rate('epub', identity)
            words = state.words.get((state.account, 'item', identity))
            if rate is None:
                state.item_estimates[identity] = None
                state.item_states[identity] = 'collecting'
            elif words is not None:
                state.item_estimates[identity] = Estimate(words.remaining * rate, words.total * rate)
                state.item_states[identity] = 'available'
            elif state.item_states.get(identity) == 'collecting' or state.item_estimates.get(identity) is not None:
                series.add(row['series_id'])
        for key in state.novel_states:
            rate = fresh.text_rate('novel', key[0])
            words = state.words.get((state.account, 'novel', *key))
            if key not in state.pending_novels:
                continue
            if rate is None:
                state.novel_estimates[key] = None
                state.novel_states[key] = 'collecting'
            elif words is not None:
                state.novel_estimates[key] = Estimate(words.remaining * rate, words.total * rate)
                state.novel_states[key] = 'available'
            elif state.novel_states[key] == 'collecting' or state.novel_estimates.get(key) is not None:
                novels.add(key[0])
        return series, novels

    @staticmethod
    def _copy(state):
        from dataclasses import replace
        return replace(state, item_estimates=dict(state.item_estimates), item_states=dict(state.item_states),
                       novel_estimates=dict(state.novel_estimates), novel_states=dict(state.novel_states))

    async def _build(self, db, uid, fresh, old, series_ids, novel_ids, eager_work):
        from app.library.router import _series_reading_uncached, _novel_reading_uncached
        fresh.collect_ready = True
        if old is not None:
            fresh.previous_novel_counts = {key: (old.words.get((old.account, 'novel', *key)), basis)
                                           for key, basis in old.novel_basis.items()}
        # A cold shelf/startup never parses source text. An explicit saved-work
        # change may prepare its native inventories off-loop before acknowledgement.
        fresh.cached_only = True
        if old is None:
            await _series_reading_uncached(db, uid, fresh)
            await _novel_reading_uncached(db, uid, fresh)
            volumes = await (await db.execute('SELECT novel_id,id FROM volumes LIMIT ?', (MAX_ENTRIES + 1,))).fetchall()
        else:
            for eager in (False, True):
                selected = sorted(i for i in series_ids if (('series', i) in eager_work) == eager)
                if selected:
                    fresh.cached_only = not eager
                    await _series_reading_uncached(db, uid, fresh, selected)
            for identity in sorted(novel_ids):
                fresh.cached_only = ('novel', identity) not in eager_work
                await _novel_reading_uncached(db, uid, fresh, identity)
            volumes = []
            for identity in sorted(novel_ids):
                volumes.extend(await (await db.execute('SELECT novel_id,id FROM volumes WHERE novel_id=? LIMIT ?',
                                                      (identity, MAX_ENTRIES + 1))).fetchall())
        for novel_id, volume in volumes:
            fresh.cached_only = old is None or ('novel', novel_id) not in eager_work
            await fresh.prepare_novel(novel_id, volume)
        self._merge(old, fresh, series_ids, novel_ids)
        # This await stays BEFORE get() rechecks both revisions. Cancellation
        # can leave the worker finishing local data, but cannot publish it.
        return await asyncio.to_thread(_prepare_files, fresh.ready_sources,
                                       old.file_checks if old is not None else None)

    @staticmethod
    def _state(fresh, before, version, rows, file_checks=None):
        from app.library.router import ShelfReading, NovelShelf
        state = State(account=fresh.cache_account, revision=before, snapshot_version=version, snapshot_rows=rows,
                      items=fresh.ready_items, sources=fresh.ready_sources, file_checks=file_checks, words=fresh.word_counts,
                      item_estimates=fresh.item_estimates, item_states=fresh.item_states,
                      novel_estimates=fresh.novel_estimates, novel_states=fresh.novel_states,
                      pending_novels=fresh.pending_novels, novel_basis=fresh.ready_novel_basis, novel_rows=fresh.ready_novels)
        by_series = defaultdict(list)
        for row in state.items.values():
            by_series[row['series_id']].append(row)
            if row['file_format'] == 'cbz':
                state.comic_estimates[fresh.comic_key(row, row)] = fresh.for_item(row, row)
        fresh.comic_estimates.update(state.comic_estimates)
        state.series = {sid: ShelfReading(len(items), fresh.total_remaining(items),
                        any(row['file_format'] == 'epub' for row in items),
                        fresh.missing_items(items), fresh.series_state(items)) for sid, items in by_series.items()}
        for identity, row in state.novel_rows.items():
            estimate = fresh.for_novel(identity)
            state.novels[identity] = NovelShelf(unread=row['unread'] or 0,
                                              remaining=estimate.remaining if estimate else None,
                                              newest=row['newest'], text_eta=bool(row['unread']),
                                              state=fresh.novel_states.get((identity, None), 'complete'))
        state.scope_work = {snapshots.signature((state.account, 'item', i)): ('series', row['series_id'])
                            for i, row in state.items.items() if row['file_format'] == 'epub'}
        state.scope_work.update((snapshots.signature((state.account, 'novel', *key)), ('novel', key[0]))
                                for key in state.novel_states)
        return state

    async def get(self, db, uid, estimator, *, force=False, authoritative=False, allow_authoritative=False, seed=True):
        if not self.enabled or (not estimator.cached_only and not force and not allow_authoritative):
            return None
        self._current_path()
        if not force and seed and getattr(estimator, "_ready_state", None) is not None:
            return estimator._ready_state
        lock = self.locks.setdefault(uid, asyncio.Lock())
        if not force and (uid in self.warming or lock.locked()):
            return None  # Original path, not a waiter behind background work.
        try:
            async with lock:
                for _ in range(3):
                    before = await revisions(db, uid)
                    limit_token = (before[0], before[1], sum(before[2].values()))
                    if self.uncacheable.get(uid) == limit_token:
                        return None
                    version = await _snapshot(snapshots.version)
                    state = self.states.get(uid)
                    full = state is None or state.account != estimator.cache_account or state.revision[0] != before[0]
                    if full:
                        count = await (await db.execute("""SELECT
                            (SELECT COUNT(*) FROM items LEFT JOIN reading_progress AS p
                             ON p.item_id=items.id AND p.user_id=?
                             WHERE items.series_id IS NOT NULL AND (p.status IS NULL OR p.status<>'completed'))
                            + (SELECT COUNT(DISTINCT novel_id) FROM chapters)
                            + (SELECT COUNT(*) FROM volumes)""", (uid,))).fetchone()
                        if count[0] > MAX_ENTRIES:
                            self.states.pop(uid, None)
                            self.uncacheable[uid] = limit_token
                            while len(self.uncacheable) > MAX_ACCOUNTS:
                                self.uncacheable.pop(next(iter(self.uncacheable)))
                            return None
                    pace = not full and state.revision[1] != before[1]
                    series_ids, novel_ids = set(), set()
                    index = None
                    eager_work = set()
                    if not full:
                        state = self._copy(state)
                        changed = {key for key in before[2].keys() | state.revision[2].keys()
                                   if before[2].get(key) != state.revision[2].get(key)}
                        if authoritative:
                            eager_work = set(changed)
                        if version != state.snapshot_version:
                            version, index = await _snapshot(snapshots.versioned_read)
                            if version[0] != state.snapshot_version[0]:
                                full = True
                            else:
                                changed.update(work for scope, work in state.scope_work.items()
                                               if state.snapshot_rows.get(scope) != index.get(scope))
                        series_ids.update(identity for kind, identity in changed if kind == 'series')
                        novel_ids.update(identity for kind, identity in changed if kind == 'novel')
                        if not force:
                            series_ids.update(await self._files(state))
                    if full or pace or series_ids or novel_ids:
                        from app.stats.estimates import build_estimator
                        fresh = await build_estimator(db, uid, cached_only=True)
                        if pace:
                            dirty_series, dirty_novels = self._retime(state, fresh)
                            series_ids.update(dirty_series)
                            novel_ids.update(dirty_novels)
                        file_checks = await self._build(db, uid, fresh, None if full else state, series_ids, novel_ids,
                                                       eager_work if not full else set())
                        version_after, rows = await _snapshot(snapshots.versioned_read)
                        after = await revisions(db, uid)
                        if after != before or version_after != version:
                            continue
                        state = self._state(fresh, after, version_after, rows, file_checks)
                    else:
                        # No source work or JSON decode on a stable hit.
                        state.revision = before
                        if index is not None:
                            state.snapshot_version, state.snapshot_rows = version, index
                        if await revisions(db, uid) != before or await _snapshot(snapshots.version) != version:
                            continue
                    entries = len(state.items) + len(state.novel_states)
                    if entries <= MAX_ENTRIES:
                        self.states[uid] = state
                        self.states.move_to_end(uid)
                        while len(self.states) > MAX_ACCOUNTS:
                            self.states.popitem(last=False)
                    else:
                        self.states.pop(uid, None)
                        self.uncacheable[uid] = limit_token
                        while len(self.uncacheable) > MAX_ACCOUNTS:
                            self.uncacheable.pop(next(iter(self.uncacheable)))
                    if seed:
                        with request_timing.item_phase('items_snapshots'):
                            state.seed(estimator)
                    return state
                self.states.pop(uid, None)
                return None
        except (OSError, sqlite3.Error) as error:
            self.states.pop(uid, None)
            log.info('ready_estimates user=%s state=unavailable error=%s', uid, type(error).__name__)
            return None

    async def api(self, db, uid, estimator, parsed):
        if not self.enabled or uid not in self.states:
            return None
        state = await self.get(db, uid, estimator, allow_authoritative=True, seed=False)
        if state is None:
            return None
        results = []
        for key, (kind, identity, volume) in parsed.items():
            if kind == 'series':
                value = state.series.get(identity)
                if (value is None or value.missing or any(
                        row['file_format'] == 'epub' and (state.account, 'item', i) not in state.words
                        for i, row in state.items.items() if row['series_id'] == identity)):
                    return None
                results.append(dict(key=key, remaining=value.remaining, total=None,
                                    collecting=value.state == 'collecting', state=value.state, missing=value.missing))
                continue
            if kind == 'item':
                row = state.items.get(identity)
                scope = (state.account, 'item', identity)
                if row is None or row['file_format'] != 'epub' or scope not in state.words:
                    return None
                value = state.item_estimates.get(identity)
                status = state.item_states.get(identity)
            else:
                scope = (state.account, 'novel', identity, volume)
                target = (identity, volume)
                status = state.novel_states.get(target)
                value = state.novel_estimates.get(target)
                if status != 'complete' and scope not in state.words:
                    return None
            if status not in ('available', 'complete'):
                return None
            results.append(dict(key=key, remaining=value.remaining if value else None,
                                total=value.total if value else None, collecting=False, state=status, missing=0))
        return {'estimates': results}

    async def _refresh(self, uid, *, authoritative=False):
        from app.stats.estimates import build_estimator
        async with _dedicated_connection() as db:
            estimator = await build_estimator(db, uid, cached_only=True)
            return await self.get(db, uid, estimator, force=True, authoritative=authoritative)

    async def _drain(self):
        try:
            # An HTTP acknowledgement never waits for an unbounded stream
            # of future commits. A following task handles newer generations.
            for _ in range(2):
                if not self.dirty or not self.enabled:
                    break
                self.dirty = False
                for uid in list(self.states):
                    await self._refresh(uid, authoritative=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.states.clear()
            log.exception('ready_estimates state=refresh_failed')
        finally:
            if self.dirty and self.enabled:
                self.task = asyncio.create_task(self._drain())

    def notify(self):
        if not self.enabled:
            return
        self.dirty = True
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._drain())

    def notify_snapshot(self):
        loop = self.loop
        if self.enabled and loop is not None:
            with suppress(RuntimeError):  # A worker can finish while shutdown closes the loop.
                loop.call_soon_threadsafe(self.notify)

    async def flush(self):
        if self.enabled and self.task and not self.task.done():
            await asyncio.shield(self.task)

    async def start(self):
        self._current_path()
        self.enabled = True
        self.loop = asyncio.get_running_loop()
        self.warmup = asyncio.create_task(self._warm())

    async def _warm(self):
        users = []
        try:
            async with _dedicated_connection() as db:
                users = [row[0] for row in await (await db.execute(
                    '''SELECT users.id FROM users LEFT JOIN sessions ON sessions.user_id=users.id
                    GROUP BY users.id ORDER BY MAX(sessions.created_at) DESC,users.id DESC LIMIT ?''', (MAX_ACCOUNTS,))).fetchall()]
            self.warming.update(users)
            for uid in users:
                await self._refresh(uid)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.states.clear()
            log.exception('ready_estimates state=warmup_failed')
        finally:
            self.warming.difference_update(users)

    async def stop(self):
        self.enabled = False
        self.loop = None
        for task in (self.warmup, self.task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self.states.clear()
        self.locks.clear()
        self.uncacheable.clear()
        self.warming.clear()
        self.task = self.warmup = None
        self.dirty = False


cache = ReadyEstimates()


class ReadyEstimateUpdates:
    """Finish eager maintenance before acknowledging a committed HTTP mutation."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def updated(message):
            if (message['type'] == 'http.response.start' and 200 <= message['status'] < 400
                    and scope.get('method') in ('POST', 'PUT', 'PATCH', 'DELETE')):
                await cache.flush()
            await send(message)
        await self.app(scope, receive, updated)
