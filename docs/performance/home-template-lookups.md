# Home template lookups

The home grid receives dictionaries. Jinja's attribute lookup tries Python
attributes before dictionary keys; explicit `card['field']` lookups avoid that
failed lookup for each rendered field. The grid also reads badge colors directly
from the shared `_status_badges.html` maps, avoiding two macro calls per card.
Other pages retain those macros, with the same shared fallback colors.

This changes template evaluation only. Card data, HTML, escaping, client search,
filters, sorting, file checks and estimate invalidation retain their behavior.
No new cache or database migration is involved.

## Local comparison (2026-10-31)

The baseline was main at `04fd095`. A disposable fixture contained 2,048 EPUBs
in 256 series, with eight entries in Reading. The real home handler supplied
context to the baseline and modified templates. Sixty paired renders alternated
execution order after compilation. HTML comparisons normalized only the live
SSE cursor and render timestamp. Additional contexts covered 128, 256 and 1,024
cards with series/novels, status combinations, partial estimates, missing covers,
blur, Unicode and HTML-special characters. The normalized HTML matched exactly.

The preceding prototype's full HTTP comparison used sixty conditional requests
per variant, alternating order with detailed timing disabled:

| Variant | Median application time | Process CPU per request |
| --- | ---: | ---: |
| Baseline | 34.65 ms | 35.67 ms |
| Dictionary lookups | 29.02 ms | 30.50 ms |
| Dictionary lookups and shared maps | 28.15 ms | 28.50 ms |

A separate repeat against the implemented templates confirmed the isolated
rendering improvement. These local measurements do not predict total navigation
time or guarantee the same results on an Intel N100. They do not measure a change
to filesystem checks or the estimate API.

Chrome 156.0.8078.80 and Firefox 159.0 exercised sorting, client search,
publication filtering and escaped error-form values at desktop 1280px and
narrow 320px widths. Both implementations retained all 256 cards and had no
observed JavaScript errors. Narrow desktop viewports are simulations, not native
mobile browser measurements. Existing home-unread and text-estimate browser
suites additionally retain their mutation controls; the partial-total control
now targets the dictionary lookup syntax.

The CSS class extractor excludes quoted dictionary subscript keys from class
literals. Its regression test fails when the previous extractor is restored,
while still requiring the actual conditional class literals to be detected.
