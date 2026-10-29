# Desktop browser validation

Required coverage is Chrome and Firefox, including the mobile viewport scenarios
and deliberate-defect controls in each engine. Native Android remains deferred.

The reusable Checks workflow runs every desktop suite on standard
`ubuntu-latest` runners: one isolated job per browser and suite, with matrix
fail-fast disabled. Versions are selected once per workflow and retained for
all jobs; missing, skipped or failed coverage blocks the aggregate validation
check. `scripts/ci_validation.py` lists the suites and preserves each runner's
exit status. Native Android and standalone comparison tools remain optional. Warm reload
performance checks are required as part of the text ETA suite.

PRs build and scan the runtime image before starting backend and browser checks, without registry publication. A failed or unavailable scan prevents these checks from starting. Release
runs on pushes to `main` and manual dispatches validate the exact planned source
commit before image publication or recovery. There is no daily schedule.
Summaries record the commit and outcomes; diagnostic artifacts are uploaded only
on failure, retained for seven days. Browser profiles are not uploaded. Browser
caches use version-specific keys and the repository's default storage cap.

Release preparation also scans the exact archive or immutable recovery image before validation. Publication verifies and transfers the scanned archive without rebuilding; reconciliation rechecks the scanned digest before changing aliases. This ordering saves test execution when scanning fails and increases elapsed time when every check passes.

The same scripts run locally for development and diagnosis.

`test_statistics_layout_browser.py` verifies the three Reading indicators at
desktop and 320px widths, bounded help bubbles, capped window totals, custom
dates and accessible chart tabs retained across date swaps. Controls restore
the two-column layout, incorrect totals, simultaneous charts, lost selection
and an out-of-bounds help bubble; both engines must reject them.

`test_text_eta_browser.py` exercises the real HTTP/SSE path from insufficient
evidence to a measured prediction, exact saved positions, distinct volume and
whole-novel scopes, and updates that retain the document, cards and covers.
It also holds the real refresh request across a reload to check the first
render and every observed update, exercises partial series totals and retries
repaired unread content by keyboard, and retains a known value on HTTP failure.
Controls disable snapshot reuse, hide partial identification and erase a known
result on failure; both engines must reject each defect.
The suite also runs `test_reload_performance_browser.py` against a real HTTP
server started with 151 distinct 100,000-word EPUBs, ten saved reading anchors
and novels with 1,817 and 176 distinct chapter bodies. It warms all scopes,
then repeats ordinary reload, HTTP-cache bypass, the actual Home link and Home
during a simultaneous estimate request three times at desktop and 320px widths. A preload probe records visible, initialized
page usability separately from automatic ETA completion; persistent SSE is
excluded from completion. Server process CPU and every individual measurement
are printed. Median usability per action has a 500 ms local-runner budget; CPU
is reported separately. Unchanged snapshot rows must remain byte-for-byte stable
including row identity, and backend guards forbid source traversal and rewrites.
A control restores redundant endpoint inventory work and must fail in each
browser. Cold preparation is reported separately. These are desktop viewport
simulations, not native LibreWolf or Vivaldi mobile measurements.
See [the reload investigation](../performance/reading-estimate-reloads.md).

The backend estimator and inventory tests cover source offsets, gradual rate
adaptation, legacy history, content removal, cache invalidation and backfill.

`test_hyphenation_browser.py` exercises native dictionary hyphenation, glyphs at
page boundaries, original text preservation, language preferences, reflow and
resume in both text readers. Narrow desktop viewports are simulations; neither
these checks nor the browser's dictionary availability imply native Android or
LibreWolf-specific coverage.

Select the current stable versions once per local validation run with
`python scripts/browser_versions.py --output browser-versions.json`, then
provision and retain those versions for the complete run.

## Commands

Browser validation scripts run both engines by default. Use `--browser
chrome` or `--browser firefox` to select one target, for example:

```sh
python scripts/test_header_browser.py --browser chrome \
  --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver
python scripts/test_header_browser.py --browser firefox \
  --firefox-binary /path/to/firefox --geckodriver /path/to/geckodriver
```

The selector also applies to the reconnection, audit, pagination, home-filter,
reading-statistics, home-preload, lazy-cover, cover-cache and tracking scripts,
and `capture_readme.py`.

The audit suite forces an EPUB exit save to arrive after bulk completion and
checks that the completed status and cleared resume position survive. It runs
this ordering at 1280px and a measured 320px viewport in each engine; this is a
mobile viewport simulation, not native Android execution.

`test_comic_keyboard_browser.py` checks continuous comic scrolling with real
Up/Down keyboard input in both browsers at desktop and measured 320px widths.
It covers direct and boosted entry, repeated keydowns, choosing Scroll after a
paged mode, the real Fullscreen API, focused controls and the end of the strip.
Keyboard scrolling must move the strip without turning pages or prematurely
completing the comic. Controls disable scrolling, reverse Up, omit focus transfer
and intercept control keys; each browser must detect every defect. These Firefox
checks do not constitute a LibreWolf run.

