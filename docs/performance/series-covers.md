# Series cover readiness

Opening a series and displaying its covers are different milestones. The series
HTML can appear while cold covers still have to be fetched and decoded. The
existing home preload prepares HTML only; this change does not speculate on
images before navigation or wait for images before displaying the page.

## Result and behavior

For grids with more than 32 cards, the existing thumbnail script gives cold
covers in the actual viewport high fetch priority, starts them eagerly, and
requests asynchronous decoding immediately. It queries after boosted scrolling
has completed, using the saved filtering and ordering already applied by the
page. Navigation continues immediately. Cached images and distant covers retain
their native path. The application keeps its existing cover URLs and HTTP cache
headers. No extra JavaScript asset is introduced; the shared listener binds once.

The reported-case run used Chrome 156.0.8078.47 and Firefox 158.0.1: eight
alternating pairs per condition, 128 activations total. The following table
shows medians and the median paired difference (candidate minus baseline),
with a seeded 4,000-resample percentile bootstrap interval. Negative differences
mean earlier readiness. These small samples do not establish universal gains.

| Browser / width | Cache | Baseline → candidate (ms) | Paired difference, 95% interval (ms) |
| --- | --- | ---: | ---: |
| Chrome / 1280 | cold | 118.8 → 102.4 | -14.0 [-17.9, -2.1] |
| Chrome / 1280 | warm | 85.9 → 86.6 | +0.1 [-16.9, +2.9] |
| Chrome / 320 | cold | 114.8 → 101.7 | -13.9 [-23.0, -0.3] |
| Chrome / 320 | warm | 97.2 → 97.8 | +0.6 [-8.8, +15.7] |
| Firefox / 1280 | cold | 108.5 → 95.5 | -13.5 [-31.0, +9.0] |
| Firefox / 1280 | warm | 48.5 → 48.5 | -1.0 [-7.0, +7.0] |
| Firefox / 320 | cold | 83.5 → 80.5 | -2.5 [-23.0, +7.0] |
| Firefox / 320 | warm | 54.5 → 50.0 | -5.5 [-12.0, -1.0] |

Both desktop medians improved in the reported configuration. Chrome improved in
7/8 cold pairs at both widths, Firefox in 6/8 desktop and 4/8 narrow pairs. The
Firefox cold intervals include zero; its exact effect remains uncertain. The
benefit is modest and strongest on desktop. Warm samples transferred no visible
cover bytes; there is no consistent measured warm-cache penalty. Home
DOMContentLoaded paired intervals pooled over cache states include zero in all
four browser/width groups, so no consistent home-loading penalty was detected.

The exact 400–500ms LibreWolf field observation was not reproduced here. This
addresses measured scheduling overhead; it does not establish the cause of that
whole delay or promise that cold network images will appear instantly. Geometry
sampling and explicit `decode()` instrumentation can affect scheduling, and
readiness is a proxy rather than captured compositor paint.

## Exploratory comparisons and limits

- Removing lazy loading from visible images alone did not consistently improve
  readiness. The retained approach also starts decoding while the transfer is
  pending, and assigns explicit fetch priority.
- An earlier 160-card / 280px experiment improved cold desktop medians from
  151.9 to 120.3ms in Chrome and 123.5 to 109.5ms in Firefox. That prototype
  registered per-navigation; the final implementation uses the existing shared
  thumbnail module and was remeasured above with 130 cards / 360px.
- An unconditional 12-card prototype did not improve cold desktop medians and
  showed warm overhead (Chrome 71.4 to 85.1ms, Firefox 25.5 to 31.5ms in four
  pairs). A 40-card diagnostic run had mixed results by engine/viewport. The
  32-card fast path is a conservative heuristic to avoid extra scheduling for
  small grids, not a browser limit or an assertion that every larger grid gains.
  The correctness suite exercises the exact 32-card boundary.
- Covers are deterministic high-entropy test images: 163,730 bytes for the first
  cover, intentionally heavier than many real 400×600 WebP covers. The same
  bytes are served to both variants. Different content, CPUs, browser forks,
  thumbnail sizes, caches and proxies can change the balance of transfer and
  decode costs. The automated Firefox result is distinct from a LibreWolf test.

## Method

Compare baseline `22fb4bf` against the candidate using
`scripts/measure_series_covers.py`. Fixtures are isolated application servers
with the same deterministic 400×600 WebP covers and a disposable account. Both
servers use localhost on different ports; the disposable session tokens are
accepted by both databases because cookies are scoped by host, not port.

Run alternating before/after pairs in Chrome and Firefox stable at measured
1280×800 and 320×800 viewports. The final reported-case run uses 130 items
and saved series thumbnails of 360px and 144px, respectively. The small viewport uses a real touch pointer sequence in a desktop
browser; this is mobile simulation, not an Android result. Press/release is
separated by 60ms, preserving the existing press-only HTML preload.

The comparison records click-to-first-grid-frame, resource timing, the first
frame in which all visible images have loaded, completion of `decode()`, and
the following animation frame. This last milestone approximates visible cover
readiness; it is not a measurement of compositor paint or physical display time.
No proxy, artificial latency or bandwidth throttling is involved.

Clear HTTP cache before each sample. Firefox also clears its decoded-image
cache through a privileged measurement-only driver. Each sample uses a fresh
query value on the cover URLs, excluding reuse from earlier samples; the warm-up
visits the series using those exact URLs before returning home and measuring.
The tool checks that visible covers transfer bytes for cold samples, transfer
no bytes for warm samples, have no duplicate resource entries, and that each
activation issues exactly one series HTML request. The HTTP cache remains
active during navigation.

All runs and deliberate-defect controls must be sequential in one checkout.
Do not measure while a mutation test changes the application.

## Reproduce

```sh
git worktree add --detach ../series-covers-baseline 22fb4bf
python scripts/measure_series_covers.py \
  --baseline-root ../series-covers-baseline \
  --items 130 --desktop-thumbnail 360 --rounds 8 --output /tmp/series-covers.json \
  --chrome-binary "$CHROME_BINARY" --chromedriver "$CHROME_DRIVER" \
  --firefox-binary "$FIREFOX_BINARY" --geckodriver "$FIREFOX_DRIVER"
```

Both browser engines are required for a complete comparison. Other fixtures can be selected with `--items` and `--desktop-thumbnail`;
individual-browser runs are diagnostic only.
