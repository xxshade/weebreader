# Text replacement confirmation and estimate refresh

Adding, editing and deleting an EPUB replacement took 5.56 seconds in the
reported installation, all inside `app` Server-Timing. The rule transaction
already committed before the response, but `ReadyEstimateUpdates` waited for
the dependent ready-estimate cache to finish before sending response headers.
That work includes unopened unfinished books in the affected series.

The response now confirms the committed rule and protected saved passage before
that estimate maintenance finishes. Legacy passage conversion and its validation
remain part of the save; they are not deferred. Estimate maintenance remains eager.

## Display contract

During any estimate update, including progress, pace, content and replacement
changes, the last available numeric value may remain visible. Unread counts,
progress, membership, titles and reader rules still use current data. There is
no previous value for a new account or a deleted/recreated work. Without a known
value the existing `Calculating…` label remains; lack-of-evidence and failure
messages retain their meanings.

Only numeric values are retained, separately from the authoritative ready state.
The display cache is bounded to four accounts and 4,096 values per account;
publication comparison has the same bound. Account and target incarnations and
the database epoch prevent recycled identifiers from borrowing a prediction.
Selected values are captured before asynchronous identity validation so concurrent
publication or eviction cannot interrupt the response.

The worker still checks revisions and source fingerprints before publishing.
Changes discovered by request-time file validation queue the affected series for
eager refresh. A completed validated publication emits `reading_estimate_updated`;
stable hits emit nothing. The existing SSE client fetches the updated numbers
without replacing covers or cards. The client also retains an existing number
if an interim response says `calculating`.

## Local evidence

The authenticated in-process HTTP probe used real application routes, SQLite,
EPUB parsing and ready-cache maintenance, with synthetic warm 100,000-word EPUBs.
Two save samples were taken for each series size. These are local measurements,
not predictions for the reported N100.

| Series size | Previous median response | New response samples |
| --- | ---: | ---: |
| 8 EPUBs | 671 ms | 15.7–20.0 ms |
| 16 EPUBs | 1,262 ms | 13.1–18.8 ms |

The previous pre-header maintenance wait accounted for approximately 657 ms and
1,248 ms respectively. The new responses performed no such wait. The background
work still costs approximately the same amount; this change moves its completion
off the save's acknowledgement path rather than eliminating parsing CPU.

Regression coverage holds the real worker while exercising authenticated add,
edit and delete requests, legacy passage protection, consecutive edits, current
reader rules, numeric fallback and eventual native-equivalent publication.
Additional coverage checks missing initial values, refresh failure, mixed series,
novel counts, identity reuse, bounds and publication during identity lookup.
Each new test has a deliberate-defect control in an isolated checkout.

Detailed save diagnostics are optional under `APP_DETAILED_SERVER_TIMING`:
`db_acquire`, `auth`, `replacement_legacy`, `replacement_writer_wait`,
`replacement_validate`, `replacement_write`, `render` and `replacement_other`.
The clocks are disabled by default. No new configuration is required.
