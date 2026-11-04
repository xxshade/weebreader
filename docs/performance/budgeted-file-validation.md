# Budgeted prepared-file validation

Prepared estimate validation continues to stat every registered EPUB on every
access, comparing current size, nanosecond modification time or error class.
Paths retain their normalization and symlink behavior. No stat result is cached.
Unprepared and oversized libraries retain the native batches of 32.

Prepared validation now compares results inside the worker and returns dirty
series IDs. One dispatch can process several blocks of 128 while the filesystem
is fast. It checks an approximately 2 ms work budget between blocks and returns
the worker when that budget is exhausted. This is not a deadline: an active
block can take longer. Cancellation signals the worker and prevents the next
block, retaining the existing limit of 128 already-started checks.

The worker reads state inputs and filesystem metadata only. It never uses a
database connection or publishes state. Database/snapshot revision checks and
cache invalidation keep their existing order. `api_files`/`items_stat` now include
fingerprint comparison previously performed after worker resumption; compare
total route time and CPU rather than attributing their phase change only to I/O.

## Local prototype evidence (2026-11-04)

Python 3.14.8, four CPU quota, 2,048 synthetic EPUBs/256 series, warm filesystem.
Sixty alternated calls per route/condition, repeated with reversed initialization.
The study also compared a separate reusable API connection; these columns isolate
adding budgeted validation to that connection, with detailed diagnostics disabled:

| Route | Reused connection only, rounds 1 / 2 | Plus budgeted validation, rounds 1 / 2 |
| --- | ---: | ---: |
| API, 100 series | 14.40 / 16.94 ms | 12.55 / 15.77 ms |
| API, 100 EPUBs | 13.40 / 17.54 ms | 11.58 / 16.68 ms |
| Home | 21.16 / 28.87 ms | 19.62 / 25.73 ms |

A separate saturated-executor diagnostic used two workers, two validations of
2,048 entries and an artificial 0.5 ms delay per stat. A short unrelated job
waited a median 75.0 ms with existing 128-file dispatches, 1,241.1 ms with a full
sweep, and 75.8 ms with budgeted validation. This diagnostic explains the budget;
it is not representative of ordinary N100 traffic or storage.

JSON and normalized HTML remained equivalent. Chrome 157.0.8103.38 and Firefox
159.0.1 exercised desktop/320px layouts and estimate refresh. Browser completion
gains varied; local server savings do not guarantee a fixed visual improvement.
Narrow desktop viewports are simulations, not native mobile tests.

No migration, configuration change or manual upgrade step is required.

## Implemented-code confirmation

The final source was compared directly, without runtime prototype patches, with
the same 60-call paired protocol and reversed initialization. These timings remain
local measurements, not Intel N100 results.

| Route | Main, rounds 1 / 2 | This change | Both independent changes |
| --- | ---: | ---: | ---: |
| API, 100 series | 18.66 / 19.63 ms | 15.63 / 16.55 ms | 14.03 / 15.56 ms |
| API, 100 books | 17.29 / 23.80 ms | 15.05 / 20.44 ms | 13.34 / 17.67 ms |
| Home | 23.09 / 26.66 ms | 20.84 / 23.46 ms | 20.99 / 24.57 ms |

HTML and estimate JSON remained equivalent. Per-request process CPU for the
100-series API fell from 20.33 / 20.67 ms to 17.33 / 18.00 ms for this change.
The combined source was checked separately; each pull request remains based on main.
