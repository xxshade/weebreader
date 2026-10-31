# Prepared reading estimates

The home, work cards and estimate API reuse account-scoped estimates prepared
by the backend. EPUBs and novels retain their word-based calculations, including
own-work and shared text evidence. Comics retain their per-series rates,
conservative fallback and own-item evidence. Mixed series aggregate the same
native estimates. No percentages are used to extrapolate text predictions.

## When preparation happens

Startup starts preparation in the background after database initialization and
backfills. The server accepts requests immediately. While preparation is busy,
a request uses the existing calculation instead of waiting for its account's
preparation lock. Cold HTML still never waits for source-text parsing.

A committed application write schedules maintenance after the writer lock is
released. Successful mutating HTTP responses await the current maintenance task
before sending headers. Updates to saved text positions may prepare their work's
source counts outside the event loop; changes to reading pace reuse counts and
apply the existing per-work/comic calculations. Newly sufficient pace does not
force every cold book in the library to be parsed during one activity submission.
The authoritative estimate endpoint still computes unavailable counts on demand.

Maintenance acknowledgements are bounded: at most two rounds in one task; later
commits are coalesced into a following task. Continuous concurrent writes, an
unavailable cache or an exhausted generation retry can leave the projection
unprepared. Requests then validate again and use the original path. Prepared
estimates are an optimization, never the source of progress or reading history.

## Invalidation and account isolation

Triggers in the main database increment source, account-pace and work revisions
inside the input mutation's transaction. Progress normally affects one series
or novel. Comic progress can also change other series' fallback rate; retained
removed-comic evidence remains part of that calculation. Content, membership,
chapter order/newest metadata, rules and account-identity changes invalidate the
appropriate projection. Rollbacks do not publish a new revision.

The disposable snapshot SQLite has its own epoch and revision, incremented in
the same commit as a snapshot insertion, update or eviction. Losing a Python
notification therefore cannot leave RAM valid indefinitely. A changed snapshot
index is matched against hashed account/work scopes; unchanged entries do not
need JSON decoding. Old duration snapshots still pass through the authoritative
endpoint to upgrade to word counts.

The backend checks revisions before and after building a generation. A retry
uses a fresh estimator, including fresh pace evidence. After three attempts or
an unavailable revision, it uses the original path. File size/mtime checks remain
off the event loop in batches of at most 32, including when filesystem watcher
events were missed. No archive resource-size limit was added.

All HTTP authentication remains in the original handlers/dependencies. Accounts
have separate projections and namespaces including the existing account identity
signature. No live database connection or source chapter text is retained in RAM.
Shutdown cancels and awaits preparation/maintenance before closing database pools.
Each worker has its own RAM; durable revisions protect against missed in-process
notifications and writes by another worker. A different worker may pay the update
cost on its next request rather than anticipating it.

## Novel position changes

For a change within the same chapter, the native inventory can calculate the
change in remaining words from the previous anchor to the current anchor. This
is allowed only when the content generation, unread chapter-set signature and
replacement-rule signature still match. A volume containing neither position
can reuse its unchanged count. Membership/rule changes, incompatible positions,
legacy durations and changes of chapter use the complete native calculation.
Whole-novel and volume scopes remain distinct.

## Bounds and operational tradeoffs

RAM retains at most four account projections, evicting the least recently used.
Each projection is limited to 4,096 unfinished items plus novel scopes (whole
novels and volumes). The persisted snapshot index independently retains its
existing 4,096-entry bound. Libraries exceeding the projection limit continue
using the original calculation. These are derived-cache bounds, not import or
reading limits. Deleting a user clears its revision rows; deleting a work clears
its obsolete work revisions.

The cache improves stable navigation at the cost of memory and additional work
at save time. Restart loses the RAM projection; background preparation rebuilds
it from the database and existing snapshots. It does not render HTML templates,
so the first HTML request can still cost more than later requests.

The earlier local EPUB prototype, after three quiet seconds following startup,
measured first-home server medians of 154 → 106 ms and subsequent requests of
85 → 42 ms on 2,048 synthetic EPUBs. Immediate navigation *during* preparation
was slower. Those measurements describe a prototype on an EPYC host, not this
all-format implementation or an Intel N100. Actual reading-session CPU and
memory costs must be considered alongside navigation measurements.

## Verification

`tests/test_ready_estimates.py` compares projections to native calculations for
all formats and mixed series; it covers exact positions, partial results,
legacy upgrade, account namespaces, invalidation without notifications,
rollback, unavailable revisions, generation races, eviction, bounded maintenance
and startup/shutdown. Native inventory comparison independently checks novel
position deltas rather than trusting the snapshots the delta itself saved.

The text ETA browser suite covers real HTTP/SSE updates, volume scopes, warm
reloads, partial/unavailable estimates and recovery in Chrome and Firefox at
1280px and 320px. Its deliberately broken volume-scope control changes parsed
request scope so it exercises both cached and native paths.
