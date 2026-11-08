"""Durable per-novel due times; one interruptible, sequential automatic worker.

Only actual attempts advance the schedule. A durable running marker survives
cancellation or process death; normal completion (including a recorded error)
clears it. Source I/O never holds the database writer.
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import logging
import random
import time

from app.config import settings
from app.database import get_connection, write_transaction

logger = logging.getLogger("novels.schedule")
_task = None
_changed = None


def _now():
    return time.time()


def _next_time(now):
    seconds = max(1, settings.novel_scrape_interval_minutes * 60)
    return now + seconds + random.uniform(0, seconds * 0.10)


def notify():
    if _changed is not None:
        _changed.set()


def _log(novel_id, due, reason):
    logger.info(
        "novel_id=%s next automatic sync=%s (%s)",
        novel_id,
        datetime.fromtimestamp(due, timezone.utc).isoformat(),
        reason,
    )


async def claim(novel_id, *, manual=False, scheduled=False):
    """Recheck eligibility/due time under the writer before any source request."""
    async with write_transaction() as db:
        row = await (
            await db.execute(
                """SELECT novels.active, novels.source_url,
            schedule.next_run_at, schedule.running FROM novels
            LEFT JOIN novel_sync_schedule schedule ON schedule.novel_id=novels.id
            WHERE novels.id=?""",
                (novel_id,),
            )
        ).fetchone()
        now = _now()
        if row is None or (not row["active"] and not manual):
            return None
        if (
            scheduled
            and row["next_run_at"] is not None
            and not row["running"]
            and row["next_run_at"] > now
        ):
            return None
        due = _next_time(now)
        await db.execute(
            """INSERT INTO novel_sync_schedule(novel_id,next_run_at,running)
            VALUES(?,?,1) ON CONFLICT(novel_id) DO UPDATE SET
            next_run_at=excluded.next_run_at,running=1""",
            (novel_id, due),
        )
        await db.commit()
    _log(novel_id, due, "manual" if manual else "automatic")
    notify()
    return row["source_url"]


async def finish(novel_id):
    async with write_transaction() as db:
        await db.execute(
            "UPDATE novel_sync_schedule SET running=0 WHERE novel_id=?", (novel_id,)
        )
        await db.commit()


async def _rows():
    db = await get_connection()
    try:
        rows = await (
            await db.execute("""SELECT novels.id, schedule.next_run_at, schedule.running
            FROM novels LEFT JOIN novel_sync_schedule schedule ON schedule.novel_id=novels.id
            WHERE novels.active=1 ORDER BY schedule.next_run_at,novels.id""")
        ).fetchall()
    finally:
        await db.close()
    missing = [r["id"] for r in rows if r["next_run_at"] is None]
    if missing:
        async with write_transaction() as db:
            await db.executemany(
                """INSERT OR IGNORE INTO novel_sync_schedule(novel_id,next_run_at)
                SELECT id,? FROM novels WHERE id=? AND active=1""",
                [(_now(), n) for n in missing],
            )
            await db.commit()
        return await _rows()
    return rows


async def _wait(delay):
    if delay is None:
        await _changed.wait()
    else:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_changed.wait(), timeout=max(0, delay))


async def _run():
    from app.novels import sync

    last_finished = None
    while True:
        _changed.clear()
        try:
            rows = [r for r in await _rows() if r["id"] not in sync._syncing_novel_ids]
            now = _now()
            due = [r for r in rows if r["running"] or r["next_run_at"] <= now]
            if due:
                failed = set()
                for row in due:
                    if last_finished is not None:
                        delay = settings.novel_scrape_request_delay_seconds - (
                            asyncio.get_running_loop().time() - last_finished
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
                    try:
                        await sync.sync_novel(row["id"], scheduled=True)
                    except Exception:
                        # sync_novel recorded its error; continue other due works.
                        failed.add(row["id"])
                    last_finished = asyncio.get_running_loop().time()
                if failed and any(
                    r["id"] in failed and (r["running"] or r["next_run_at"] <= _now())
                    for r in await _rows()
                ):
                    raise RuntimeError("Could not settle the automatic sync schedule")
                continue
            await _wait(min(r["next_run_at"] for r in rows) - now if rows else None)
        except Exception:
            logger.exception("Novel scheduler failed; retrying in 60 seconds")
            # The failed attempt's own completion notification must not
            # immediately bypass this backoff.
            _changed.clear()
            await _wait(60)


async def start():
    global _task, _changed
    if _task is not None and not _task.done():
        return
    rows = await _rows()
    now = _now()
    for row in rows:
        recovery = row["running"] or row["next_run_at"] <= now
        _log(
            row["id"],
            now if recovery else row["next_run_at"],
            "recovery due" if recovery else "restored",
        )
    _changed = asyncio.Event()
    _task = asyncio.create_task(_run(), name="novel-sync-schedule")


async def stop():
    global _task, _changed
    task, _task = _task, None
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    _changed = None
