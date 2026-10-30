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
calculations, queries or cache decisions. It affects only GET requests to `/`,
including its query-string variants, and is controlled by installation settings,
not a request parameter.

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
