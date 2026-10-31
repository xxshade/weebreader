# File checks owned by the prepared estimate state

The prepared estimate cache used to construct a Path and a small request-local
row for each EPUB on every access, dispatching metadata checks in batches of 32.
Each state now owns immutable source records and an immutable index. Its copies
share these records; rebuilds reuse a record only when both item ID and original
path string match. Completed or removed sources disappear with the new state.

New paths are constructed and fully converted to filesystem strings in a worker
after `_build` merges unchanged data. The existing database and snapshot revision
checks still happen after that worker finishes and before publication. A cancelled
preparation can finish its local work but cannot publish a partial state.

Every validation still calls `Path.stat()` for every registered source and checks
the current size, nanosecond modification time or error against the saved basis.
The cache stores lexical paths, not reusable `stat()` results. Paths keep their
existing normalization and symlink behavior.

Only prepared-state validation uses batches of 128. Cancelling a request prevents
dispatch of another batch; the worker already running can finish up to 128 checks.
It holds no database connection or open file across calls. The native estimator
retains batches of 32. Libraries above the existing 4,096-entry cache limit retain
the native path, without a new global path LRU. The existing four-account limit,
background warmup and native fallback while warming remain in place.

## Local investigation (2026-10-31)

The disposable fixture contained 2,048 real synthetic EPUBs in 256 series, with
eight entries in Reading. Two rounds alternated 60 requests per variant and route,
inverting startup order in the second round. The prototype reduced warmed home
server time from 27.7–29.5 ms to 18.3–19.1 ms. APIs for 100 series and 100 items
also improved. HTML and estimate JSON remained equivalent. These are local Linux
x86_64 measurements with a four-CPU quota and a warm overlay filesystem, not the
user's Intel N100 or cold/network storage.

In 24 real save/home cycles per variant and round, the prototype's progress POST
increased by approximately 0.8–1.8 ms, while the complete save/home cycle improved
by about 10–11%. Rebuilding the path records after progress reused all 2,048 Path
objects. Preparing the reused rows took about 0.27–0.35 ms inside the worker and
0.38–0.47 ms including dispatch/resumption. Full initial path conversion moves
roughly 4–5 ms from the first validation into background preparation; an isolated
validator experiment measured about 6.3 ms to prepare 2,048 complete paths.

A tracemalloc experiment attributed about 1.4 MiB to 2,048 prepared paths and
2.7 MiB to 4,096, including their records/index. These are Python allocations of
the prototype, not application RSS; simultaneous old/new states can temporarily
retain additional data. Stable copies share their immutable records.

A separate real 8,192-EPUB fixture retained equivalent HTML/JSON and used the
native fallback, with zero prepared-path work. The small latency differences in
that fallback run do not establish a benefit: the active code path was unchanged.
This avoids the earlier global-LRU prototype's miss pattern above its capacity.

The final implementation was also compared directly with the preceding version,
without runtime prototype patches. Two rounds of 60 requests per route produced:

| Route / metric | Previous | Implementation |
| --- | ---: | ---: |
| Home | 28.64 / 27.44 ms | 19.31 / 17.81 ms |
| API, 100 series | 30.52 / 28.62 ms | 21.19 / 19.73 ms |
| API, 100 items | 23.19 / 21.32 ms | 13.87 / 12.46 ms |
| CPU per request, three-route mixture | 28.39 / 27.28 ms | 19.67 / 17.39 ms |

Values separated by `/` are the two runs, not confidence intervals. Chrome and
Firefox desktop and measured 320px viewport runs additionally check navigation,
all 256 cards and JavaScript errors. Browser usability medians vary more than
backend timings: improvements are not consistent in every profile/run. Neither
these measurements nor the prototype promise faster navigation on every visit.
Narrow desktop viewports are simulations, not native Android or LibreWolf/Helium
measurements.

## Regression coverage

The ready-cache tests exercise shared immutable records, eager conversion outside
the event loop, changed origins/renames, completed/deleted sources, fresh metadata
on every access, cancellation during preparation and validation, and revisions
committed while preparation is running. They retain existing coverage for mixed
formats, pace changes, snapshots, account namespaces, eviction and warmup.

Mutation controls break copying, pruning, origin matching, eager conversion,
metadata checks, revision retries, the active-batch bound, entry limits,
publication ordering and worker dispatch; each must trigger the intended test.
The native file-stat tests retain their original 32-file cancellation bound.

No database migration, environment variable or manual upgrade step is required.
