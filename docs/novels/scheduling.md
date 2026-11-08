# Web novel synchronization schedules

Each novel has its own next automatic synchronization time, saved in the SQLite
library database. Restarting the container preserves future times and logs each
active novel's planned execution, with an explicit UTC offset. Keep the existing
data volume to retain these schedules.

The interval is configured by `APP_NOVEL_SCRAPE_INTERVAL_MINUTES` in the container
environment (default 360 minutes). The existing random delay of zero to ten
percent is retained. The actual chosen time is saved, so restarting does not
choose another random delay. Changing the interval preserves already planned
executions; the new interval applies when each novel next starts a sync.

A completed or failed attempt starts its next interval from the start of that
attempt. **Sync now**, including the first sync after adding a novel, postpones
only that novel's automatic time. It does not postpone other novels. A duplicate
request while that novel is already syncing is skipped without another schedule
change. Automatic work checks eligibility and the saved deadline again before
starting, so an old queue selection cannot repeat a subsequent manual sync.

## Startup and recovery

| Saved state | Behavior |
| --- | --- |
| Future execution | Restore it and wait; no startup scraping |
| No saved schedule | Save one and perform one recovery sync in the background |
| One or several missed times | Perform one recovery; do not replay each missed time |
| Interrupted attempt | Recover once, even if a future time had already been saved |
| Attempt finished with an error | Keep the recorded error and wait for the normal interval; manual retry remains available |
| Paused novel | Skip automatic work; manual sync remains available |
| Re-enabled novel with a missed time | Recover once |

A recovery starts a new interval. With a six-hour interval and no random delay,
recovering at 15:00 means the next attempt is at 21:00. Downloaded chapter bodies
are reused; recovery does not download the whole novel again.

The first upgrade has no saved schedules, so active novels receive one recovery
sync. Later restarts with future schedules do not trigger that pass.
No manual database migration or configuration change is required.

## Execution and persistence

A durable running marker is committed with the next due time before contacting
the source. Completing an attempt, including recording an ordinary source
error, clears the marker. Cancellation or process death leaves it for recovery.
Short transactions never span source requests. Deleting a novel also deletes its
schedule. Schedule metadata does not alter reading progress or chapter bodies.

One application worker runs automatic syncs sequentially and maintains the
configured delay between novels. It sleeps until the earliest due time or a
manual/eligibility notification; it does not repeatedly poll while idle.
Shutdown cancels and awaits the automatic task before closing database resources.
A persistence failure that prevents settling an attempt backs off rather than
immediately spinning on the same due work.

Tests use a scripted source and controllable clock to exercise fresh workers,
missed/interrupted attempts, the manual route, stale queue selections, real timer
wakeup, pause/resume and deletion. Deliberate defects verify that these
regressions detect the behavior they guard.
