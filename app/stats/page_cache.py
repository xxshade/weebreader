"""Bounded Statistics query results, prepared between reader requests.

Every access pins and validates one SQLite generation. No HTML or authorization
is cached. A foreground miss executes the same queries as background preparation.
"""
import asyncio
import copy
import logging
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta

from app.config import settings
from app.stats import page_data
from app.stats.windows import resolve_window

logger = logging.getLogger('stats.cache')


class ReaderDeferred(Exception):
    """Only preparation yields; the foreground reader owns its own lifetime."""


@dataclass(frozen=True)
class Period:
    user_id: int
    days: int | None = 30
    start: str | None = None
    end: str | None = None

    @property
    def preset(self):
        return self.days in (30, 60, 90) and not (self.start or self.end)

    def window(self):
        return resolve_window(self.days, self.start, self.end)


def retained_size(value, seen=None):
    """Account Python object graphs, including the key, before retaining them."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    total = sys.getsizeof(value)
    if isinstance(value, dict):
        total += sum(retained_size(k, seen) + retained_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple, set)):
        total += sum(retained_size(v, seen) for v in value)
    return total


class StatisticsCache:
    MAX_BYTES = 8 * 1024 * 1024
    MAX_ENTRIES = 256
    MAX_CUSTOM_PERIODS = 16
    COALESCE_SECONDS = .150
    READER_QUIET_SECONDS = .025

    def __init__(self):
        self._lock = asyncio.Lock()
        self._connection = None
        self._identity = None
        self._path = None
        self._version = None
        self._calendar = None
        self._accounts = {}
        self.periods = OrderedDict()
        self.entries = OrderedDict()
        self.bytes = 0
        self._active = False
        self._worker = None
        self._clock = None
        self._dirty = False
        self._last_dirty = 0.
        self._reader_count = 0
        self._reader_epoch = 0
        self._last_reader = 0.
        self._idle = asyncio.Event()
        self._idle.set()
        self._stop_sql = threading.Event()
        self._preparing = False

    def _invalidate(self):
        self.entries.clear()
        self.bytes = 0
        self._version = None

    def _file_identity(self):
        path = settings.db_path.resolve()
        stat = path.stat()
        return str(path), stat.st_dev, stat.st_ino

    async def _drop_connection(self):
        self._invalidate()
        self._identity = None
        connection, self._connection = self._connection, None
        if connection is not None:
            await asyncio.shield(connection.close())

    async def _data_version(self):
        async with self._connection.execute('PRAGMA data_version') as cursor:
            return (await cursor.fetchone())[0]

    def _learn(self, period):
        for days in (30, 60, 90):
            self.periods.setdefault(Period(period.user_id, days), None)
        added = period not in self.periods
        self.periods[period] = None
        self.periods.move_to_end(period)
        while sum(not p.preset for p in self.periods) > self.MAX_CUSTOM_PERIODS:
            del self.periods[next(p for p in self.periods if not p.preset)]
        return added

    async def _reconcile_accounts(self):
        async with self._connection.execute('SELECT user_id, incarnation FROM stats_account_generations') as cursor:
            accounts = {r[0]: r[1] for r in await cursor.fetchall()}
        changed = {uid for uid, generation in self._accounts.items() if accounts.get(uid) != generation}
        if changed:
            self.periods = OrderedDict((p, None) for p in self.periods if p.user_id not in changed)
            self._invalidate()
        for uid in accounts:
            if uid not in self._accounts or uid in changed:
                self._learn(Period(uid))
        if accounts != self._accounts:
            self.notify()
        self._accounts = accounts

    def _check_reader(self, epoch):
        if epoch is not None and (self._reader_count or epoch != self._reader_epoch):
            raise ReaderDeferred()

    async def _read(self, name, uid, start, end, epoch):
        self._check_reader(epoch)
        # Library composition is global, but all account history/status is scoped.
        key = (name, None if name == 'library_totals' else uid, start, end)
        if key in self.entries:
            self.entries.move_to_end(key)
            return copy.deepcopy(self.entries[key][0])
        value = await page_data.load(self._connection, name, uid, start, end)
        self._check_reader(epoch)
        amount = retained_size((key, value))
        if amount <= self.MAX_BYTES:
            while self.entries and (len(self.entries) >= self.MAX_ENTRIES or self.bytes + amount > self.MAX_BYTES):
                _, (_, old_size) = self.entries.popitem(last=False)
                self.bytes -= old_size
            self.entries[key] = (copy.deepcopy(value), amount)
            self.bytes += amount
        return value

    async def _snapshot(self, period, *, partial=False, epoch=None):
        from app.database import get_connection
        async with self._lock:
            self._check_reader(epoch)
            try:
                identity = await asyncio.to_thread(self._file_identity)
                if self._connection is None or self._identity != identity:
                    await self._drop_connection()
                    if self._path is not None and self._path != identity[0]:
                        self.periods.clear()
                        self._accounts.clear()
                    self._connection = await get_connection()
                    await self._connection.execute('PRAGMA query_only=ON')
                    self._identity = identity
                    self._path = identity[0]
                calendar = page_data.calendar_token()
                version = await self._data_version()
                if version != self._version or calendar != self._calendar:
                    self._invalidate()
                    if epoch is None:
                        self.notify()
                self._version, self._calendar = version, calendar
                self._preparing = epoch is not None
                if self._preparing:
                    self._stop_sql.clear()
                    await self._connection.set_progress_handler(lambda: int(self._stop_sql.is_set()), 1000)
                    self._check_reader(epoch)
                await self._connection.execute('BEGIN')
                # Pin one view before inspecting any retained query result.
                await (await self._connection.execute('SELECT rootpage FROM sqlite_schema LIMIT 1')).fetchone()
                pinned = await self._data_version()
                if pinned != version:
                    self._invalidate()
                await self._reconcile_accounts()
                if period.user_id not in self._accounts:
                    # A queued period from a deleted account is never rebuilt.
                    if epoch is None:
                        from fastapi import HTTPException
                        raise HTTPException(401, 'Account no longer exists')
                    return None
                if epoch is None:
                    if self._learn(period):
                        self.notify()
                elif period not in self.periods:
                    return None
                window = period.window()

                async def read(name, uid, start, end):
                    return await self._read(name, uid, start, end, epoch)

                values = await page_data.collect(read, period.user_id, window, partial=partial)
                self._check_reader(epoch)
                await self._finish_transaction()
                after = await self._data_version()
                if (after != pinned or identity != await asyncio.to_thread(self._file_identity)
                        or calendar != page_data.calendar_token()):
                    self._invalidate()
                    self.notify()
                    # Foreground returns a coherent snapshot, never mixes generations.
                    # Do not retain it; the next request/preparation will validate anew.
                else:
                    self._version = after
                return window, values
            except BaseException as error:
                # Only expected reader preemption may retain completed query results.
                interrupted = isinstance(error, ReaderDeferred) or (
                    isinstance(error, sqlite3.OperationalError)
                    and getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT
                    and epoch is not None and epoch != self._reader_epoch)
                if interrupted:
                    try:
                        await self._finish_transaction()
                        after = await self._data_version()
                        if (after != self._version or self._identity != await asyncio.to_thread(self._file_identity)
                                or self._calendar != page_data.calendar_token()):
                            self._invalidate()
                        self._version = after
                    except BaseException:
                        await self._drop_connection()
                        raise
                    raise ReaderDeferred() from error
                await self._drop_connection()
                raise
            finally:
                self._preparing = False
                # Covers early returns as well as setup/pinning/validation failures.
                if self._connection is not None:
                    try:
                        await self._finish_transaction()
                    except BaseException:
                        await self._drop_connection()
                        raise

    async def _finish_transaction(self):
        await self._connection.set_progress_handler(None, 0)
        if self._connection.in_transaction:
            await self._connection.rollback()

    async def get(self, uid, days=None, start=None, end=None, *, partial=False):
        # Validate before retaining an interest or opening a connection.
        resolve_window(days, start, end)
        period = Period(uid, days or 30, start, end)
        try:
            return await self._snapshot(period, partial=partial)
        except (sqlite3.Error, OSError):
            # A private-reader failure does not poison a pooled/writer connection.
            logger.warning('Statistics reader failed; retrying with a fresh connection', exc_info=True)
            return await self._snapshot(period, partial=partial)

    def notify(self):
        if not self._active:
            return
        self._dirty = True
        self._last_dirty = time.monotonic()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._maintain())

    async def _wait_for_reader(self):
        while True:
            await self._idle.wait()
            delay = self._last_reader + self.READER_QUIET_SECONDS - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if not self._reader_count and time.monotonic() >= self._last_reader + self.READER_QUIET_SECONDS:
                return

    async def _prepare(self):
        # Discover all accounts without occupying the interactive read pool.
        from app.database import get_connection
        db = await get_connection()
        try:
            async with db.execute('SELECT user_id FROM stats_account_generations') as cursor:
                users = [row[0] for row in await cursor.fetchall()]
        finally:
            await db.close()
        for uid in users:
            self._learn(Period(uid))
        periods = sorted(list(self.periods), key=lambda p: ((30, 60, 90).index(p.days) if p.preset else 3, p.user_id))
        for period in periods:
            await self._wait_for_reader()
            epoch = self._reader_epoch
            await self._snapshot(period, epoch=epoch)
            # Release the Statistics lock/transaction between periods.
            await asyncio.sleep(0)

    async def _maintain(self):
        try:
            while self._dirty and self._active:
                while (delay := self._last_dirty + self.COALESCE_SECONDS - time.monotonic()) > 0:
                    await asyncio.sleep(delay)
                self._dirty = False
                try:
                    await self._prepare()
                except ReaderDeferred:
                    self._dirty = True
        except asyncio.CancelledError:
            raise
        except Exception:
            # Foreground remains usable; a subsequent write/access retries preparation.
            logger.exception('Statistics preparation failed')

    async def check_calendar(self):
        current = page_data.calendar_token()
        if current != self._calendar:
            self.notify()

    async def _watch_calendar(self):
        while True:
            tomorrow = date.today() + timedelta(days=1)
            midnight = time.mktime((*tomorrow.timetuple()[:3], 0, 0, 0, 0, 0, -1))
            await asyncio.sleep(max(.01, min(60., midnight - time.time())))
            await self.check_calendar()

    async def start(self):
        await self.close()
        self._active = True
        self._clock = asyncio.create_task(self._watch_calendar())
        self.notify()

    async def close(self):
        self._active = False
        tasks = [t for t in (self._worker, self._clock) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._worker = self._clock = None
        async with self._lock:
            await self._drop_connection()
        self._accounts.clear()
        self._path = None
        self.periods.clear()
        self._calendar = None
        self._dirty = False
        self._reader_count = 0
        self._reader_epoch = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._lock = asyncio.Lock()

    def reader_enter(self):
        self._reader_count += 1
        self._reader_epoch += 1
        self._idle.clear()
        self._stop_sql.set()
        # Only the connection currently owned by preparation may be interrupted.
        if self._preparing and self._connection is not None:
            with suppress(sqlite3.Error):
                self._connection._conn.interrupt()

    def reader_leave(self):
        self._reader_count -= 1
        self._last_reader = time.monotonic()
        if not self._reader_count:
            self._idle.set()


cache = StatisticsCache()


class StatisticsReaderPriority:
    """Track full reader response lifetimes, including streamed bodies."""
    def __init__(self, app):
        self.app = app

    @staticmethod
    def is_reader(path):
        import re
        return (path.startswith('/reader/') or path in {'/reading-time', '/reading-session'}
                or bool(re.match(r'^/novels/\d+/(?:read/|manifest$|resource/|progress$)', path))
                or bool(re.match(r'^/novels/(?:chapters|volumes)/\d+/read-status$', path)))

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not self.is_reader(scope['path']):
            return await self.app(scope, receive, send)
        cache.reader_enter()
        try:
            return await self.app(scope, receive, send)
        finally:
            cache.reader_leave()
