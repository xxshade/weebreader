# Measuring HTML request processing

HTML responses include `Server-Timing: app;dur=42.500`, with the elapsed
application time in milliseconds. The measurement starts in the existing page
middleware and includes authentication, handler work, HTML body preparation and
ETag calculation. Each conditional request receives its own measurement, even
when its response is 304 with no body. HTML fragments use the same measurement.
The value is a response header and never changes the HTML body or its ETag.

The metric is wall time, including waits during processing, not CPU time. It
excludes work before entering the middleware, network transit, Caddy processing,
body transfer and browser rendering. JSON, static assets and persistent SSE
streams do not receive this metric. There is no database write or per-request
logging for these measurements.

## Reading the measurement

1. Open browser developer tools, then Network.
2. Reload the home page and select its `/` document request.
3. Record its HTTP status (200 or 304), Waiting time and Server-Timing value.
   The header is available under Response Headers, and supporting browsers also
   expose the application metric in their timing details.
4. Repeat under comparable conditions. Compare medians and slow requests
   separately; retain the status and distinguish a normal reload from bypassing
   the HTTP cache.

For example, Waiting of 155 ms and an application measurement of 42.5 ms leave
roughly 112.5 ms outside the measured interval. This difference does not isolate
Caddy: it also includes the network, scheduling outside the measurement and
other unmeasured work. Browser session-history restores may make no HTTP request
at all, so they do not produce a fresh application measurement. Estimates,
images and SSE updates after HTML arrival are separate requests and activities;
this metric does not measure when the entire page becomes usable.

A local Caddy reverse-proxy check verifies header preservation for 200 and 304.
The deployed proxy configuration must also be checked: it can remove or replace
headers. No changes to Caddy are normally required to forward Server-Timing.

Only the total `app` duration is reported initially. Detailed query, rendering
and CPU diagnostics should be added only if this measurement points to relevant
application work; per-process CPU time cannot reliably be attributed to one
concurrent request.

## Optional home-page phases

To investigate a recurring home-page cost, set
`APP_DETAILED_SERVER_TIMING=true` in the application container's environment and
recreate that container. The sample Compose file forwards this variable from
its environment or `.env`; it defaults to `false`. This option does not change
calculations, queries or cache decisions. This home breakdown covers GET requests
to `/`, including query-string variants. The same flag also enables the estimate
API breakdown below. Installation settings control both; request parameters and
headers cannot enable them.

The response keeps the total `app` metric and adds the following elapsed phases:

| Metric | Interval |
| --- | --- |
| `db_acquire` | Acquiring the primary request read connection, including waiting for a pool slot and opening/configuring a connection when needed. This is not the time spent executing all SQL. |
| `auth` | Session and account validation after acquiring that connection, including its query. |
| `home_data` | Preparing home data and cards, including queries, cached estimate preparation and file checks. |
| `render` | Rendering the home template after preparing its context. |
| `revalidate` | Collecting the HTML body, hashing it and preparing the 200/304 response. |
| `other` | The nonnegative residual of `app` after subtracting those phases, including framework handling and gaps between measured phases. |

These top-level phases are sequential and must not be added to `app`: together with
`other`, they account for that total, within rounding precision. They are not
nested SQL spans or CPU measurements. Initial pool and template warming can
change the distribution; distinguish the first access from repeated 304s.
Diagnostics themselves add some overhead, so detailed results should not be
compared as if this option were free.

After deploying, record the entire Server-Timing header and status from five
normal home reloads. The largest phase directs the next investigation; a large
`other` value calls for finer measurement rather than guessing its cause.
Then disable the option and recreate the container again. No history, SQL,
credentials, user identifiers or file names are recorded by these phases, and
no per-request diagnostic logs or database writes are added.

### Inside home data preparation

The same option also reports these sequential parts of `home_data`:

| Metric | Interval |
| --- | --- |
| `home_queries` | Initial library, series, novel, status, hidden-status and reading-recency queries, including fetching rows and building lookup maps. This is not SQL engine time alone. |
| `home_estimator` | Preparing the shared cached-only reading pace estimator. |
| `home_series` | Preparing series reading indicators, including its queries, cached estimates and file checks. |
| `home_novels` | Preparing novel reading indicators. |
| `home_tracking` | Fetching tracking-service link counts. |
| `home_cards` | Constructing series and novel cards and sorting their combined list. |
| `home_reading` | Preparing the Reading shortcut section. |
| `home_other` | Residual inside `home_data`, including final template context construction and gaps between subphases. |

