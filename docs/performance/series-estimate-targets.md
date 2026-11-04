# Prepared series estimate targets

`ReadyEstimates.api()` previously searched all prepared items for each requested
series to detect an EPUB without a word-count basis. The state now collects an
immutable set of those series during its existing item-grouping pass. API target
resolution uses membership instead of repeating that search.

The index belongs to the bounded account state (at most 4,096 entries, four
accounts). It is rebuilt with the merged items and word counts, preserved by
state copies, and discarded with the state. It does not cache filesystem metadata,
skip revision checks, add a background task or change the dedicated connection.
An explicitly unknown index retains the original scan. Oversized libraries keep
the native estimator path.

A legacy duration snapshot is not a word-count basis, even if its estimate looks
available. Series with an absent estimate or missing estimates still fall back;
comics require no word basis. Mixed series retain both checks. The dictionary's
item identity supplies the word-count key.

## Local prototype comparison (2026-11-04)

Baseline: main at `8e42136` (application source from PR #31). Python 3.14.8,
four-CPU quota, warm synthetic fixture with 2,048 EPUBs in 256 series and eight
Reading cards. Two rounds reversed server initialization order; 60 alternating
requests per route/condition. Times are medians, with detailed timing disabled
unless specified. These are local-runner results, not Intel N100 measurements.

| Request | Baseline, rounds 1 / 2 | Indexed grouping, rounds 1 / 2 |
| --- | ---: | ---: |
| 100 series | 20.63 / 19.92 ms | 13.23 / 13.04 ms |
| 10 series | 13.05 / 13.44 ms | 12.35 / 12.70 ms |
| One series | 12.75 / 11.75 ms | 12.40 / 12.34 ms |
| 100 individual EPUBs | 12.91 / 13.30 ms | 12.90 / 13.43 ms |
| Home GET | 18.02 / 18.52 ms | 17.77 / 18.00 ms |

Only requests with many series showed a substantial repeatable gain. With
detailed timing enabled, `api_targets` for 100 series fell from 7.08–7.21 ms to
about 0.07 ms. Process CPU per such request fell from 20.7–21.5 ms to 13.8–14.2 ms
with detailed timing disabled. JSON responses were byte-identical.

A second comparison made 36 real progress saves per condition/round, checked the
stored anchor, then requested 100 series. State construction increased by
0.3–0.5 ms; the complete save/query cycle fell from 74.5–75.2 ms to 62.6–64.2 ms.
The isolated save timing varied and does not demonstrate a faster save. Building
the index in a separate pass cost 0.6–0.8 ms with no corresponding API advantage.

Incremental retained index memory was 288 bytes with every basis available,
8,408 bytes for 256 incomplete series, and 131,288 bytes for the worst-case
4,096 incomplete series. Construction also uses a temporary mutable set; these
are retained sizes, not peak allocations. Existing state limits bound retention.

Chrome 157.0.8103.38 and Firefox 159.0.1 completed eight alternating pairs at
1280px and 320px. All 256 cards remained, without observed JavaScript errors or
width overflow. The three estimate requests consumed 25–33 ms less active fetch
time per navigation. Completion includes the client's programmed delay; initial
home usability differences are not evidence for this API-only optimization.
Narrow desktop viewports are simulations, not native Android checks.

An 8,192-EPUB fixture produced identical API responses with no state/index
construction. It showed no gain (45.99 ms baseline versus 47.36 ms prototype in
20 alternating calls), as expected for the unchanged native path.

Prototype validation compared 1,200 generated states, rejected a deliberately
incorrect index, and exercised the existing state lifecycle suite for both
construction variants. Production regressions additionally enforce no repeated
item scan, legacy/unknown-state fallback, missing-estimate guards, mixed formats,
immutable copies and index rebuilding after changes. Existing cache lifecycle
tests compare the index against the original predicate after state transitions.
