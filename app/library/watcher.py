"""
Incremental file watcher (see the architecture document, section 7).

`watchdog` runs on its own thread (it's synchronous); events are handed off
to the asyncio event loop via `asyncio.run_coroutine_threadsafe`, with
per-file debouncing so a large file isn't reprocessed multiple times while
it's still being copied.
"""

import asyncio
import concurrent.futures
import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.config import settings
from app.library.scanner import (
    _inside_ignored_folder, _is_eligible, _walk, mark_folder_missing, mark_missing, process_file,
)

logger = logging.getLogger("watcher")

_MAX_SETTLE_ATTEMPTS = 20

IMPORTS_AT_ONCE = 3


def _stat_signature(path: Path) -> tuple | None:
    """(size, mtime) -- both, because neither alone is enough: a client
    that pre-allocates the whole file keeps the size fixed while it fills
    it in, and a file copied in one burst can land with the mtime it had
    at the source."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, library_id: int, root_path: str, loop: asyncio.AbstractEventLoop):
        self.library_id = library_id
        self.root_path = root_path
        self.loop = loop
        self._pending: dict[str, threading.Timer] = {}
        self._generations: dict[str, object] = {}
        self._sizes: dict[str, tuple | None] = {}   # stat at scheduling time, to spot a file still growing
        self._settle_attempts: dict[str, int] = {}
        # dest path -> the path it was renamed from, for an import that is a
        # rename (see on_moved).
        self._moved_from: dict[str, str] = {}
        self._lock = threading.Lock()
        # At most IMPORTS_AT_ONCE imports per library. Every debounced file
        # used to start its own process_file at once and queue for the
        # global write lock: with 100 files dropped in, a progress save
        # arriving a second later waited 7.6 s (2.2 s once the cover was
        # encoded outside the lock). Measured with the same 100 files:
        # one at a time, 0 ms but 9.5 s to import them; three, 0 ms and
        # 3.9 s; four, 14 ms and 3.3 s. Created on the loop, by the first.
        self._import_slots: asyncio.Semaphore | None = None

    def _wanted(self, path: Path) -> bool:
        return _is_eligible(path) and not _inside_ignored_folder(path, Path(self.root_path))

    async def _with_a_slot(self, work):
        if self._import_slots is None:
            self._import_slots = asyncio.Semaphore(IMPORTS_AT_ONCE)
        async with self._import_slots:
            await work()

    def _schedule(self, path_str: str, expected=None):
        path = Path(path_str)
        if not self._wanted(path):
            return

        with self._lock:
            if expected is not None and self._generations.get(path_str) is not expected:
                return
            existing = self._pending.pop(path_str, None)
            if existing:
                existing.cancel()
            self._sizes[path_str] = _stat_signature(path)
            generation = object()
            self._generations[path_str] = generation
            timer = threading.Timer(settings.watcher_debounce_seconds, self._fire, args=(path, generation))
            self._pending[path_str] = timer
            timer.start()

    def _fire(self, path: Path, generation):
        path_str = str(path)
        with self._lock:
            if self._generations.get(path_str) is not generation:
                return
            self._pending.pop(path_str, None)
            scheduled_signature = self._sizes.pop(path_str, None)
        if not path.exists():
            with self._lock:
                if self._generations.get(path_str) is not generation:
                    return
                self._generations.pop(path_str, None)
                self._settle_attempts.pop(path_str, None)
                moved_from = self._moved_from.pop(path_str, None)
            if moved_from is not None:
                self._schedule_delete(moved_from)
            return

        # A file still being copied into the library is the normal way to
        # get a half-written one imported. If it grew (or was touched)
        # between being scheduled and now -- a whole debounce interval --
        # it is still arriving, so wait for another quiet interval instead
        # of reading it. Bounded, because a file that keeps changing
        # forever must still eventually be looked at; process_file's own
        # retry is what covers the case this heuristic cannot see (a
        # writer that pauses for longer than the debounce looks exactly
        # like one that finished).
        if scheduled_signature is not None and _stat_signature(path) != scheduled_signature:
            with self._lock:
                if self._generations.get(path_str) is not generation:
                    return
                attempts = self._settle_attempts.get(path_str, 0) + 1
                self._settle_attempts[path_str] = attempts
            if attempts <= _MAX_SETTLE_ATTEMPTS:
                logger.info("Still being written, waiting: %s", path)
                self._schedule(path_str, expected=generation)
                return
            logger.info("Still changing after %d checks, processing anyway: %s", attempts, path)
        with self._lock:
            if self._generations.get(path_str) is not generation:
                return
            self._generations.pop(path_str, None)
            self._settle_attempts.pop(path_str, None)
            moved_from = self._moved_from.pop(path_str, None)

        async def work():
            imported = await process_file(self.library_id, self.root_path, path)
            # A rename: process_file has taken the old row over by its
            # content (scanner._moved_item). Forgetting the old path only
            # AFTER that is the point -- removing it first, as a separate
            # delete racing the import did, deleted the item with its
            # reading progress before it could be followed.
            if imported and moved_from is not None and not Path(moved_from).exists():
                await mark_missing(self.library_id, moved_from)

        self._submit(self._with_a_slot(work))

    @staticmethod
    def _log_future_failure(future):
        try:
            future.result()
        except concurrent.futures.CancelledError:
            return
        except Exception:
            logger.exception("Watcher background operation failed")

    def _submit(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        future.add_done_callback(self._log_future_failure)

    # Same debounce/lock as _schedule/_fire (create/modify) -- using the
    # SAME `_pending` dict, keyed by the same path, is what makes a pending
    # create/modified get cancelled if the file is removed right after, and
    # vice versa: a pending delete gets cancelled if the file reappears
    # (e.g. an editor that deletes and recreates, or a cut-and-paste too
    # fast for watchdog to recognize as "moved").
    def _schedule_delete(self, path_str: str):
        path = Path(path_str)
        if not _is_eligible(path):
            return

        with self._lock:
            existing = self._pending.pop(path_str, None)
            if existing:
                existing.cancel()
            self._sizes.pop(path_str, None)
            self._settle_attempts.pop(path_str, None)
            generation = object()
            self._generations[path_str] = generation
            timer = threading.Timer(settings.watcher_debounce_seconds, self._fire_delete, args=(path, generation))
            self._pending[path_str] = timer
            timer.start()

    def _fire_delete(self, path: Path, generation):
        with self._lock:
            if self._generations.get(str(path)) is not generation:
                return
            self._pending.pop(str(path), None)
            self._generations.pop(str(path), None)
        if path.exists() and not _inside_ignored_folder(path, Path(self.root_path)):
            return  # reappeared before the debounce fired -- nothing to do
        self._submit(self._with_a_slot(lambda: mark_missing(self.library_id, str(path))))

    # A FOLDER deleted or moved. inotify reports a folder moved out of the
    # library (or to the trash) as the folder deleted, with no event for
    # any file in it; and a folder renamed inside it as one move. So both
    # are handled at the folder: what arrived under the new name is
    # imported (each file taking its old item over by content), and what
    # is confidently gone under the old one is removed.
    def _schedule_folder(self, src: str, dest: str | None):
        key = "folder:" + src
        with self._lock:
            existing = self._pending.pop(key, None)
            if existing:
                existing.cancel()
            generation = object()
            self._generations[key] = generation
            timer = threading.Timer(settings.watcher_debounce_seconds, self._fire_folder,
                                    args=(key, src, dest, generation))
            self._pending[key] = timer
            timer.start()

    def _fire_folder(self, key: str, src: str, dest: str | None, generation):
        with self._lock:
            if self._generations.get(key) is not generation:
                return
            self._pending.pop(key, None)
            self._generations.pop(key, None)
        root = Path(self.root_path)

        async def work():
            imported = True
            if dest is not None and not _inside_ignored_folder(Path(dest) / "x", root):
                paths, errors = await asyncio.to_thread(_walk, Path(dest))
                imported = not errors
                for path in paths:
                    if self._wanted(path):
                        if not await process_file(self.library_id, self.root_path, path):
                            imported = False
            if imported:
                await mark_folder_missing(self.library_id, src)

        self._submit(self._with_a_slot(work))

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            self._schedule_folder(event.src_path, event.dest_path)
            return
        if self._wanted(Path(event.dest_path)):
            # Imported under its new name, and the old one forgotten AFTER
            # (see _fire) -- not as a separate delete racing the import.
            with self._lock:
                self._moved_from[event.dest_path] = event.src_path
                pending = self._pending.pop(event.src_path, None)
                self._generations.pop(event.src_path, None)
                if pending:
                    pending.cancel()
            self._schedule(event.dest_path)
        else:
            # Out of the library, into a folder it ignores (the desktop's
            # trash on a USB drive), or renamed to something it does not
            # read: gone.
            self._schedule_delete(event.src_path)

    def on_deleted(self, event):
        if event.is_directory:
            self._schedule_folder(event.src_path, None)
        else:
            self._schedule_delete(event.src_path)


class LibraryWatcher:
    """One watchdog Observer per library."""

    def __init__(self):
        self._observers: dict[int, Observer] = {}

    def start(self, library_id: int, root_path: str, loop: asyncio.AbstractEventLoop):
        if library_id in self._observers:
            return
        handler = _DebouncedHandler(library_id, root_path, loop)
        observer = Observer()
        observer.schedule(handler, root_path, recursive=True)
        observer.start()
        self._observers[library_id] = observer
        logger.info("Watcher started for library %s at %s", library_id, root_path)

    def stop_all(self):
        for observer in self._observers.values():
            observer.stop()
            observer.join(timeout=5)
        self._observers.clear()


watcher = LibraryWatcher()