**These children are already included in `home_data` and `app`.** Add the
children and `home_other` to compare with `home_data`; add only the original
top-level phases and `other` to compare with `app`. Adding every header value
would count home data twice. Rounding can produce small differences.

The added boundaries do not change queries, calculations, file checks or cache
invalidation. They measure elapsed time, including waits, rather than per-query
SQL execution or CPU. No extra option is needed: keep
`APP_DETAILED_SERVER_TIMING=true`, recreate the updated container and capture
five comparable normal home reloads with their statuses and complete headers.
Compare medians across requests before selecting an optimization; disable the
option and recreate the container after diagnosis.

### Inside series reading indicators

The same opt-in home diagnostic further divides `home_series` into:

| Metric | Interval |
| --- | --- |
| `series_query` | Querying and fetching unfinished items and their reading progress. Includes driver/row conversion and waiting; not SQL execution time alone. |
| `series_prepare` | `prepare_items`: preparing cached item estimates, including its queries, replacement rules, file checks and snapshot lookups. This is distinct from preparing the shared pace estimator (`home_estimator`). |
| `series_aggregate` | Grouping items by series and constructing unread counts, remaining time, missing-estimate counts and estimate states. |
| `series_other` | Residual of `home_series` after these three parts, including gaps between their boundaries. |

These parts are already included in `home_series`, `home_data` and `app`.
Compare their sum with `home_series`; keep using only direct children when
checking the other two levels. None of the three measures CPU alone. They do
not change which queries run, how estimates are calculated or when caches are
invalidated, and do not introduce background work.

Keep `APP_DETAILED_SERVER_TIMING=true` when recreating the updated application
container. Capture the complete header and status of five comparable normal
home reloads. If `series_prepare` dominates, its internal queries, file checks
and snapshot lookups are candidates for a subsequent diagnostic; this header
does not yet separate them. Disable the option and recreate the container when
finished.

### Inside EPUB estimate preparation

The same opt-in diagnostic also divides `series_prepare` into exclusive item
phases, accumulated across all items and batches in that series pass:

| Metric | Interval |
| --- | --- |
| `items_query` | Fetching the requested EPUB rows and saved progress in the existing batches, including row materialization. |
| `items_positions` | Selecting unprepared EPUBs and determining their pace, state and saved text anchors in memory. |
| `items_rules` | Preparing replacement-rule scopes, fetching rules in batches and looking up any remaining per-series rules. |
| `items_stat` | Awaiting the existing batches of file checks in a worker thread, including scheduling and filesystem waits. |
| `items_index` | The initial snapshot-index read for this estimator, including the worker wait. Zero when no index read is needed in this scope. |
| `items_snapshots` | File-stat error handling, fingerprint construction, snapshot validation/decoding, application of the current pace and assignment of estimates/states. Excludes `items_index`. |
| `items_other` | Residual of `series_prepare`, including loop and instrumentation boundaries outside the timed blocks. |

The index is loaded inside a snapshot lookup. Its nested elapsed interval is
subtracted from the snapshot interval so these item phases do not overlap.
Their sum, including `items_other`, can be compared with `series_prepare`;
all are already included in `home_series`, `home_data` and `app`.

Only the series preparation pass in the home diagnostic contributes to these
item metrics. Later novel/Reading preparation and the estimate-refresh endpoint
do not add to them. In the home pass, cached-only preparation still does not
parse cold EPUB inventories or save new snapshots. Queries, cache invalidation,
file checks and their ordering are unchanged.

These finer probes add measurement overhead, especially per item. Treat them as
diagnostic elapsed times, not CPU measurements or an exact uninstrumented
baseline. Recreate the updated container with
`APP_DETAILED_SERVER_TIMING=true`, collect complete headers and HTTP statuses
from five comparable normal home reloads, then disable the option and recreate
the container again. No SQL text, file names or account identifiers are emitted.

