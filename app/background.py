"""Shared fire-and-forget background task helper.

`asyncio.create_task(coro)` on its own is a trap: the event loop only keeps
a WEAK reference to the returned Task, so the garbage collector can destroy
it mid-execution with no exception and no trace in the logs (a documented
asyncio gotcha -- see asyncio.create_task's own docs: "Save a reference to
the result of this function, to avoid a task disappearing mid-execution").
This is the real cause of a bug once seen in this project: some library-scan
files silently never got processed, non-deterministically between runs.

`run_background()` keeps a strong reference in a module-level set until the
task finishes, and also logs any exception that escapes the coroutine --
previously, a task that raised outside its own internal try/except was
silently swallowed by asyncio unless the GC happened to collect it in time
to print "Task exception was never retrieved".
"""

import asyncio
import logging

logger = logging.getLogger("background")

_background_tasks: set[asyncio.Task] = set()


def _log_if_failed(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task %r ended with an unhandled exception", task.get_name(), exc_info=exc)


def run_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_log_if_failed)
    return task
