# Project notes for coding agents

Read this before changing anything. It is the set of invariants, traps and
decisions that are not obvious from the code — the *why*. `README.md` covers
what the app is and how to run it. `CLAUDE.md` holds only what is specific to
the Claude Code sandbox.

**Keep this file short.** The history of every bug lives in `git log`; what
belongs here is what a future change can still break. Write the rule and the
one measurement that proves it, not the story.

## Architecture

FastAPI + Jinja2 + SQLite (aiosqlite) + htmx. No build step: no bundler, no
CSS compiler, no npm. Everything under `app/static/` is committed as served.

```
app/
  main.py            app wiring, lifespan (startup backfills), /covers mount
  database.py        connections, init_db, PRAGMA user_version migrations
  schema.sql         fresh-database schema
  auth/              session, password+TOTP, WebAuthn passkeys, settings
  library/           scanner, watcher, parsers/, search, job_status, schedule
  reader/            comic + text reader routes (manifest, resource, progress)
  novels/            scraped web novels (sync, chapters, reader routes)
  stats/             /stats page, page_reads recording, reading estimates
  tracking/          MangaUpdates + MyAnimeList
  covers/            thumbnail pipeline (WebP, content-addressed names)
  static/js/         plain ES, one file per screen; no modules, no bundler
  static/src/        app.css (frozen), theme.css (hand-written), fonts, vendor
```

Tests: `python3 -m unittest discover -s tests -t .` (stdlib only). Also
`pyflakes app scripts`, `node --check` on each JS file, and
`scripts/test_webauthn_isolated.py`. The reusable Checks workflow runs the
complete validation on PRs and the exact commit selected for publication or
recovery. PRs also build Docker without publishing. Both desktop browsers remain
mandatory; see `docs/testing/browser-checks.md` for suites and mutation isolation.

## Invariants that bite

### The stylesheet is frozen, with no generator

`app/static/src/app.css` began as a snapshot of Tailwind v4's output and is now
plain CSS. **`.leading-none` must stay AFTER the `.text-*` rules** — source
order is what makes `text-xs leading-none` render at line-height 1.

**A class with no rule in that file silently does nothing** (hit five times).
Editing `app.css` is allowed — add the rule beside its neighbours.
`tests/test_css_classes.py` checks every class used in a template or JS has a
rule, unless listed in `HOOK_CLASSES` (JS-only, unstyled on purpose), and fails
when a listed hook is no longer looked up; its extractors are tested too.

- `theme.css` is hand-written and **unlayered** (it beats `app.css`): layout
  things — `--header-h`, sticky rules, `.library-tab`, `.ctl` (the shared
  control height), `.avatar`, `.thumb-grid`, `.viz*`, the AMOLED override,
  `body:has(#reader-root)`.
- Shared components (`.btn`, `.input`, `.cover`, `.card-title`) live in
  `app.css`'s `@layer components`, so utilities still win (`class="btn px-4"`).
