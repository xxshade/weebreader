# Account page preload measurements

Statistics and Settings retain press-only HTML anticipation. Each destination
improved in every paired comparison with added latency in both engines, with
cold and warm destination caches. With no added latency the gains were small
or absent in some conditions; this does not promise universally faster pages.

## Method

Baseline application: `0312f85` (main after PR #220). Candidate: this change.
Chrome 156.0.8078.47 and Firefox 158.0.1, fixed for the experiment. Widths were
1280px and measured 320px, with actual mouse and touch press/release actions
in desktop browsers. The narrow viewports simulate mobile; these are not
native Android results.

Four alternating before/after pairs per destination, engine, viewport, cache
state and delay: 384 activations and exactly 384 destination HTML requests.
The requested hold was 60ms. A wrapper delays only the destination HTML by
0, 120 or 400ms, modeling response latency rather than constrained bandwidth
or the deployed proxy. No other browser suite ran during these measurements.

Before each sample the HTTP cache is cleared (Chrome DevTools
`Network.clearBrowserCache`; Firefox `Services.cache2.clear()` through an
explicitly permitted GeckoDriver system context). The home then loads common
assets. Cold means the destination has not been visited after clearing; warm
means it was visited before returning home. Cookies/session identity remain
present. HTTP caching stays enabled during navigation. The privileged option
is confined to this optional measurement tool, not normal browser validation.

Timing starts at click/release and ends at an animation frame containing the
Statistics `.viz` root or Settings `#profile`. It measures destination HTML
appearance, not completion of every script, image or interaction. Anticipation
does not parse destination HTML or fetch its images before navigation.

## Median wait after release (ms)

| Destination | Engine / width | Cache | No added delay | +120ms | +400ms |
| --- | --- | --- | ---: | ---: | ---: |
| Statistics | Chrome / 1280 | cold | 38.2 → 33.2 | 140.6 → 77.6 | 422.2 → 361.2 |
| Statistics | Chrome / 1280 | warm | 22.7 → 18.3 | 139.6 → 75.1 | 420.3 → 355.9 |
| Statistics | Chrome / 320 | cold | 31.9 → 32.1 | 139.6 → 71.6 | 418.4 → 350.4 |
| Statistics | Chrome / 320 | warm | 31.9 → 30.9 | 137.3 → 69.1 | 416.1 → 350.6 |
| Statistics | Firefox / 1280 | cold | 23.5 → 15.0 | 146.5 → 81.0 | 423.0 → 360.5 |
| Statistics | Firefox / 1280 | warm | 23.0 → 15.5 | 142.5 → 78.5 | 423.0 → 356.0 |
| Statistics | Firefox / 320 | cold | 21.5 → 17.5 | 147.5 → 82.5 | 424.0 → 360.0 |
| Statistics | Firefox / 320 | warm | 22.5 → 16.5 | 148.0 → 82.0 | 423.0 → 363.5 |
| Settings | Chrome / 1280 | cold | 28.8 → 21.9 | 145.7 → 82.1 | 426.3 → 362.0 |
| Settings | Chrome / 1280 | warm | 25.4 → 20.4 | 143.8 → 81.9 | 421.7 → 360.1 |
| Settings | Chrome / 320 | cold | 32.0 → 31.8 | 140.4 → 77.2 | 423.4 → 358.7 |
| Settings | Chrome / 320 | warm | 33.5 → 32.4 | 142.6 → 73.0 | 423.1 → 356.0 |
| Settings | Firefox / 1280 | cold | 32.5 → 24.0 | 152.5 → 96.5 | 431.5 → 370.0 |
| Settings | Firefox / 1280 | warm | 34.5 → 23.5 | 148.5 → 86.0 | 437.5 → 365.5 |
| Settings | Firefox / 320 | cold | 29.0 → 24.0 | 152.0 → 82.5 | 429.0 → 360.0 |
| Settings | Firefox / 320 | warm | 23.5 → 26.5 | 146.5 → 82.5 | 428.0 → 365.0 |

All 128 paired comparisons with added latency improved. Each normal activation
issued one destination request, including clicks made while its preload was
pending. Canceled presses and retries after failures or invalidation are not
normal activations; the functional suite covers them separately and they can
use additional requests.

## Home loading

`DOMContentLoaded` medians below pool both destination experiments (24 pairs
per engine/width/cache). The interval is a seeded 4,000-resample percentile
bootstrap of the paired median difference, candidate minus baseline, in ms.

| Engine / width | Cache | Median before → after (ms) | 95% interval for paired difference |
| --- | --- | ---: | ---: |
| Chrome / 1280 | cold | 108.7 → 98.1 | -28.8 to 17.0 |
| Chrome / 1280 | warm | 35.1 → 35.3 | -4.0 to 4.2 |
| Chrome / 320 | cold | 82.0 → 80.8 | -9.2 to 3.0 |
| Chrome / 320 | warm | 33.1 → 33.1 | -6.7 to 2.0 |
| Firefox / 1280 | cold | 90.5 → 86.0 | -22.0 to 12.0 |
| Firefox / 1280 | warm | 56.0 → 54.5 | -7.0 to 5.0 |
| Firefox / 320 | cold | 69.0 → 69.0 | -5.0 to 7.0 |
| Firefox / 320 | warm | 51.0 → 52.5 | -1.0 to 6.0 |

These home measurements include ordinary run-to-run variation and do not
establish zero overhead. They should not be extrapolated to every deployment.

## Reproduce

Use isolated checkouts and current stable binaries selected once for the run.
The measurement tool defaults to both engines; both are mandatory for a complete
comparison. Do not overlap it with mutation or other browser suites.

```sh
git worktree add --detach ../account-preload-baseline 0312f85
python scripts/measure_home_preload.py --destination stats \
  --cache-states cold warm --rounds 4 \
  --baseline-root ../account-preload-baseline --output /tmp/stats-preload.json \
  --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver \
  --firefox-binary /path/to/firefox --geckodriver /path/to/geckodriver
# Repeat with --destination settings and a separate output file.
```

The JSON contains individual navigation and home timings, request counts,
actual hold intervals, selected engines and baseline commit. `--browser` allows
an isolated engine investigation but does not substitute for executing both.
