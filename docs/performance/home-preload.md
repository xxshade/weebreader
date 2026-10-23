# Home detail preload measurements

The press-only preload is retained because it consistently reduced the wait
after release under additional response latency in both required engines. With
no added delay, gains were small and inconsistent; this is not a general
page-speed improvement.

## Method

Baseline: `8b9e787` (before home detail preloading). Browsers: Chrome
156.0.8078.47 and Firefox 158.0.1. Widths: 1280px and 320px, using desktop
browsers with a narrow viewport and real WebDriver touch pointer sequences
for the latter. These are mobile simulations, not Android results.

Eight alternating before/after pairs per condition; 192 normal activations
total. Both versions use disposable, identical 40-series libraries and enabled
HTTP caching. A 60ms pointer hold was requested (approximately 62–72ms observed).
A wrapper around the real application adds 0, 120 or 400ms before serving the
series detail request. This models response latency, not bandwidth limits or
a full reverse-proxy/mobile network. No other browser suites ran concurrently.

The navigation measurement starts at click/release and ends at the animation
frame where the target item grid is present. It measures destination appearance,
not complete reader initialization, all image downloads or reading activity.
The candidate validates the entire response body before reuse, including changes
received during its transfer.

## Median wait after release (milliseconds)

| Browser / viewport | No delay: before → after | +120ms: before → after | +400ms: before → after |
| --- | ---: | ---: | ---: |
| Chrome / 1280px | 27.7 → 23.2 | 145.9 → 80.6 | 425.9 → 363.1 |
| Chrome / 320px | 33.4 → 32.7 | 145.4 → 77.8 | 425.2 → 352.8 |
| Firefox / 1280px | 33.0 → 23.5 | 153.5 → 84.0 | 434.0 → 362.0 |
| Firefox / 320px | 35.5 → 23.5 | 151.5 → 83.5 | 431.0 → 366.0 |

All 64 paired comparisons with added latency favored preloading. Every normal
activation issued exactly one detail HTML request, including when the response
was still pending at release.

## Home loading

Median `DOMContentLoaded` times across 24 visits per variant and viewport:

| Browser / viewport | Before | After |
| --- | ---: | ---: |
| Chrome / 1280px | 44.0ms | 42.3ms |
| Chrome / 320px | 43.8ms | 45.2ms |
| Firefox / 1280px | 70.0ms | 75.0ms |
| Firefox / 320px | 66.0ms | 68.0ms |

The home differences were within run-to-run variation; paired bootstrap
intervals included zero in every target. This sample does not establish zero
overhead or predict a particular deployed server's performance.

Canceled press attempts are excluded from the 192 normal activations. The
functional suite separately verifies an uncompleted press (one speculative
HTML request, no navigation), expiry followed by a new press, discarded stale
responses and failed-preload retries. Such attempts can use extra requests.
Preloading does not parse the HTML or request its images before navigation.

## Reproduce

Create an unchanged checkout and run from the candidate repository with the
same selected stable browser/driver binaries for the entire comparison:

```sh
git worktree add --detach ../ebookreader-preload-baseline 8b9e787
python scripts/measure_home_preload.py \
  --baseline-root ../ebookreader-preload-baseline --rounds 8 \
  --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver \
  --firefox-binary /path/to/firefox --geckodriver /path/to/geckodriver \
  --output /tmp/home-preload-measure.json
```

The JSON contains individual timings, observed press durations and request
counts. The benchmark requires both browsers and fails on duplicate normal
detail requests. Keep it separate from mutation suites, which edit served files.