- Only `-webkit-line-clamp` cuts by lines; the unprefixed `line-clamp` is parsed
  by no engine and Firefox logs it. `-webkit-text-size-adjust` stays (Chrome uses
  it; Firefox's "declaration dropped" for it is expected).

### htmx is 4.x, and some of its changes are silent

The vendored bundle is **htmx 4.0.0** (`htmax.min.js`, provenance in
`app/static/src/vendor/README.md`). Run the tarball's `upgrade-check.py` first on
any future major. What the checker does not find, each failing without an error:

- **`detail.successful` is gone** — the status is `detail.ctx.response.status`.
- **Every status swaps**, not just 2xx. FastAPI renders refusals as JSON, so
  `base.html` sets `noSwap: [204, 304, "4xx", "5xx"]`. A form whose refusal IS
  markup opts in with `hx-status:422="swap:outerHTML"`. **A bare
  `hx-status:422` does nothing** (a valueless attribute reads as `""`).
- **htmx binds before our deferred scripts**, so a `submit` listener cannot stop
  a request. Keep native validity CURRENT instead (`syncValidity` in
  `searchable-select.js`): htmx checks validity before issuing.
- **`htmx:after:swap` targets the element that TRIGGERED the request.**
  `htmx:after:process` is the one that names the swapped-in element (order:
  after:request, before:swap, after:settle, after:process, after:swap).
- `htmx:load` maps to `htmx:after:process` for "once per processed root";
  `htmx:after:init` fires once per ELEMENT.
- **No attribute is inherited anywhere**, so explicit inheritance is a no-op.
  The 60s `defaultTimeout` is safe (trackers cap at 15s; the SSE extension
  clears it for `text/event-stream`).
- `autofocus` is honoured on EVERY swap — dialogs use `data-dialog-focus`.
- **`defaultSettleDelay` is 0.** With a delay, every incoming element whose id
  is on the outgoing page wears the OLD page's attributes until it ends, and
  the page's scripts run in between: boosting /series/1 → /series/2, series 2
  read series 1's `data-series-id` (its "Unread only", its saved sort) and
  `data-has-manual`. Zero skips that step; `htmx:after:settle` still fires.

**Extensions:** the bundle carries eleven; `base.html` whitelists
`extensions: "sse,history-cache,resync,preload"`. **The separator is a comma** — htmx
splits on `/,\s*/`, so a space makes one name matching nothing and every
extension silently fails (measured: `/library/events` never requested). The rest
are left out for cost (a 40-element swap: 5.7 ms against 6.6 ms), not safety.

Home detail preloads require `autoBoost: false` and explicit press triggers.
When changing navigation, SSE or the htmx bundle, run
`scripts/test_home_preload_browser.py`: the five-second native cache has no
state invalidation or failed-response fallback of its own.

### One Jinja environment

`app/templating.py` builds the only `Jinja2Templates`; every router imports
`templates` from it. Register a global or filter there, never on a router-local
environment (seven copies drifted: a filter on one rendered as an error on the
page that used another).

### Jinja resolves getattr before the mapping

A dict key named `items`, `keys`, `values`, `get`… resolves to the **dict
method**, silently. Use non-colliding names or a namedtuple.

### Database

- **Reads borrow from a pool** (`read_connection()` / the `get_db` dependency,
  4 connections, `PRAGMA query_only`). Opening one per request cost 0.95 ms
  median against 0.124 ms for a query on a live one.
- **A streamed response must not depend on `get_db`.** FastAPI keeps a yield
  dependency open until the response is SENT, and an event stream is sent for as
  long as the tab is open: four open tabs held the whole pool and every other
  request waited forever. `/library/events` authenticates through
  `get_streaming_user`, which borrows a connection for the check alone.
  `tests/test_connections.py` walks the route's dependencies.
- An `HTTPException` hands its pooled connection back; any other exception
  closes it. The pool and the writer both re-open when `settings.db_path`
  changes (the tests move it per test).
- **Writes share one connection behind the global write lock**
  (`write_transaction()`). It **does not commit** — the caller does; a block that
  returns without committing is rolled back so the next writer cannot commit it,
  and a block that raises drops the connection. Don't touch the write connection
  after its block ends: it belongs to the next writer.
- Every connection's aiosqlite worker thread is made a **daemon**
  (`get_connection`): one left open at exit otherwise held the process in
  `threading._shutdown` forever. `tracking.service.drain()` still awaits pushes
  so they finish.
- **Schema changes go in `schema.sql` AND `ADDED_COLUMNS`** in `database.py`
  (added when `PRAGMA table_info` lacks them). A test checks every listed column
  is in `schema.sql`.
- Anything that **deletes or rewrites** data is a one-time migration gated on
  `PRAGMA user_version` (1–8). `init_db` runs on a bare connection (foreign keys
  off), and **re-runs `schema.sql` after the migrations**: a table rebuilt with
  `DROP TABLE` loses its triggers (migration 5 does exactly that). SQLite cannot
  change a foreign key's action or a `CHECK` by `ALTER TABLE`; both need a
  rebuild — so a `CHECK` exists only where `schema.sql` created the table, and
  the endpoint's own validation is what guards an upgraded database.
- `sort_title` / `sort_name` hold `natural_sort_key()` output (zero-padded digit
  runs). **Never search or match against them.** Escape `%`/`_` in LIKE.
- Name the columns a hot query needs. `NOVEL_COLUMNS` omits the cover BLOB;
  `SERIES_CARD_COLUMNS` / `NOVEL_CARD_COLUMNS` are what a card renders (`SELECT
  *` over 400 series with summaries: 5.7 ms / 1.1 MB against 3.5 ms).
- **`LENGTH()` on TEXT reads the whole value.** `chapters.content_size` is kept
  beside `content_html`, and every write of one writes the other.
- **`item_read_totals` / `chapter_read_totals` are a trigger-maintained cache of
  `page_reads`** (estimator build: 224 ms → 2 ms on 200,000 spans). The
  estimator reads only the totals and `/stats` only the raw spans; `page_reads`
  is **append-only** (an `AFTER INSERT` trigger would miss a DELETE/UPDATE —
  pinned); the totals CASCADE where `page_reads` uses SET NULL (a reused rowid
  would inherit a head start); `rebuild_read_totals()` defines correct.
- **`/stats` reads the whole history from covering indexes**
  (`idx_page_reads_user_reads`, `idx_page_reads_user_chapter_reads`): with one
  user `user_id` filters nothing, so a column missing from the index is a seek
  per row -- 1493 ms for /stats on 200,000 spans, 473 ms covered.
  `tests/test_stats_index.py` explains the real queries' plans. **An index on
  a column a migration adds goes in `database.AFTER_MIGRATIONS`, not
  `schema.sql`**: its first pass runs before the migrations and stops at the
  first statement an older table cannot take (`no such column: kind`).
- **A status is stored as a code, never its label, and there is no lookup
  table.** The same values are written out in `status_labels.py`, two `CHECK`s,
  `STATUS_VIZ_SLOT` and the badge maps (Jinja and JS);
  `tests/test_status_vocabulary.py` keeps them agreeing.

### Nothing blocking runs on the event loop

An `async def` handler runs ON the loop. Zip reads, Pillow, argon2, hashing,
directory walks and stats go through `asyncio.to_thread` (four comic page loads
in flight took an unrelated endpoint from 3.9 ms to 37.9 ms median).

### Page scripts bind global listeners once per document

A boosted visit swaps `<body>` and **re-runs every script in it in the same
document**. A script in the body that adds a listener to `document`, `window`
or `<body>` must check a `window.<name>` flag first, and must look its elements
up when the event arrives instead of holding them. Without that, two copies of
a delegated toggle undid each other (the settings "?" bubbles, the stats Custom
range and "Show more" worked on every other visit, both browsers) and the
popovers' copies kept every previous page alive (17 user-menu listeners after
four laps). `tests/test_listeners_bound_once.py` fails any body script that
binds globally without a flag. Scripts in `<head>` run once per document and
need none — which is why shared things (`Sortable.min.js`, `reorder.js`,
`reader-resume.js`, `reading-time.js`, `dialogs.js`, `cover-preview.js`) live
there: a swap re-inserts scripts force-async, so ordering between body scripts
is not guaranteed either (`createReadingTimeTracker is not defined` in 3 of 5
Firefox runs when they were in the body).

A MutationObserver is the same: one per document, reading the current element
when it fires (`novel-chapters.js`, `reorder.js` once per container selector).

### Images and caching

Thumbnails are `{item_id}-{hash12}.webp` — different content, different URL —
which is the only reason `/covers` can answer `immutable`. Prefixes are
load-bearing: an item thumbnail starts with a digit, so `series-…` and
`avatar-…` cannot collide and say a file may be deleted when replaced. Novel
covers live in the BLOB with `cover_image_hash` rendered as `?v=`.

