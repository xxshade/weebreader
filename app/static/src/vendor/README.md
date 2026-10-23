# Vendored third-party assets

Static, final-form JS files committed directly into the repo instead of
downloaded at build time -- same reasoning as `app/static/src/fonts/`
(already committed .woff2 files): small, immutable once downloaded, and
downloading one at every build only added a way for the build to break
(a wrong/nonexistent version, a renamed asset, a network hiccup) for zero
benefit, since the file itself never changes between builds anyway.

## htmax.min.js

- **Version**: 4.0.0
- **Source**: `dist/htmax.min.js` from the npm tarball
  https://registry.npmjs.org/htmx.org/-/htmx.org-4.0.0.tgz
  (tarball sha1 `72323bd24261364fe3111803c46b80da1fb33445`, which is the
  `dist.shasum` npm publishes for that version -- checked on download)
- **sha256**: `2b90cb1844656b9f025805b479288cea2877c7f91068a845c4106ebf5d87a315`
- **What it is**: htmAX is htmx's own bundle -- the same 4.0.0 core as
  `htmx.min.js` plus eleven extensions in one file. This app enables `sse`,
  `history-cache` and `preload` through the `base.html` extension whitelist.
  `resync` is registered by the application itself.
- **Why the bundle and not `htmx.min.js` + `ext/hx-sse.min.js`**: loading
  them as two files puts the extension's registration in a different
  deferred execution from htmx's, and htmx 4 loaded with `defer` schedules
  its first pass over the document with `setTimeout` rather than waiting
  for DOMContentLoaded -- so a timer can run between the two scripts.
  Measured with the extension's response delayed 350ms on a cold cache:
  **0 of 10 loads** connected the SSE stream. Processing the element again
  from app code fixed that and opened **2 streams** on a warm load, because
  the extension's duplicate guard is only set once the response arrives.
  One file has no such window. It costs 26 KB gzipped against 15.5 KB for
  the pair.
- **Preload internals**: home-preload.js observes `_htmx.preload` (`prefetch`,
  `action`, `expiresAt`) after dispatching the explicit
  `home-card-press` event. The event is emitted only by the loaded guard. The bundled extension
  reuses its response without revalidation, so the app checks freshness and
  retries failed/non-HTML/redirected responses during the real navigation.
  Explicit home work-detail and account-menu Statistics/Settings links opt in.
  Cookie snapshots cover shared display-preference changes; committed reading-time
  and settings mutations emit `account_updated` for other tabs and devices.
  `autoBoost: false` keeps other links out. Re-run `test_home_preload_browser.py`
  in both engines when upgrading this bundle; trigger delay modifiers are not
  implemented by this version's preload extension.
- **Known upstream defect**, worth re-checking on upgrade: the `sse`
  extension's cleanup calls `abortController.abort()` and then
  `reader.cancel()` and discards the promise `cancel()` returns, which
  rejects with `AbortError: BodyStreamBuffer was aborted` because the abort
  has just errored that stream. It surfaces once per swap that removes the
  connected element. This file is byte-pinned to the published artifact, so
  it is not patched here; `#library-events` carries `hx-preserve` instead,
  which keeps the element out of the teardown entirely (see AGENTS.md).
- **Three internals the app reaches into**, all pinned by
  `tests/test_events.py` so that an upgrade fails here rather than in a
  browser: the connection lives at `element._htmx.sse`; its reader loop
  suppresses `htmx:sse:error` when `h.abortController.signal.aborted` or
  the error is named `AbortError`; and htmx `console.error`s any event
  whose detail carries an `error` before dispatching it. Together they are
  why `library-events.js` marks the connection at `beforeunload` with a
  `{signal: {aborted: true}}` sentinel -- Firefox raises a plain
  `TypeError` for a torn-down fetch and matches neither condition, so every
  reload logged one, and no listener can stop a log that happens before the
  event exists. A fourth internal keeps that from muting the stream for
  good: each reconnect assigns its own controller (`h.abortController=c`),
  so the sentinel lasts one break.
- **Note**: 4.x is published to npm under the `next` dist-tag; `latest` is
  still 2.x. `npm view htmx.org version` therefore reports a 2.x release
  and is not the version to take. The GitHub release carries no built
  asset for v4.0.0 either (checked: 404 under both `v4.0.0` and `4.0.0`,
  while the same URL for v2.0.10 answers 200), which is why the source is
  the tarball rather than a release download.

To upgrade: download the tarball for the desired version from
https://registry.npmjs.org/htmx.org, check its sha1 against the
`dist.shasum` the registry publishes for that version, extract
`package/dist/htmax.min.js`, overwrite this file, and update the version
and hashes above. The tarball also ships `dist/scripts/upgrade-check.py`
and `dist/skills/htmx-upgrade-from-htmx2.md`, which are what a major
upgrade should be worked from.

## Sortable.min.js

- **Version**: 1.15.7
- **License**: MIT; the complete upstream copyright and permission notice is
  included in [Sortable.LICENSE.txt](Sortable.LICENSE.txt).
- **Source**: `Sortable.min.js` from the npm tarball
  https://registry.npmjs.org/sortablejs/-/sortablejs-1.15.7.tgz
  (tarball sha1 `83a0bddc472117ee328dea20b2e6f490fed20f86`, which is the
  `dist.shasum` npm publishes for that version -- checked on download)
- **sha256**: `bf4241bc73fef7f11c59a283a69fe8051cdd31c6d8ff5a2b9ba219e7831fcf76`
- **Why**: manual reordering used the native HTML5 drag-and-drop API,
  which is mouse-only -- it does not fire from touch, so reordering did
  not work on a phone at all (confirmed on Vivaldi for Android, and
  measured in Chromium: a mouse drag fires `dragstart`, a synthesized
  touch drag fires nothing). Sortable is pointer/touch based.

To upgrade: download the tarball for the desired version from
https://registry.npmjs.org/sortablejs, check its sha1 against the
`dist.shasum` the registry publishes for that version, extract
`package/Sortable.min.js`, overwrite this file, and update the version and
hashes above.