`provision_webdrivers.py --browser chrome` or `--browser firefox` provisions just
that desktop target. Omitting the option provisions both, preserving the local
setup command. The optional native Android provisioning path remains separate.

A selected browser must execute its entire chosen suite without skipped tests.
Mutation controls must fail with their intended assertion in that browser;
setup errors and missing executions do not satisfy them. A complete validation
requires both target results, even when the commands are invoked separately.

## Parallel execution and measurements

Mutation suites temporarily edit application JavaScript, CSS and templates.
For parallel local execution, use separate checkouts; run scripts sequentially
within each checkout. Keep backend tests separate from mutations in that
checkout too. Read-only version metadata may be shared; application files and
disposable fixture state belong to each run.

Compare elapsed validation time and summed execution time separately when
measuring parallel local runs. Retain browser versions and note infrastructure
failures; one run is not a guarantee of future duration.

## MyAnimeList configuration

`test_tracking_browser.py` checks missing client ID, missing redirect URI and
both missing through the real app in Chrome and Firefox at desktop and measured
320px viewports. It requires a visible explanation of
`APP_MYANIMELIST_CLIENT_ID` and `APP_MYANIMELIST_REDIRECT_URI` and keeps the user
on Settings without creating pending OAuth state. Controls remove each server
guard or configuration instruction; both engines must reject each defect.
Configured redirect values and token exchange are covered by backend tests.

## Listing cover loading

`test_lazy_covers_browser.py` checks home, Reading and series grids at desktop
and mobile viewport widths. It uses large thumbnails to put the end beyond
native prefetch ranges, measures cold-profile cover loading, visits every cover,
checks reserved layout geometry and verifies morph cache reuse. Search,
library tabs and a real SSE card replacement exercise reveal and live updates.
Controls that restore eager loading in each template and the live card builder
must fail in each selected browser. Native lazy loading uses browser-defined
prefetch distances; these checks do not require exact request counts or promise
that only visible images load.

For series grids larger than 32 cards, the suite also checks explicit priority
and early decoding of cold visible covers after a boosted navigation. Saved
search, unread status and reverse order determine the visible cards; navigation
from a scrolled home must use the destination's final viewport. A short viewport
must promote no covers, and grids of 32 cards keep native scheduling. The
instrumented `decode()` still invokes the browser's real decoder. Controls remove
the eager hint, fetch priority, decode call, small-grid fast path and viewport
boundary separately; both engines must reject each intended defect.

The paired [cover-readiness measurements](../performance/series-covers.md) are
separate from the correctness suite and must not overlap mutation controls.

The cache and resync suites visit all covers before asserting zero additional
cover transfers. Waiting for every offscreen lazy image without visiting it would
neither warm the cache nor test image correctness.

## Home detail preloads

`test_home_preload_browser.py` uses real mouse and touch pointer actions at
1280px and 320px in both engines. Only home work-detail links preload, on a
press; hover, focus and reader shortcuts keep their ordinary behavior. A normal
activation issues one HTML request. Expired, failed, non-HTML and invalidated
responses use a fresh navigation request. A held real response and a real SSE
update verify freshness even when a click is already waiting for the preload
and its response body is still streaming. An accepted transfer may finish after
the cache-selection deadline without restarting the download.
The controls disable the trigger, HTTP-error fallback, pending-response
invalidation or full-body validation, and incorrectly expire an already accepted
transfer. Every selected engine must reject each defect.

Performance comparisons use the separate `measure_home_preload.py` tool, which
requires both engines. See [the method and results](../performance/home-preload.md).

## Account page preloads

The home-preload suite also exercises the account menu's Statistics and Settings
links in both engines at 1280px and measured 320px viewports, with real mouse and
touch presses. Hover and focus do not fetch. Successful navigation reuses one
HTML request; failed speculative responses fall back to one fresh request.
A response body is held while an independent authenticated HTTP session commits
reading activity or a settings change. The old body must be discarded and the
new data rendered. Local preference writes, shared-cookie changes and SSE
reconnection also invalidate anticipated pages.

Settings assertions wait for the external appearance script's `load` event,
not just the presence of its HTML: a boosted body swap exposes the theme select
before that script initializes its value from the device cookie. The preference
case deliberately delays script activation; a control removes the readiness
wait and must reproduce the default-theme assertion in both engines.

Controls remove the account-link trigger, committed-change notification,
preference invalidation and cookie comparison. Both selected engines must reject
the intended defects. `tests.test_account_events` additionally checks notifications
after real commits, error/GET exclusions and the successful OAuth GET callback.

The optional measurement command and cold/warm-cache protocol are documented in
[Account page measurements](../performance/account-preload.md). Measurement runs
and mutation suites must not overlap in one checkout.