The reader's routes are NOT content-addressed (`/reader/7/page/3` keeps naming
page 3 after the file is replaced), so they answer `immutable` only when `?v=`
matches the current file hash (`_content_cache_headers`). `/static` does the
same through `asset_url()` + `VersionedStaticFiles`; a test checks every asset
URL earns the long header, since the two computing `v` differently would fail
silently.

**Every HTML response is `private, no-cache` with an ETag of the body**
(`revalidate_pages`). `no-cache`, not `no-store`: the back/forward cache stays
usable and an unchanged page revalidates as a bodiless 304. Renders must be
byte-identical for that to hit (no nonce or timestamp in the markup). A reverse
proxy must not add `public` — `/` is per-user. The content-type guard comes
first and leaves the event stream untouched (hashing it would hang).

`page_dimensions` is a cache keyed by `(item_id, page_index)` and trusted when
the row count matches, so `process_file` deletes it when a file's hash changes.

Covers are 400x600 WebP q80, avatars 128x128 (WebP −33..−42% against JPEG;
AVIF cost 2x decode on a phone).

### The scanner

- **An unchanged file is not read.** `process_file` compares size and
  `st_mtime_ns` against `items.file_size`/`file_mtime_ns` first; only a file
  whose size or time moved is hashed, and the time is recorded from the stat
  taken BEFORE hashing (a touched file is hashed once, then skipped again). The
  daily scan used to hash the whole library (20 files × 10 MB: rescan 28 ms
  against 588 ms of hashing on a warm cache). Anything that must reach
  already-imported rows is a startup backfill in `main.py`'s lifespan — cheap in
  the steady state, a no-op the second time, and `report_progress=False`
  (`job_status` keeps finished jobs, which would put a bar on every boot).
- **Changing what a parser puts in `items.metadata` means bumping
  `epub.METADATA_VERSION`** (shared by both parsers): `backfill_metadata`
  re-reads only rows below it, so without the bump the library keeps the old
  reading. Descriptions are stored as plain text (`plain_text.py`) and the card
  renders them escaped — never as markup from a file.
- **A renamed or moved file keeps its item** (`_moved_item`): a file whose hash
  matches an item whose path no longer exists moves that row, id and reading
  state included; a renamed folder moves its series (`adopt_series_id`), whose
  name follows only while it was the folder's own (`automatic_series_name`).
  The watcher imports a move's new path BEFORE forgetting the old, or the old
  row is gone when the hash is looked up. A copy is a new item.
- **No two libraries read the same files**: a root is stored resolved and
  refused when it equals, contains or sits inside another (`_overlap`);
  nested roots gave a file to whichever library saw it first. Existing
  overlapping libraries are left alone (there is no way to delete one).
- **A library that looks empty is not emptied.** A root that is missing or
  unlistable, or a walk that found no file while the library has items, skips
  the reconciliation and records an error on the root (a NAS mount dropping
  deleted the whole library and its reading state).
- The walk skips dot-folders and `IGNORED_FOLDERS` (`#recycle`, `@eaDir`, ...)
  and each directory it has already visited by `(st_dev, st_ino)` — a symlink
  loop walked forever.
- **Unchanged files are decided in bulk** (`_plainly_unchanged`, one SELECT of
  path/size/mtime per library): 8,500 files rescanned in 0.98 s against 7.9 s.
  The reconciliation deletes with `_delete_item(recompute=False)` and settles
  each series once (emptying a 500-item folder held the write lock 4.33 s,
  now 0.38 s). Covers are encoded BEFORE the write lock is taken.
- **A scan settles only the series it changed, once each**: imports defer
  their renumbering (`process_file(renumber_later=)`, settled by
  `_settle_series`) and the reconciliation redoes only series an item left.
  Renumbering every series on every scan held the lock 651 ms for 400
  unchanged series (now 1 ms); per-file renumbering made a 1000-file import
  25.9 s (now 6.4 s). Numbers BEFORE the cover: the cover is the first item
  in order. A renumber that moved anything publishes `item_updated`.
- The watcher imports at most `IMPORTS_AT_ONCE` (3) files concurrently; a
  100-file drop used to keep a progress save waiting 7.6 s.
- `scan_events` holds errors only; one row per path (`_replace_error`), cleared
  when the path reads again.
- **One scan per library at a time.** Asked again meanwhile, it runs once more
  afterwards rather than beside it.
- The walk (`os.walk(onerror=)`, `followlinks=True`, since `rglob` silently
  skips a folder it may not enter) and the reconciliation's stats and listings
  run in threads BEFORE the write lock; a folder that looked empty is looked at
  again inside it, so a file arriving in between keeps its series.
- **"Unknown is not a no."** The reconciliation DELETES permanently, so
  filesystem helpers return `None` for "couldn't tell" (a `PermissionError`),
  distinct from `False`, and only a confident "gone" deletes an item, a series or
  an error row.
- **An emptied series keeps what the reader put into it.**
  `_series_holds_reader_state` decides; `UNIQUE(library_id, folder_path)` is how
  a file landing back in the folder finds it again; `series_scope.has_items` is
  the one clause every listing uses so an empty series shows nowhere
  (`tests/test_empty_series.py`). `_get_or_create_series` returns "appeared",
  not "was inserted".
- Numbering is decided **per series**: `extract_candidates()` tags every number
  it sees (`chapter`/`volume`/`bare`, title and stem) and
  `resolve_series_numbers()` picks one kind for the series. Parsers must not
  guess. A bracketed group is a tag, not the name (`Serie 032 (2026)` is 32);
  `_renumber_series` runs wherever `_recompute_series_cover` does.
- **Archives:** format from magic bytes, not the extension (a RAR renamed
  `.cbz` is an actionable error). No CBR conversion. A file not yet a valid zip
  is retried at 2s/5s/10s **only if modified in the last two minutes**
  (`SETTLING_SECONDS`) — an old broken file is reported at once. The watcher
  also skips a file whose `(size, mtime)` changed during its debounce. A
  successful read clears that path's error rows: `/library/issues` is current
  state.

