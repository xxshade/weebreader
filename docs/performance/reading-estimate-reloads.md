# Reading estimate reload performance

The v8.4.3 investigation found that making HTML renders cache-only did not make
reloads cheap. The browser still requested authoritative estimates after each
visit; that endpoint reopened anchored EPUB inventories, traversed novel bodies
and rewrote snapshots even when its inputs had not changed. Its CPU work and
cache contention delayed simultaneous Home requests.

The regression fixture contains 151 distinct 100,000-word EPUB archives, ten
saved reading anchors, 26 text series and two novels with 1,817 and 176 distinct
approximately 1,000-word chapters. Source inventories and final snapshots were
warmed before the following ASGI measurements. Authentication was overridden;
these numbers exclude a browser, network/proxy and startup jobs.

| Warm operation | v8.4.3 | Word-count snapshot reuse |
| --- | ---: | ---: |
| Unchanged estimate request, wall time | 846–1,134 ms | 101–156 ms |
| Unchanged estimate request, process CPU | 761–942 ms | 78–109 ms |
| Home during an estimate request, wall time | 640–776 ms | 165–280 ms |
| Home alone, wall time | typically 150–185 ms | typically 75–107 ms |
| Home from series, wall time | typically 164–185 ms | 77–122 ms |

Before the change, every warm estimate request performed ten full EPUB inventory
lookups, 141 EPUB total lookups, 1,993 novel total lookups and 153 snapshot writes.
The corrected warm request performs none of those operations. Each estimator
reads the bounded snapshot index once (typically 0.45–0.7 ms in this local probe),
checks exact account/scope/content/position/rule signatures and applies current
pace to the cached word counts. Lightweight browser revalidation remains enabled
so changes occurring during HTML generation can still be incorporated. CPU measured during overlapping requests includes
both requests and must not be added together as independent request CPU.

Setting anchors to the beginning reduced the reproduced interference; removing
only novel targets did not eliminate it. A temporary snapshot-reuse control
also removed the overlap failure before the implementation was changed. That
control did not establish invalidation correctness; backend regressions now
exercise pace, anchors, file changes, replacement rules, chapter edits,
completion, volume membership and old/corrupt cache recovery.

## Browser regression

`python scripts/test_reload_performance_browser.py` runs the maintained real-HTTP
regression with the browser flags documented in
[Desktop browser validation](../testing/browser-checks.md). The required text ETA
CI suite invokes it automatically. It restarts the real app with the entire
fixture, authenticates normally, warms all scopes and measures three repeats of:

- ordinary full reload;
- HTTP-cache-bypassed reload (Chrome CDP and Firefox BiDi);
- the actual Home link from a series;
- Home while an independent estimate request runs on the same server.

A script installed before document parsing measures the visible, initialized
Home page after paint frames; the controls are also checked independently.
Estimate completion is tracked separately, excluding the permanent SSE stream.
Every individual usability and server process CPU measurement is emitted as
JSON. The median usability of each action's three repeats must be at most 500 ms
on the local runner; server CPU is measured and reported separately. Stable snapshot rows, including
row IDs, provide a deterministic guard against repeated writes. A control
restores redundant endpoint calculations and must fail in both browsers.

Both desktop and 320px viewport scenarios are required in Chrome and Firefox.
Those are simulations, not native LibreWolf or Vivaldi mobile measurements, and
localhost results do not establish the deployment's network or server latency.

## Cold preparation and upgrades

Cold fixture preparation is reported separately from the warm budget. The first
conversion of the investigation's already-warm duration snapshots took 1,566 ms
wall / 1,279 ms CPU and wrote 153 word-count snapshots once. A completely fresh
synthetic shelf also needs to build its source inventories and takes longer.
Legacy durations remain visible while their scopes are upgraded. No cache
removal or history reset is required. Subsequent visits reuse source counts;
changes to reading pace only require multiplication by the current learned rate.

## Replacement rule queries

Word-count reuse still needs the current text replacement rules to validate each
snapshot. Previously, preparing 151 EPUBs in 26 series repeated that query once
per EPUB. Each request's account-bound estimator now reuses rules per series or
novel, including empty results. Novel volumes share their novel's rules, while
series and novels with the same numeric ID remain distinct. A new request starts
with no rules cached, so additions, edits and deletions invalidate counts as
before.

