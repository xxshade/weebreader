"""Daily scan of every library, on a schedule.

The library is otherwise kept up to date by the watcher (see watcher.py),
which is incremental and immediate -- but it only ever sees what happens
while the process is running. Anything that changed with the container
stopped, or that the filesystem never reported (some network and fuse
mounts deliver no inotify events at all), stayed invisible until someone
pressed "Rescan" by hand. This closes that gap without changing what a
scan does: it is exactly the same `scan_library()` the button runs.

Its own scheduler instance rather than the novels one (app/novels/sync.py):
they are unrelated pipelines with unrelated failure modes, and sharing an
instance would mean a shutdown of one tearing down the other's jobs.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import get_connection
from app.library.scanner import scan_library

logger = logging.getLogger("library.schedule")

scheduler = AsyncIOScheduler()


async def scan_all_libraries() -> None:
    """Scans every configured library, one after another -- deliberately
    not concurrently: a scan is I/O-bound on the same disks and takes the
    same single write lock, so running them in parallel would only make
    each one slower while the app is least responsive."""
    db = await get_connection()
    try:
        libraries = await (await db.execute("SELECT id, name, root_path FROM libraries")).fetchall()
    finally:
        await db.close()

    for library in libraries:
        logger.info("Scheduled scan starting: %s", library["name"])
        try:
            await scan_library(library["id"], library["root_path"])
        except Exception:
            # One unreachable library (an unmounted share, most likely)
            # must not cost the others their scan.
            logger.exception("Scheduled scan failed for library %s", library["name"])
    logger.info("Scheduled scan finished for %d libraries", len(libraries))


def start_scheduler() -> None:
    if scheduler.running:
        return
    if not settings.scheduled_scan_cron.strip():
        logger.info("Scheduled library scan disabled (scheduled_scan_cron is empty)")
        return

    try:
        trigger = CronTrigger.from_crontab(settings.scheduled_scan_cron)
    except ValueError:
        # A typo here must not stop the app from starting -- the watcher
        # and the manual rescan both still work without this job.
        logger.exception(
            "Invalid scheduled_scan_cron (%r), no scan scheduled", settings.scheduled_scan_cron
        )
        return

    scheduler.add_job(
        scan_all_libraries,
        trigger,
        id="scan_all_libraries",
        # A scan can outlast its own interval on a large library, and the
        # container may also be asleep at the scheduled time: max_instances
        # stops a second run piling onto a running one, and coalesce turns
        # several missed firings into a single catch-up run.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info(
        "Scheduled library scan: %s (timezone %s)", settings.scheduled_scan_cron, scheduler.timezone
    )


def stop_scheduler() -> None:
    """wait=False, same reasoning as the novel scheduler's: a scan in
    progress can run far longer than Docker's stop grace period, and
    blocking here would just mean SIGKILL mid-write instead of a clean
    exit. scan_library is idempotent, so an interrupted scan costs
    nothing beyond having to run again."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
