# Exclusive estimate API connection

`/library/reading-estimates` reuses one read-only connection behind its existing
single-request slot. It remains separate from the four interactive readers:
native inventories may be slow and must not occupy capacity needed by the home
page or authentication. Warmup and anticipated cache refresh retain their own
connections. No additional API concurrency or periodic task is introduced.

The connection opens on demand with the normal PRAGMAs and `query_only=ON`.
It reopens when the database path changes. Handled HTTP errors preserve a healthy
connection; unexpected failures and cancellation discard it, draining/closing
outstanding SQLite work before another borrower. A connection left in an
explicit transaction is discarded too. `close_pool()` waits for the active
estimate borrower and closes this reader along with the other owned connections
at shutdown.

`api_slot` still measures time waiting for exclusive access. `api_db` measures
acquisition, including setup when a new connection is needed; a warm acquisition
can be near zero. Closing after headers is not part of successful response timing.

## Local evidence (2026-11-04)

Prototype comparisons used Python 3.14.8, four CPU quota, 2,048 synthetic EPUBs
in 256 series, warm filesystem caches, and the series-target index from v8.4.27.
Sixty calls per condition/route alternated execution order; the second round
inverted server initialization. With detailed diagnostics disabled:

| API | Current, rounds 1 / 2 | Reused connection, rounds 1 / 2 |
| --- | ---: | ---: |
| 100 series | 16.42 / 18.73 ms | 14.40 / 16.94 ms |
| 100 individual EPUBs | 15.11 / 20.38 ms | 13.40 / 17.54 ms |

Home GET did not improve consistently. Earlier diagnostic runs measured local
fresh setup at about 1.5–1.8 ms and warm acquisition near 0.002 ms. The first API
still needs a connection. These measurements do not guarantee the same savings
against the approximately 5.9 ms reported for setup on the Intel N100.

A persistent reader added one idle worker, one descriptor and roughly 0.5 MiB of
RSS in this fixture. RSS is allocator/workload dependent, not a memory bound.
Shutdown closed the retained connection. Tests exercise fresh committed data,
WAL checkpointing, read-only enforcement, healthy/refused requests, failures,
cancellation, serialization, pool independence, database changes and cleanup.

No migration, configuration change or manual upgrade step is required.

## Implemented-code confirmation

The final source was compared directly, without runtime prototype patches, with
the same 60-call paired protocol and reversed initialization. These timings remain
local measurements, not Intel N100 results.

| Route | Main, rounds 1 / 2 | This change | Both independent changes |
| --- | ---: | ---: | ---: |
| API, 100 series | 18.66 / 19.63 ms | 16.30 / 17.97 ms | 14.03 / 15.56 ms |
| API, 100 books | 17.29 / 23.80 ms | 15.98 / 23.60 ms | 13.34 / 17.67 ms |
| Home | 23.09 / 26.66 ms | 24.29 / 27.31 ms | 20.99 / 24.57 ms |

HTML and estimate JSON remained equivalent. Per-request process CPU for the
100-series API fell from 20.33 / 20.67 ms to 17.67 / 18.33 ms for this change.
The combined source was checked separately; each pull request remains based on main.