### Readers

- Both text readers (EPUB, web novel) are **one engine**, `reader-text.js`, with
  an adapter per `data-source`. Text positions are `elidx:N:OFFSET` (UTF-16
  offset in the Nth block); legacy `elidx:N` means offset zero. Web novels
  store the same offset in `character_offset`. Both modes share the anchor.
  Reading terminology is in `CONTEXT.md`. Visual hyphenation preserves these
  offsets and original text. Its override/language are per work and browser;
  no language means no automatic hyphenation. Native dictionaries are browser
  dependent. Exercise glyph continuity and resume in both browsers when changing
  hyphenation, paragraph fragmentation or source-language propagation.
- **A block is `reader-blocks.js`, for both modes** — never a selector of the
  engine's own. The body is walked: p/h*/pre/table/figure/hr are one block
  whatever they hold, a container holding blocks is descended into, loose text
  is a block. A page copies each block's containers around it, merged with its
  neighbour's, and carries the chapter's stylesheets and body attributes. The
  flat `querySelectorAll` it replaced lost `<th>`/`<hr>`/loose text, split
  tables into run-on text and showed a quoted `<p>` twice; paging a book built
  for it forward and back, both browsers, the pages now equal continuous
  scroll's text exactly.
- Read continuous scroll's position **before hiding it**: hidden, every offset
  is 0 and the switch to Paginated opened on the last block mounted.
- **A file newly moved into `<head>` is missing from every tab opened before
  it shipped** (a boost never replaces `<head>`; think installed PWA). Its
  caller must notice and reload once — `reader-text.js` does, for
  `reader-blocks.js`; without that the reader never rendered.
- **Fragments keep DOM text offsets, not page numbers.** `reader-pagination.js`
  splits rendered lines while `reader-blocks.js` owns block identity and ancestor
  reconstruction. Reserve a stable scrollbar gutter: Chromium otherwise wraps
  the same fragment differently after an overflowing trial. Run
  `scripts/test_pagination_browser.py` in both browsers; it compares forward and
  backward content and resumes across reflow and a sub-1% device advance.
- **Firefox Android inflates long flow roots independently.** The iframe's
  `-moz-text-size-adjust: none` prevents it; Gecko ignores the shared `100%`
  value. `scripts/probe_android_typography.py` exercises the production CSS with
  an inflation control, original heading sizes and reader font scaling. Desktop
  viewport emulation is not Firefox Android evidence.
- **Paginated text uses ONE iframe** laid out by `renderPage`; every await in
  it re-checks `pgGeneration`, so a page turn or a settings change started later
  wins (85 → 50 ms a turn in Chromium, 100 → 49 in Firefox).
- The chapter iframes' sandbox has **no `allow-scripts`** (`createChapterIframe`):
  an EPUB's own scripts never run. A chapter that failed to load is not cached;
  continuous scroll retries it rather than mounting past it.
- Continuous scroll saves on a block change, throttled to 1 s (one wheel scroll
  posted 60 saves, now 3); the bottom of the last chapter is the end.
- The comic reader has ONE page store (`pageBlobs`/`pageRequests` keyed
  `"item:page"`, invalidated by `storeEpoch`), sharing in-flight requests and
  revoking on leaving; moving into the next chapter updates `#reader-root`'s
  data attributes (`rememberPosition`). A landscape page is never paired.
- Chapters render in a sandboxed `srcdoc` iframe: **events do not bubble out**,
  so listeners and the stylesheet go inside every section
  (`wireIframeInteractions`, `data-stylesheet-url`).
- Progress is **weighted by each spine entry's size** (EPUB: uncompressed
  bytes; novel: content size), average for an entry without one.
- Text replacements apply to text BETWEEN tags as a chapter is fetched, before
  anything measures it; never to the file or `content_html`.
- The end of an EPUB **navigates** to the next item (`_next_item`), saving the
  one being left as finished first. **With no next volume, both readers say so**
  (`#series-end` in the comic; built in JS in the text reader).