On the warmed server fixture above, this reduces EPUB rule queries from 151 to
26 and total aiosqlite worker calls for Home from 366 to 116. Worker calls include
execution and result fetching; they are not a count of SQL statements. Backend
tests trace the real authoritative endpoint and the cache-only HTML estimator,
including multiple preparation batches, concurrent accounts and rule edits
between requests. This does not change the SQLite engine, connection pool or
write serialization.

## Bounded file checks

With rules reused, the next comparison found that 151 eligible EPUBs still
submitted 151 separate worker tasks for their filesystem `stat()` calls. The
estimator now prepares eligible items first and submits at most 32 file checks
per worker task, reducing those submissions to five on this fixture. File size
and modification time still participate in every request's fingerprints; no
file metadata is retained across requests. Rules, positions, current pace and
source inventory invalidation are unchanged.

The worker returns each file's result or error independently, including invalid
paths. Estimation keeps healthy results and identifies unavailable items in a
partial total. Each batch finishes before the next is submitted, so task
cancellation can leave only its bounded active batch running. Completed items,
unusable source positions and insufficient pace evidence need no file checks.

Backend tests exercise both the authoritative endpoint and cache-only HTML
preparation with 65 eligible files plus excluded items. They count actual source
checks, assert worker-thread execution and batch bounds, and exercise changed,
missing, invalid and repaired paths across requests and cancellation. Local
Xeon measurements are comparisons, not emulation of an Intel N100 or energy
measurements; CPU time alone cannot establish watts saved.

## Grouped estimate queries

Request-local rule reuse still issued one query per distinct series, and the
estimate endpoint prepared every requested series separately. Multiple series
now share bounded rule queries (500 series per query, scoped to the account),
including empty rule sets and preserving rule order. A single series retains
the original lazy lookup. Novel rules remain scoped independently.

The endpoint gathers validated series targets before preparing their unread
items once through the same aggregation used by Home. The selected scope uses
an equality query for one series and an `IN` query for multiple series; an empty
selection returns without querying. This does not broaden the 100-target limit
or truncate the number of books within the requested series. Individual item
targets, including orphans and completed items, remain independent, and results
retain the deduplicated request order. File checks retain their sequential
32-file batch bound and remain fresh on each request.

## Reusing snapshot reads and grouping revisions

Prepared estimate validation still checks the main database revisions and the
snapshot generation before and after its work. Main-database revision reads now
use one compound SELECT for the global generation, account pace and work
revisions. This reduces six SELECTs per stable validation to two without caching
revision values, and each compound read observes one SQLite snapshot.

The disposable snapshot database has one serialized read-only connection. Each
borrow checks file identity and schema version, reads a fresh generation, and
ends its read transaction before returning. Generation and index reads share a
transaction. Missing tables, triggers or generation rows use the existing repair
path; file replacement reopens the connection, and corruption fails closed.
The validation token includes file/schema identity so recreating a table without
changing the persisted revision cannot validate an old RAM projection. Snapshot
writes keep their existing short-lived connections and invalidation notifications.
The persistent reader closes after request and background work stops. Cancelled
API database reads retain the existing discard-on-error behavior.

An alternating local HTTP comparison of the integrated change used 729 EPUBs
across 59 series, two novels and a single 64-target estimate request. All 729
files remained checked, response JSON matched, and no fallback occurred:

| Warm operation | Previous | Combined reads |
| --- | ---: | ---: |
| Stable home, backend median | 7.52 ms | 6.64 ms |
| Stable estimate API, backend median | 5.23 ms | 4.69 ms |
| Home after saved progress, backend median | 8.16 ms | 7.68 ms |
| Estimate API after saved progress, backend median | 5.38 ms | 4.60 ms |

Each scenario used 36 alternating samples per variant. The progress scenario
saved a valid anchor without new pace evidence; it is not a full reading session.
Earlier prototype comparisons with reversed startup order found similar API
gains. These are local synthetic measurements, not N100 or browser latency
predictions. A separate prototype CPU experiment reduced process CPU per home +
API pair from 12.34 to 10.94 ms; this does not measure electrical power.

The new resource cost is one persistent connection and its SQLite cache. In an
isolated 4,096-work-revision stress probe, grouped revision rows retained about
53 KiB more Python allocations than the reader alone. Actual libraries have a
different number of work revisions than files. Python allocation tracking excludes
SQLite C allocations, and these observations are not a universal memory budget.
