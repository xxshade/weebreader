# Prepared Statistics

Statistics caches query results in RAM, not HTML, authentication or account
settings. The page and daily-activity partial use the same validated data path.
A cache miss runs current queries before rendering; warming never makes an old
snapshot authoritative.

The application prepares 30/60/90 days for every account in the background after
startup and application writes. Consulted custom ranges join preparation until
restart or bounded-interest eviction. There is no inactivity timeout. Writes
coalesce for 150 ms. This deliberately spends CPU even when Statistics is not
visited, in exchange for prepared results after reading. Continuous writes or
reader traffic can defer completion; an open reader alone does not pause it.

Reader HTTP requests take priority until their response bodies finish. Only the
Statistics background connection is interrupted. Preparation yields, removes its
SQLite progress handler and ends the read transaction, then resumes after a
25 ms quiet interval. Completed queries survive a yield only when the database
and calendar generation remain unchanged. Foreground Statistics, authentication
and reader writes never receive that interruption. Transactions/locks are released
between prepared periods, with 30 days first.

Each access checks the main file identity, `PRAGMA data_version` on the same
persistent query-only connection, local date and timezone. Queries share one
pinned SQLite snapshot. A change during collection discards retained results;
the response still comes from one coherent snapshot, and preparation retries.
External commits are discovered on the next access, not by continuous DB polling.
A calendar timer prepares the new day without requiring a Statistics visit and
checks at most once per minute for wall-clock/timezone adjustments.

`stats_account_generations` distinguishes account creations, including deletion
and recreation with identical numeric IDs/profile fields. Its tokens are not
credentials. Existing accounts are backfilled idempotently at startup; insert
and delete triggers maintain identity. New accounts never inherit the deleted
account's consulted custom ranges. Sessions continue to be checked for each
request.

The process retains at most 256 query results under an 8 MiB accounted Python
object budget and 16 consulted custom periods. Preset interests are retained
for every account; entries can be evicted when the budget cannot hold them all.
Oversized/missing results use current calculation. The bound covers retained
result/key graphs, not total RSS, response rendering or temporary SQL buffers.
Prepared data is process-local; multiple server processes prepare independently.

Recovery follows the documented offline restore procedure. Stop the application,
restore a consistent backup and restart; derived RAM state is empty and rebuilt.
Hot replacement of a live WAL database is not a supported restore procedure.
Changing the configured database path drops the private reader and old interests.
Unexpected errors/cancellation close the private reader; the next access can open
a fresh connection. Shutdown cancels maintenance and closes its connection before
pool cleanup.

Statistics queries now calculate only what is shown: history existence/active
days, the comic visit average and daily comic visits alongside all-media time.
Legacy visits, repeated checkpoints and per-row time caps retain their meaning.
The unused completion calculation is removed from the page; shared query APIs,
including `time_to_finish`, remain available unchanged.

## Validation

`tests.test_statistics_cache` covers warm reuse, separate accounts, identical
account recreation, external commits, calendar change, reader preemption,
validated partial retention, cancellation/recovery, restart, budgets and commit
notification ordering. Existing Statistics/query/layout tests remain required.
The local research additionally exercised consistent offline restore and timezone
boundary spans. Chrome and Firefox desktop plus 320 px simulations validate the
rendered page and daily controls; viewport simulations are not native Android.

Performance comparisons must use isolated data, unchanged browser versions and
alternating requests. Warm medians are not first-navigation guarantees and local
results are not predictions for an Intel N100 or measurements of electricity.

### Local application comparison (2026-11-06)

Synthetic fixture: 100,000 activity rows, two accounts, 729 EPUBs plus comic and
novel data. Twenty alternating requests per route/variant, warmed beforehand.
Bodies matched after normalizing only process-specific SSE cursors and origin
ports. These are backend `app` medians on the local executor, not N100 forecasts.

| Route | Current main | Prepared cache |
| --- | ---: | ---: |
| `/stats` | 389.7 ms | 9.6 ms |
| `/stats?days=60` | 446.8 ms | 12.2 ms |
| `/stats/daily-activity?days=90` | 156.8 ms | 12.0 ms |
| `/stats` with a consulted eight-day custom window | 342.6 ms | 7.1 ms |

This isolates the warm-navigation benefit, not preparation CPU, first-visit or
power consumption. Earlier prototypes showed meaningful background CPU cost;
eager preparation remains a deliberate tradeoff. Production-path browser controls
passed in Chrome 157.0.8103.38 and Firefox 159.0.1 at 1280/320 px, including
EPUB/comic/novel requests during preparation, completion with the reader visible,
preset/custom changes and fresh data after writes. `python scripts/test_statistics_cache_mutations.py` runs fourteen
deliberate-defect controls for cache protections/timing reuse, restoring source
after each control. The backend CI runs these controls after the unit suite.