- A web novel saves `scroll_percent` (whole novel; rate learning only) and
  `chapter_percent` (the card's percentage); the card's TIME is the volume's.
- Comics are `(page + 1) / pageCount`; spreads are found by replaying the
  renderer's walk over `/dimensions`. **Reaching the bottom finishes a comic
  however short the last page**: `measureCurrentScrollPage` answers the last
  page when nothing is left to scroll, and `postProgress` lets "reached the end"
  outrank the page on screen.
- `h-dvh`, `viewport-fit=cover` and a bottom-gap setting, because a phone's bar
  overlays the last line.
- **A page is measured in the font it is drawn in** (await `doc.fonts.ready`
  after the text is in, bounded), and the RENDERED page is checked against the
  viewport before it is shown, cutting the overflow off the edge the reader is
  moving towards (the first cold page was 899px in an 807px viewport).
- The display settings are ONE floating panel (`reader/_settings_panel.html`)
  rendered by both readers; `tests/test_reader_settings.py` compares the engine's
  ids against both pages.
- **The readers are `base.html` with three blocks overridden**
  (`tests/test_reader_shell.py`): `viewport_content` (adds `viewport-fit=cover`;
  a string because it is rendered into `<head>` and onto `<main>`), `chrome`
  (empty) and `live_updates` (empty: a reader opens no stream). The page shape
  is `body:has(#reader-root)` in CSS, not a body class — a boosted swap keeps
  `<body>`'s attributes, and the reader rendered 0px tall without it.
- **Readers are boosted too**, and that holds only because: they save on
  `htmx:before:history:update` (raised before the response is parsed, and only
  by a navigation that moves the URL) AND on `htmx:history:cache:hit` (Back
  raises nothing else); **nothing in a reader binds to `document`/`window`
  directly** — `bindOutside` wraps every handler in `root.isConnected` and a
  visible exit drops the whole set and stops `readingTime` (without it, arrow
  keys on the library page posted progress for a closed book and marked it
  finished); `fullscreen.js` keeps one `fullscreenchange` listener per document.
- **A book is finished once.** The readers post `completed` on every save from
  the end on; only the save that changes the status stamps `completed_at` and
  queues a tracker push. "Mark as read" keeps an already-completed row's date.
  A first save that is already the finish stamps it too.

### Reading time and estimates

- `page_reads` stores the RAW duration and the `cap_seconds` in force; counted
  time is `MIN(seconds, cap_seconds)`. Legacy clients send durations and `ago`;
  modern visits use server-anchored visible intervals and account session claims
  (`stats/activity.py`). Checkpoints append only accounting deltas under the
  same `visit_id`; count distinct visits, not checkpoint rows. Caps survive
  checkpoints/retries. Word totals and valid pace samples are separate; text estimates use only
  matched valid word/time evidence, while historical durations remain intact.
  A boosted reader upgrades the shared recorder from the current body asset
  URL when its retained head has an older bundle; keep the same document.
  An implausible legacy duration is clamped, an implausible `ago` rejected.
  Spans under 1s are dropped; a
  hidden tab stops the clock. A span whose item or chapter is gone is skipped
  by `INSERT … SELECT` (one used to fail the whole batch of up to 200); a
  chapter's novel comes from `chapters`, never from the client.
- **`page_reads` survives its content** (`ON DELETE SET NULL`) and stores its
  own `kind`: deleting a file does not un-read it. Every query grouping by
  `item_id` needs `item_id IS NOT NULL`. What IS about the item (title, series
  folder, completion, size, each reader's summed time) is kept as a **removed
  item** (`removed_items` / `removed_item_reads`, written by
  `scanner._delete_item` only when someone read it; AUTOINCREMENT, never a
  reused rowid; series matched by `library_id` + `folder_path`). It counts in
  "Most recent", start-to-finish and the rate, never in the Libraries counts
  (docs/adr/0001).
- Text estimates (`stats/estimates.py`) count words ahead of the saved source
  section/block/UTF-16 offset, independently of the displayed byte-weighted
  percentage. EPUBs and web novels share valid pace evidence; legacy durations
  never supply a word rate. The evidence floor matches Statistics (1,000 words,
  300 seconds, three samples); own-work samples gradually replace a prior worth
  at most 3,000 words from other works. Only insufficient evidence shows
  `Collecting data`; pending, unavailable content/position and request failure
  are distinct states. Novel scope is explicit: Reading volume versus whole novel.
  `prepare_items` / `prepare_novel` lazily load only requested word inventories.
  Inventory caches contain offsets/counts, not source text; source/rules/version
  invalidate them. Account-scoped remaining/total word snapshots live in a separate
  bounded derived SQLite cache (no main writer/pool slot); source/position/rules
  invalidate them. Both HTML and the authoritative endpoint apply current pace
  to these counts: unchanged requests read the index once, traverse no source
  inventories and rewrite no snapshots. Old duration snapshots upgrade once.
  Chapter content revisions
  must change even on same-size edits. Series totals retain known time and count
  missing estimates explicitly. Every HTML render is cache-only, even for one
  EPUB or chapter: saving a new anchor can require a full inventory.
  Account/work pace totals use an append-only ledger trigger and a once-only
  backfill receipt. Removed content retains account evidence; per-work totals
  cascade so reused IDs inherit none. See `docs/reading-statistics.md`.
- Comic estimates retain pages/series rates, with the 95th percentile of
  per-series rates for an unread series. Own-item evidence requires at least
  5% progress and 60 counted seconds.
- Text cards refresh authoritative estimates on progress or new committed valid
  pace evidence (`reading_estimate_updated`); percent-only arithmetic is for
  comics. Preserve the document, card and cover on these numeric updates.
- Container publication scans the exact OCI archive before upload; recovery
  scans the original immutable digest. Preserve that ordering, archive identity
  and the fail-closed CRITICAL policy in `docs/security-scanning.md` when changing
  Docker checks, release publication, recovery or reconciliation.
- A home card's badge and estimate cover the same unread scope. Text estimates
  carry their explicit item/series/novel scope; chapter percentages cannot scale
  a whole volume or novel estimate.
- **Marking read is a declaration.** `completed_from_percent` (and the novel's
  `read_from_percent`) records how far the reader had really got; the rate uses
  it instead of 1.0 (one book ticked off at 12% made every card 8x too fast),
  and `time_to_finish` admits only completions from 90%. NULL = recorded before
  this existed, taken at its word.
- Clearing a status DELETES the `reading_progress` row (the row is what puts a
  card in "Reading"); `page_reads` stay. It asks via `hx-confirm`, never an
  inline `onsubmit` (two dialogs).
- `/stats`: daily measures share an accessible tab area; date swaps retain the
  selected tab. Window time sums the same daily rows as the time chart. The
  three headline indicators retain full-history scope. The daily window is the one server-side control — links to
  `/stats?days=N` swapped into `#daily-activity`, both built from
  `_daily_context`; an unparseable date is a 400. Daily time stacks by
  `page_reads.kind`; the visit chart counts comics only. Fixed
  `DAILY_KIND_SLOTS` (never by rank; "Other" is slot
  4, grey failed the validator); a day is the SUM of its kinds; segments share
  the column by `flex-grow`; the tooltip text is server-rendered into `data-tip`
  and `aria-label`; no `title`; roving tabindex. The calendar builds local
  `YYYY-MM-DD` strings (never `toISOString()`), with the max day from the server.
  "Most recent" is one entry per series FOLDER and kind (a web novel, or an
  item in no series, is one too), totalling everything read in it and opening
  (`<details>`) onto its items newest first; entries under 60s counted are
  dropped (the floor is on the entry, not the item); it sends the top
  `RECENT_LIMIT` entries of EVERY kind (filtered client-side). Web novels sit
  in Libraries as one more block, chapters by reading status. Text pace uses
  matched words/time across EPUBs and novels together; historical time remains
  intact. See `docs/reading-statistics.md` for eligibility and sample floors.
  The start-to-finish tiles split comics from prose. A novel's unit is the volume.
- `format_duration` exists in Python and JS, both rounding half UP;
  `tests/test_duration_parity.py` compares them.

### Live updates (SSE)

- **The connection is htmx's `hx-sse`**, on `#library-events` in `base.html`;
  `library-events.js` is only handlers. **Every frame is NAMED** (an unnamed one
  would be swapped into the page as HTML); `frame()` writes them all.
  `tests/test_events.py` compares every published `type` against the handler
  keys, both ways.
- **Messages are HELD until the handlers exist**: an inline script right after
  `#library-events` (base.html) `waitUntil`s every `htmx:sse:before:message` on
  a promise library-events.js releases once bound. htmx (deferred) connects on
  a timer that can beat the deferred library-events.js -- measured in Chromium,
  a document that arrived at once got `events_gap` before any handler.
- The extension ships **in the same file as htmx** — as two files a timer ran
  between them and 0 of 10 slow loads connected.
- **Nothing is replayed.** Every connection compares where the page stands
  (`Last-Event-ID`, or on a document's first connection the `?since=` base.html
  renders into the stream URL) with `events.is_current`: behind → `events_gap`,
  which the page answers with a re-sync; then always an `id:`-only POSITION
  frame, so the next reconnect is compared from here. A full queue
  (`MAX_PENDING_PER_STREAM`) gets the same gap + position.
- `#library-events` survives a boosted swap: it carries `hx-preserve` and steps
  OUT of `<body>` for every swap (`insertAdjacentElement("afterend")`), so one
  connection lasts the tab. Its handlers bind once per ELEMENT and `pageshow`
  once per document; nothing is captured at load time.
- **A resync never morphs a reader** (`#reader-root` is skipped), and an
  `item_removed` removes CARDS only (`itemSelector` excludes the reader). The
  home grid is looked up when an event arrives (`homeGrid()`), never held.
- Leaving a page is not an error: at `beforeunload` the page sets a sentinel
  `{signal: {aborted: true}}` so the extension stays quiet — Firefox tears the
  fetch down between `beforeunload` and `pagehide` with a plain `TypeError`.
- Events carry the value where they can (a progress percentage updates a card
  with no request); a STATUS change refetches the card or grid. Every path that
  changes progress or a status publishes; a whole-series action sends one event.
  A status event carries no status (it is per reader), so each browser
  refetches `/library/{kind}/{id}/card`.
- Ids are `{epoch}-{n}`, in memory only; the EPOCH is drawn on every boot, so
  a restart makes every open page behind once (one morph each) -- without it a
  counter restarted at 1 would read as current. `job_progress` has no id.
  `data/event-sequence.json` (an older persisted sequence) is deleted at boot.
- **Re-syncing is a MORPH, not a reload** (`resync`, `tests/test_resync.py`):
  fetch the same URL (`no-store`) and `htmx.swap` it with `innerMorph`, so
  scroll, focus, typed text and open dialogs survive. Each page re-applies its
  own state afterwards (`libraryGrid.resync()`, the series sort/search on
  `htmx:after:swap`, `novelChaptersApply`), because an identical `<script>` is
  kept, not re-run. `open` on `<details>`/`<dialog>` is kept by the `resync`
  EXTENSION (the morph's attribute hook exists only for extensions, and the
  whitelist must name it). When the stream's `lastEventId` is past the fetched
  page's `data-rendered-at` (an event landed between render and morph), it
  re-syncs again. Transient fetch/morph errors retry with backoff, never reload;
  redirects navigate to their destination. A late response cannot morph a
  different page after boosted navigation. Home cards need stable
  `library-card-{kind}-{id}` ids in BOTH Jinja and the live JS builder: with
  40 reverse-sorted cards, unkeyed morphing replaced 31 images. Run
  `scripts/test_resync_browser.py` in Chromium AND Firefox for these paths.
- **Three caches can answer a Back:**
  - the browser's back/forward cache (`pageshow`, `persisted`): the reconnect's
    `Last-Event-ID` is compared by the server like any other; the page only
    re-syncs itself if no connection comes back within `RESUME_GRACE_MS`;
  - htmx's history cache: each snapshot is stamped in `sessionStorage` with
    `{doc, seen}` (event count) at `htmx:before:history:update` (not
    `cache:after:save`, which fires after `history.state` moved). On a hit:
    stamp holds → kept, zero requests; otherwise restored and re-synced at
    `htmx:history:cache:after:restore` (`cache:hit` fires before the swap). A
    back/forward-cache restore bumps the count;
  - the HTTP cache (Firefox answers Back from it despite `no-cache`, `persisted`
    false): the stream URL's `?since=` is the render's position, so the first
    connection is told it is behind.
- **A restored reader asks the server** (`reader-resume.js`, in `<head>`):
  `GET /reader/{id}/progress` or `/novels/{id}/progress`, on a `persisted`
  restore, on `htmx:history:cache:hit`, and on any `back_forward` navigation. It
  compares against what THIS TAB last sent, by section/block/character for text
  readers and percent for comics/legacy clients. It also checks on returning to
  a visible tab. Only an ahead position replaces the page; exit saves must not
  overwrite it (`data-resume-pending`).
- Markup this file swaps in itself goes through `htmx.process` (`wireHtmx`).
- The "Reading" strip promotes a card on a progress event with no refetch (both
  writers set the sorted column to `now`). A new series card's insertion is
  awaited by later `item_added` events (`pendingSeriesCards`); bursts coalesce in
  `scheduleRefresh` (500 ms).

### Client-side sorting, filtering and pre-paint

- Every sort is `appendChild` over cards already rendered. The direction is in
  the comparator (never `reverse()`, which flips ties — sorts run twice), and
  numeric fields compare as numbers.
- The series page's "Original" order comes from `data-original-order`; sorting
  by estimate uses the projected total. The SELECT renders only when there is
  more than one order; the direction button always. The sort is saved per
  series. The sort, Reorder and Reset live in `#series-order-panel` under the
  title (a novel's Reorder and Reset in `#novel-order-panel`), opened by the
  icon beside the others (`disclosure.js`; only the icon closes it) and
  rendered open while dragging — "Done" is how that mode ends. The series icon
  is lit for any order but the default, in both copies (pre-paint and
  `renderToggle`), like the filters icon.
- **Pre-paint state is one inline block computing the final answer**, in the
  body. Separate blocks per criterion un-hid each other's cards for a frame.
  Every filter clause lives in TWO places that must agree: `applyFilters()` and
  the pre-paint block.
- Theme, thumbnail size and a novel's "Unread only" are **cookies** (`ui_prefs.py`
  reads, `ui-prefs.js` writes), so the server renders them in the first byte.
  "Unread only" is per series (localStorage) and per novel
  (`novel-chapters-unread-only:<id>`, set to "1" and deleted when off).
  **Every control carries `autocomplete="off"`** (sign-in/setup and credential
  fields aside): Firefox restores controls on reload, and under hx-boost the
  state it restored was ANOTHER page's — a series reloaded filtered by the
  novel's checkbox (`tests/test_form_restoration.py`). The theme
  goes on `<html>`; the per-page ones on `<main>` (inside what a body swap
  replaces). **A cookie NAME is percent-encoded in transit**
  (`thumbnail-size:series` arrives as `%3A`), and `ui_prefs._cookie` decodes it.
  Every value is validated against what the UI can produce, never echoed.
- Thumbnail size's maximum is the cover's resolution (400). Mobile header
  popovers span the header, independently of the compact search group. The
  header stays on one row at normal text sizes from 320px; enlarged text may
  wrap. `test_header_browser.py` and the Android header regression measure
  geometry and interactions, and reject the original two-row layout.
- `?library=<id>` selects a library tab on arrival, in both copies, only when a
  `[data-library-filter]` of that name exists.
- The header's filters live behind one button (`_filters_panel.html`,
  `filters-panel.js`); the controls stay in the DOM so both copies stay
  identical. The icon is lit (`text-indigo-400`) when the panel is off its
  defaults — computed by each page (`setActive()`), in both copies.
- "Show everything" is a checkbox drawn as an eye (`:checked` in CSS); hidden
  statuses stay as real `hidden` options. `series.blur_cover` blurs the home
  grid's `<img>`, and its items' in the Reading strip and the item search
  (never on the series page); each of those grids carries `--cover-blur`
  itself, since a refresh replaces it, and `series_updated` re-renders them.
  The radius is `users.cover_blur_px` as `--cover-blur`, and a key missing from `get_current_user`'s dict renders nothing (and the blur with
  it). The overhang is `-2 * var(--cover-blur)` with the size written out in
  full (`img { max-width: 100% }` pins a replaced element).