### Cost when item diagnostics are inactive

When the detailed item scope is inactive, its per-item timing blocks use a
lightweight no-op context rather than constructing a generator context manager.
They still perform a small scope check, but do not read the diagnostic clock or
create timing spans. Active detailed diagnostics keep the same exclusive phases,
request isolation and exception cleanup. This does not add a reading-estimate
cache or change calculations, invalidation or file checks.


## Optional reading-estimates API phases

After upgrading, keep `APP_DETAILED_SERVER_TIMING=true` and recreate the container.
POST `/library/reading-estimates` then includes `Server-Timing: app;dur=...` and
API phases, including on handled 400/401 responses. With the flag off, its JSON
responses retain their previous headers. Other JSON endpoints and SSE remain
outside this diagnostic. Existing HTML timing and cache validators are unchanged.

The total ends before the response headers are sent. It includes the successful
POST's existing wait for pending eager estimate maintenance; it excludes network,
proxy, browser work and body transfer. The diagnostic does not change whether the
request waits, how estimates are calculated, or which cache/connections it uses.

| Metric | Interval |
| --- | --- |
| `api_auth` | Session validation, including borrowing and returning its authentication read connection. This is broader than the home's query-only `auth` phase. |
| `api_slot` | Waiting to enter the existing single estimate-connection slot. |
| `api_db` | Opening the dedicated estimate connection and configuring `query_only`. Not all SQL execution. |
| `api_input` | Reading/parsing JSON and validating targets. |
| `api_estimator` | Constructing the request's reading estimator. |
| `api_cache` | Attempting the prepared-cache response, including validation and target resolution. |
| `api_fallback` | Native estimate work when the prepared response is unavailable. Zero on a complete prepared-cache hit. |
| `api_encode` | JSONResponse's JSON encoding. FastAPI's preceding response conversion remains in `api_other`. Error handlers using ordinary JSONResponse also remain in that residual. |
| `api_flush` | Existing wait for pending eager maintenance before successful POST headers, outside the route itself. |
| `api_other` | Nonnegative residual of `app` after the top-level API phases: framework work, conversion and unmeasured gaps. |

These phases plus `api_other` account for `app` within rounding precision. Child
phases are already included in their parents:

| Parent | Children and residual |
| --- | --- |
| `api_cache` | `api_validate` (validation/rebuild), `api_targets` (resolving the requested scopes), `api_cache_other` (eligibility and remaining cache handling). |
| `api_validate` | `api_files` (prepared-state file checks), `api_index` (snapshot-index/version operations), `api_revisions` (database revision reads), `api_validate_other` (remaining validation, copies or reconstruction). |

Do not add child durations to their parent again. File-check time includes worker
scheduling and metadata calls, not pure disk time. Native fallback preparation is
inside `api_fallback`, rather than being relabeled as prepared-state validation.
Phases that were not reached are zero; early validation errors do not perform
estimator/cache work. Background refresh internals do not inherit route phases;
only the request's actual wait for them contributes to `api_flush`.

For successful responses the yield dependency closes the estimate connection
AFTER sending the response, so that future time cannot be added to the already
sent header. Error unwinding can close it before producing error headers; that
elapsed cleanup is included in `api_other`. Do not interpret the absence of a
connection-close phase as zero cleanup work.

In browser DevTools, select the actual POST to `/library/reading-estimates`, not
the home document. Record the HTTP status, the complete Server-Timing header and
Waiting time from five warm successful requests. Distinguish home versus series
views and different batches; their target counts and kinds need not be identical.
No payload, cookies, SQL, names, identifiers or reading history are added to the
metrics or logs. Do not send cookies or authorization headers with the timings.
Disable the option and recreate the container when finished.

Backend tests inject elapsed time at the actual authentication, semaphore,
connection, parsing, estimator, file/index/revision, target, encoding and pending
flush boundaries. They verify nested accounting, default-off behavior, request
isolation, error cleanup and exclusion of background refresh work. Browser audit
checks expose API headers and retain equivalent estimate JSON in Chrome and
Firefox at desktop and simulated 320px widths. Mutation controls disable the
instrumentation to verify that these assertions can fail.