- The home library filter is tabs (`aria-pressed` is the state and the style),
  one hidden Rescan form per library, "+" for a new one
  (`tests/test_home_tabs.py`).

### Tracking (MangaUpdates, MyAnimeList)

- **A MAX, never a count, and never backwards.** The highest `number_in_series`
  marked read; nothing sent unless higher than what was last sent AND the
  service's own number, read before every write.
- Units: a scanned series moves in ONE unit (`series.number_kind`) and says
  nothing about the other; a web novel moves in both (chapter = MAX read; volume
  = highest with nothing unread). `tracker_links` caches what was sent per field,
  and agreeing with the service is recorded only for a unit the shelf counts.
- **Every status a catalogue has a word for is reported once per change**
  (`STATUSES` / `STATUS_FOR` per client; "Stopped reading" has no counterpart).
  `last_pushed_status` holds what the service heard. A status needs no progress.
- Dates are MyAnimeList-only (`my_list_status{start_date,finish_date}` must be
  named); a push fills one only where the site has none.
- A score of `0` is "unrated" on both; `score_in_range` refuses 0 and >10.
  MangaUpdates rates at its own endpoint, hence `get_progress` (push) vs
  `get_state` (editor). MyAnimeList's list call **replaces the record**, so each
  client writes through one `set_state(...)`; MAL scores round half UP by hand.
- A link is **chosen from search results**, never matched by title; results are
  narrowed to the shelf's family (prose/pictures), counting what was dropped.
  Linking runs `catch_up` BEFORE the push (only ever marks read, only upward, in
  the shelf's own unit from `LocalProgress.kind`), and brings the description
  into `series.summary` (`source_summary` = the tracker's wording).
- One icon per service beside the title, each opening its own `<dialog>` (no
  `closedby`: Escape closes, the backdrop does not). The panel is fetched on the
  click, blanked first when switching service, and the editor inside is the only
  path that can walk a tracker backwards. Cancel is `type="button"`.
- Every request goes through `models.send` (the module's `_client` is the
  seam tests mock); the status is the caller's, since a 404 means "not listed"
  to some calls.
- **Pushes are queued after the commit, fire-and-forget, one per shelf at a
  time** (`push()` itself holds a per-shelf lock, for the backfill and linking) (a request meanwhile runs once after). Only the session token is
  stored. MAL is OAuth2 PKCE (`plain`); each installation supplies its own
  `APP_MYANIMELIST_CLIENT_ID` and `APP_MYANIMELIST_REDIRECT_URI` matching its
  registered callback. Missing either refuses a connection before writing OAuth
  state. Token refresh needs the original registration's client ID and any
  required secret. A provider's `error=` is shown in its own words.

### Other

- **Auth forms are `hx-boost="false"`**: a refusal is rendered as a page (a
  boosted 401/429 is not swapped, so a wrong password showed nothing). An
  expired session answers htmx with `HX-Redirect` to the login, keeping the
  query string in `next`.
- **Auth:** a TOTP code signs in once — its time step is claimed in the same
  statement that checks it (`totp_credentials.last_used_step`), and the setup
  code counts as used. The failed-login memory is keyed by username (proxies
  hide client IPs), bounded to 1000 names. argon2 runs in a thread, against a
  dummy hash for an unknown name. `_safe_next_path` / `safe_redirect_target`
  are the open-redirect guards.
- **A passkey login challenge is per browser**: each `begin` adds a row named
  by the `passkey_challenge` cookie (one shared row let any stranger's `begin`
  fail the owner's login); `_consume_challenge` takes it out with `DELETE …
  RETURNING`, only within `CHALLENGE_SECONDS`. A session is started only by
  `_start_session` (sweeps expired rows) and its cookie set only by
  `_set_session_cookie` (`max_age` = `session_lifetime_hours`).
- **Bytes the app did not write are sandboxed** (`UNTRUSTED_CONTENT_HEADERS`:
  CSP `sandbox`, `nosniff`, on the 304 too): an EPUB's files and a novel cover.
  Opened as a page (a book's `target="_blank"` link), a book's script read
  `/settings/security` with the session, both browsers.
- Manual reordering is **SortableJS** (native drag-and-drop is mouse-only). Its
  `ghostClass` must have a CSS rule, and swapped-in containers are re-armed by a
  MutationObserver.
- No pagination anywhere. A novel page renders every chapter and names its
  columns (900 chapters: 16.3 ms / 6.5 MB with `SELECT *`, 5.6 ms / 0.21 MB
  without). Its filter is not rendered in drag mode, re-applies on a
  MutationObserver over `data-read`, and only a text search forces volumes open.
- Edit panels (series, novel) are native `<dialog>`s opened by `dialogs.js`;
  endpoints swap the panel inside, reach the headline by `hx-swap-oob` (rendered
  even when empty), signal success with `HX-Trigger`, refuse with a **422**, and
  keep `method`/`action` for no-JS. A novel's title/synopsis follow the source
  only while they still equal `source_title`/`source_synopsis`.
- **Publication status is not tracking status**: two lists, a column on
  `series` defaulting to `ongoing`; a web novel's comes from its source
  (unrecognised → no badge). A search sets both status filters aside. On a page
  each status is a chip with a transparent `<select>` over it (`opacity: 0`,
  never `display: none`), answering with the re-rendered badge.
- Clamped text ("Show more") is `.clamp-3` in CSS; `_clamp.html` reserves the
  button's slot for text over 120 characters and `read-more.js` hides an
  unneeded one with `visibility` (0 CLS).
- Settings save through htmx with `hx-swap="none"` and a 204; a refusal is a
  redirect. The help bubbles span their row, open on hover, focus-within and
  `[data-open]` (`tooltip.js`, for touch), with a 300 ms grace.
- `select[data-searchable]` becomes a combobox; `required` moves to the visible
  input and `setCustomValidity` is kept current.
- **Fullscreen:** the manifest's `display: fullscreen` for the installed app;
  in a tab, one tap per page armed on `click`/`touchend`/`pointerup`/`keydown`
  (touchstart is not an activation). `inAppFullscreen()` turns the arming off
  when the display mode already is fullscreen. Android bakes `display` into the
  WebAPK at install time.
- `range-value.js`'s first pass waits for `DOMContentLoaded` (tested with
  `readyState === "complete"`, not `"loading"`).
- The text-replacement list swaps the whole `#replacement-rules` partial, whose
  count and cap live inside it.
- OPDS was removed (unused, and the only Basic-Auth surface).

## Working on this repo

**Write pull request titles and descriptions in English.**

**After creating or updating a PR, monitor every required CI check to completion.**
Investigate and fix failures before handing back; report any genuine blocker explicitly.

**A merged pull request is finished — never push follow-up work at it.** Check
before every push whether the branch's PR is still open, and open a NEW one when
it is not.

## How things get verified here

Claims are measured, not asserted.

- **Both browsers, always.** Firefox and Chromium disagree about things this app
  depends on (a torn-down fetch, bfcache, when a restored page's scripts run).
  Where one cannot drive a mechanism (Playwright's Firefox never restores from
  the back/forward cache), say so beside the number.
- **Parallel browser checks use isolated checkouts.** See
  `docs/testing/browser-checks.md` for browser selection and mutation isolation.
- **Desktop plus mobile viewports.** Run Chrome/Chromium and Firefox desktop,
  including the responsive/mobile viewport scenarios in both engines. Both targets
  must pass. Select current stable versions once per local run and retain those
  versions throughout it; an older fallback does not satisfy validation.
  Native Android browser checks are deferred and optional; their scripts remain
  available for future work in `docs/testing/android.md`. Describe narrow desktop
  viewport results as simulations. Keep user-reported device results distinct
  from automated test evidence.
- **Mutation-test every new test**: break what it guards and see it fail.
- **A probe installed after navigation cannot see a flash** — use
  `add_init_script` and check the instrument against a control.
- `page.fill()` fires `input`, not `keyup` (htmx triggers on `keyup`); use
  `press_sequentially`. Programmatic `scrollTop` proves nothing in continuous
  scroll.
- Counting listeners across boosted visits needs a real boosted link
  (`htmx.process` on an injected `<a>`), and a same-document marker on `window`
  — `performance.getEntriesByType("navigation")` has one entry in any document.
- Pixel diffs need two servers, two data directories and a control on both
  sides (±1 antialiasing noise at radii).

## Status

Implemented and exercised end to end: auth (password, TOTP, WebAuthn via
`soft-webauthn`), scanning of real CBZ/EPUB files, both readers, web-novel
scraping, statistics, tracking, and the live-update path. The readers' own
maths (pagination, tap zones, scroll measurement) is covered by browser runs,
not unit tests. The Docker build is exercised as CLAUDE.md describes.
