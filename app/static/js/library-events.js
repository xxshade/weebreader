/**
 * Listens on /library/events (SSE) and reflects changes on screen, without
 * an F5, whenever the watcher or a scan changes the library.
 *
 * Every change updates whatever screen is actually open: a removed item
 * fades out, an added one appears in its correct place, and the owning
 * series' cover/item count follows along on the home page. The banner is
 * only a fallback now, for the cases where there is genuinely nothing on
 * screen to update (a standalone item with no series card, or a request
 * that failed) -- it used to be the whole response to an added item.
 *
 * What made the banner necessary before was that placing a new card
 * correctly means knowing the series' natural/manual ordering and its
 * pagination, which only the server knows. So the server renders it: the
 * handlers below re-fetch the same partials the page was built from
 * (/series/{id}/items-grid, /library/series/{id}/card) instead of
 * reimplementing any of that here.
 *
 * Runs on every authenticated page (included via base.html) -- the handlers
 * simply find nothing to do on pages without the matching
 * [data-item-id]/[data-series-id] elements.
 *
 * The CONNECTION is not this file's any more: #library-events carries
 * hx-sse:connect and htmx's hx-sse extension owns it, delivering each named
 * message as a DOM event on that element. This file is the handlers and
 * nothing else. See the bottom of the file for what that buys and for the
 * ordering it depends on.
 */
(() => {
    // How long a restored page waits for its stream to come back before
    // giving up and re-syncing from the server (see resync). The extension
    // reconnects with a 500ms base delay and up to 30% jitter, and the
    // resume measured here landed well inside this; what the margin is
    // really for is a reconnect that has to wait on the network. Too short
    // only costs the re-sync this avoids.
    const RESUME_GRACE_MS = 3000;

    // A page restored from the browser's back/forward cache comes back
    // with its DOM exactly as it was frozen and its stream down for the
    // whole time away. It is not reloaded: the reconnect sends the last id
    // the stream saw as `Last-Event-ID`, and the SERVER says whether
    // anything was published since -- `events_gap` if so, which re-syncs
    // (see the foot of this file), nothing if not. So a restore costs a
    // request only when the page is actually behind.
    //
    // The one thing the page checks itself is that the stream DID come
    // back: no connection within RESUME_GRACE_MS, and it re-syncs anyway.
    //
    // Chrome is not the only browser here: Safari keeps pages with an open
    // stream in its page cache and suspends them, which is exactly the
    // shape this guards. Registered before anything else in this file so it
    // applies even where the stream never starts.
    //
    // Bound once per DOCUMENT. The window outlives a <body> swap and this
    // file runs again after each one, so without the guard a restore
    // would run this once per navigation made before it (measured
    // under hx-boost: 1 listener on the first page, 4 after three
    // navigations).
    if (!window.libraryEventsRestoreBound) {
        window.libraryEventsRestoreBound = true;

        // ---- Carrying the stream through a page that has none ----
        //
        // `hx-preserve` covers the swap where the incoming page ALSO
        // carries this element: htmx matches it by id, moves the live one
        // into the new page's position, and the connection is never
        // touched. A READER carries none -- every reader template renders
        // an empty `live_updates` block on purpose -- so there is nothing
        // to match, and the element goes the way of every other child of
        // <body>: htmx cleans it up, and the hx-sse extension answers a
        // cleanup by aborting the stream.
        //
        // Measured before this, one boosted click from the home page
        // into a comic reader, both browsers: **0** stream elements
        // left, `htmx:sse:close` with
        // reason `cleanup`, and an uncaught `BodyStreamBuffer was
        // aborted` (Chromium) / `The operation was aborted` (Firefox) --
        // then a second `GET /library/events` on the way back out. Per
        // reader visit.
        //
        // So the element steps OUT of <body> for the duration of the
        // swap. That is htmx's own trick for the nodes it preserves
        // (`document.body.insertAdjacentElement("afterend", ...)`), and
        // it works for the same two reasons: the node stays IN THE
        // DOCUMENT, so `isConnected` -- the flag the extension's read
        // loop tests before every frame -- never goes false, and no swap
        // target contains it, so no cleanup reaches it.
        //
        // EVERY swap, rather than the ones that look like they need it.
        // Telling those apart means predicting what the incoming content
        // holds, and a wrong prediction is a torn-down connection. This
        // is two DOM moves, and it composes with `hx-preserve` instead of
        // competing with it: htmx looks the parked node up by id, which
        // does not care that it is sitting outside <body>, stashes it and
        // puts it back in the incoming page's position -- leaving the
        // restore below nothing to do.
        let parked = null;
        document.addEventListener("htmx:before:swap", () => {
            const stream = document.getElementById("library-events");
            if (!stream || !document.body.contains(stream)) return;
            parked = stream;
            document.body.insertAdjacentElement("afterend", stream);
        });
        // `finally:swap`, not `after:swap`: a listener that cancels the
        // swap returns before `after:swap` is raised, and the element
        // would be left outside <body> for good.
        document.addEventListener("htmx:finally:swap", () => {
            const stream = parked;
            parked = null;
            // Already back where it belongs whenever the incoming page
            // had a place for it. Only the page that had none -- the
            // reason this exists -- leaves anything to do, and the foot
            // of <body> is where this element lives anyway.
            if (stream && !document.body.contains(stream)) {
                document.body.appendChild(stream);
            }
        });

        // ---- Bringing a history snapshot the library has outgrown up to date ----
        //
        // hx-boost means Back is htmx's now, and the history-cache
        // extension answers it from a copy of the DOM in sessionStorage
        // instead of from the server. That is a straight trade against
        // something this app went out of its way to have: every HTML
        // response carries `no-cache` and an ETag precisely so a Back
        // REVALIDATES, because the library changes underneath it.
        //
        // The stream is what makes the trade payable. It stays connected
        // across a boosted swap (hx-preserve), so this document KNOWS
        // whether anything happened while a page was off screen -- and if
        // something did, the snapshot of that page is a picture of the
        // library before it. Measured: a series renamed from another
        // client while this tab sat on /novels/1, then Back -- the cached
        // restore showed the OLD name, with zero requests.
        //
        // The counter is compared against its value WHEN THAT SNAPSHOT WAS
        // TAKEN, not against zero. Events that arrived while the page was
        // on screen were applied to it live and are in the snapshot; only
        // what came after leaving makes it a lie.
        //
        // AND THE STAMP IS PER SNAPSHOT, kept beside the extension's own
        // entry rather than in a variable. The two live very different
        // lives: the extension stores each snapshot in `sessionStorage`
        // under `htmx-history-<htmxId>`, which outlives the document,
        // while a counter on `window` dies with it. So a NEW document is
        // served a snapshot written by a dead one and reads its own
        // counter of 0 against a baseline of 0 -- agreement by
        // coincidence, over a page that may be any amount out of date.
        // That is what Firefox does on the ordinary way back from a
        // reader, which is not boosted and so replaces the document --
        // so a stamp from another document never holds.
        //
        // `history.state.htmxId` is the extension's own key. It is read
        // at `htmx:before:history:update` and NOT at the extension's own
        // `cache:after:save`, which looks like the obvious place and is a
        // trap: a restore SAVES the page it is leaving too, and by the
        // time that fires `history.state` has already moved to the entry
        // being restored -- so stamping there overwrites the very stamp
        // the hit is about to check, with this document and this count,
        // and the guard agrees with itself. `before:history:update` fires
        // only going forward (no restore emits one) and still names the
        // entry being left, which is the one just snapshotted.
        //
        // A snapshot whose stamp holds is restored and left alone: zero
        // requests, because nothing has happened since it was taken. Any
        // other is restored too -- what is on screen meanwhile is this
        // page a moment ago rather than a blank -- and then re-synced,
        // once the extension has put it back and restored its scroll
        // (`cache:after:restore`), so the morph starts from it.
        window.libraryEventsSeen = 0;
        const THIS_DOCUMENT = `${Date.now()}-${Math.random()}`;
        const stampKey = (id) => `library-events-stamp-${id}`;

        document.addEventListener("htmx:before:history:update", () => {
            const id = history.state?.htmxId;
            if (!id) return;
            try {
                sessionStorage.setItem(stampKey(id), JSON.stringify({
                    doc: THIS_DOCUMENT,
                    seen: window.libraryEventsSeen,
                }));
            } catch {
                // No storage (private mode, blocked site data). The read
                // below then finds nothing and re-syncs.
            }
        });

        document.addEventListener("htmx:history:cache:hit", () => {
            let stamp = null;
            try {
                stamp = JSON.parse(sessionStorage.getItem(stampKey(history.state?.htmxId)));
            } catch {
                stamp = null;
            }
            if (stamp && stamp.doc === THIS_DOCUMENT
                && stamp.seen === window.libraryEventsSeen) {
                return;
            }
            document.addEventListener("htmx:history:cache:after:restore",
                                      () => resync(), { once: true });
        });

        // ---- Re-syncing the page in place, instead of reloading it ----
        //
        // Every "this page is behind and the events to catch it up are
        // gone" used to end in `location.reload()`: correct, and it threw
        // away the scroll position, the focus, a text selection and any
        // dialog open on the page -- to show, usually, the same page with
        // a couple of numbers changed. It is the answer to every page that
        // is behind -- a stream reconnecting after events it never saw, a
        // server restart, a history snapshot the library has outgrown --
        // since nothing missed is ever replayed (see events.py).
        //
        // So the page is fetched again and MORPHED into the one on
        // screen: htmx's `innerMorph` walks the two trees and changes only
        // what differs, so the nodes the reader is looking at, scrolled to
        // or typing in stay the same nodes. Measured on the home page with
        // the grid scrolled and the search box focused: scroll and focus
        // unchanged, the stream still connected, one document.
        //
        // What a morph cannot know is what the PAGE'S OWN SCRIPTS did to
        // the server's HTML after it arrived -- the saved sort, the filters,
        // the selected library tab. Those scripts do not run again (an
        // identical <script> is kept, not re-executed), so the morph puts
        // back the server's order and the server's `hidden` classes and
        // each page re-applies its own state from what it saved:
        // `window.libraryGrid.resync()` on the home page; the series
        // page's sort and search already re-run on `htmx:after:swap`,
        // which this raises; the novel page's chapter filter below.
        //
        // And `open` is the reader's, not the server's. A <details> the
        // reader expanded, or a <dialog> they are halfway through, would
        // otherwise be closed by the morph because the server rendered it
        // shut -- so while a re-sync runs, that one attribute is left
        // alone on those two elements.
        //
        // A transient failure must leave the current document usable.
        // Retry in the background with backoff; a reload here also makes
        // the browser revalidate every image and discards local UI state.
        // An actual redirect (usually an expired session) still navigates
        // to the destination instead of morphing a login form into this page.
        //
        // That guard is an htmx EXTENSION, not a listener: the morph's
        // per-attribute hook is offered only to extensions (it is not
        // raised as a DOM event at all -- measured, a document listener
        // for it never fired and the dialog closed). Hence `resync` in
        // base.html's extension whitelist, which registration obeys.
        let resyncing = null;
        let resyncRetry = null;
        let resyncDelay = 1000;
        htmx.registerExtension("resync", {
            htmx_before_morph_attr: (element, detail) => {
                if (!resyncing || detail?.attrName !== "open") return;
                if (element.tagName === "DETAILS" || element.tagName === "DIALOG") return false;
            },
        });

        function resync() {
            window.homePreload?.invalidate();
            if (resyncing) return resyncing;
            clearTimeout(resyncRetry);
            resyncRetry = null;
            // Not over a reader. Its DOM is built by its own script, so
            // morphing the server's HTML over it blanked the page (measured:
            // the comic image's blob: src went to "" and "1 / 6" to "",
            // after a server restart reconnected the stream). A reader shows
            // nothing a missed library event could have made stale.
            if (document.getElementById("reader-root")) return Promise.resolve();
            const url = window.location.href;
            const page = document.querySelector("main");
            // A boosted navigation can finish while this request is in
            // flight. Its old response must never overwrite the new page,
            // even if the user has since returned to the same URL.
            const isCurrentPage = () => window.location.href === url
                && document.querySelector("main") === page;
            let again = false;
            resyncing = fetch(url, {
                headers: { Accept: "text/html" }, cache: "no-store",
            })
                .then((response) => {
                    if (!isCurrentPage()) {
                        again = true;
                        return null;
                    }
                    if (response.redirected) {
                        window.location.assign(response.url);
                        return null;
                    }
                    if (!response.ok) throw new Error(`Re-sync returned HTTP ${response.status}`);
                    return response.text();
                })
                .then(async (text) => {
                    if (!isCurrentPage()) {
                        again = true;
                        return;
                    }
                    if (text === null) return;
                    await htmx.swap({ text, target: document.body, swap: "innerMorph",
                                      sourceElement: document.body });
                    window.libraryEventsSeen = (window.libraryEventsSeen || 0) + 1;
                    window.libraryGrid?.resync?.();
                    window.novelChaptersApply?.();
                    again = streamIsPast(renderedAtIn(text));
                    resyncDelay = 1000;
                })
                .catch((error) => {
                    if (!isCurrentPage()) {
                        again = true;
                        return;
                    }
                    console.warn("Library re-sync failed; retrying", error);
                    resyncRetry = setTimeout(() => {
                        if (isCurrentPage()) resync();
                    }, resyncDelay);
                    resyncDelay = Math.min(resyncDelay * 2, 30000);
                })
                .finally(() => {
                    resyncing = null;
                    if (again) resync();
                });
            return resyncing;
        }
        window.libraryResync = resync;

        // Where in the sequence the fetched page was RENDERED. An event
        // published between that and the morph was applied to the old DOM
        // and then painted over with a render from before it -- so when the
        // stream has seen past that point, the page is fetched again. Each
        // round fetches a newer render, so it ends; on the ordinary path the
        // two agree and nothing more is asked.
        function renderedAtIn(text) {
            return /id="library-events"[^>]*data-rendered-at="([^"]+)"/.exec(text)?.[1] || null;
        }

        function streamIsPast(renderedAt) {
            const here = positionOf(streamPosition());
            const rendered = positionOf(renderedAt);
            // Another epoch is a restart between the two, which the
            // stream's own reconnect is told about.
            if (!here || !rendered || here.epoch !== rendered.epoch) return false;
            return here.number > rendered.number;
        }

        // A private of the vendored bundle, reached on purpose -- the same
        // one the beforeunload sentinel below reads. See src/vendor/README.md.
        function streamPosition() {
            return document.getElementById("library-events")?._htmx?.sse?.lastEventId;
        }

        // "<epoch>-<number>", which is events.py's event_id.
        function positionOf(id) {
            const parts = /^(.+)-(\d+)$/.exec(String(id ?? ""));
            return parts ? { epoch: parts[1], number: Number(parts[2]) } : null;
        }

        // Leaving the page kills the open stream, and the extension only
        // keeps QUIET about that when the break was its own: its catch is
        //
        //     h.abortController?.signal?.aborted || t.name === "AbortError"
        //
        // and htmx `console.error`s any event whose detail carries an
        // `error` BEFORE the event is dispatched -- so no listener and no
        // config can silence it afterwards. Chrome tears a fetch down with
        // an AbortError and satisfies the second half; Firefox raises a
        // plain TypeError and satisfies neither, so it logged one on every
        // reload and every navigation away.
        //
        // `beforeunload`, not `pagehide`, and that is the whole fix:
        // Firefox kills the stream BETWEEN them. Measured on a link
        // navigation -- beforeunload 1888ms, the error 1897ms, pagehide
        // 1918ms -- which is why doing this at `pagehide` changed nothing.
        //
        // And a SENTINEL rather than a real abort(). All the extension
        // reads is `signal.aborted`, so saying "this break is expected" is
        // enough, and it leaves the connection alone: an abort landing
        // inside a reconnect's own fetch is read by the loop as `break`,
        // which closes the stream for good -- and at `beforeunload` there
        // is no telling whether the page is going away or into the
        // back/forward cache, where that stream is what catches it up. The
        // loop assigns a real controller on its next attempt, so this
        // clears itself and a genuine error still reports.
        const EXPECTED_BREAK = { signal: { aborted: true }, abort() {} };
        window.addEventListener("beforeunload", () => {
            // A private of the vendored bundle, reached on purpose: it is
            // the one thing that catch looks at. See src/vendor/README.md.
            const sse = document.getElementById("library-events")?._htmx?.sse;
            if (sse) sse.abortController = EXPECTED_BREAK;
        });

        window.addEventListener("pageshow", (event) => {
            if (!event.persisted) return;
            // Every snapshot taken BEFORE this freeze is now suspect, and
            // bumping the counter is what says so: the guard above re-syncs
            // any of them until a fresh one is taken.
            //
            // The two halves keep different company. The snapshots live in
            // sessionStorage and outlive the document; the counter lives on
            // `window` and does not -- so without this, a document coming
            // back out of the back/forward cache reads as "nothing has
            // happened" against snapshots written before it went away, and
            // an arbitrary amount of reading happened in between.
            //
            // Measured, home -> series (boosted) -> reader -> Back -> Back
            // with no pause between the two: the second Back was served
            // the cached home from before the reading, with zero requests.
            window.libraryEventsSeen = (window.libraryEventsSeen || 0) + 1;
            const stream = document.getElementById("library-events");
            // No stream on this page means nothing will ever catch it up.
            if (!stream) {
                resync();
                return;
            }
            const fallback = setTimeout(resync, RESUME_GRACE_MS);
            stream.addEventListener("htmx:sse:after:connection",
                                    () => clearTimeout(fallback), { once: true });
        });
    }

    // No capability guard any more. The stream is htmx's hx-sse extension
    // now, which reads a fetch() body rather than constructing an
    // EventSource -- and every browser that can run htmx 4 (itself built on
    // fetch) can do that. What used to be here tested window.EventSource,
    // which after the move would have been a check on something this file
    // no longer touches: true everywhere, and true for the wrong reason.
    // Without the element below there is simply no stream, which is the
    // same graceful degradation by a shorter route.

    // Looked up when it is SHOWN, not captured here. The handlers at the
    // foot of this file are bound once, to an element that survives a
    // whole-<body> swap (hx-preserve, see base.html) -- so a reference
    // taken now would keep pointing at the banner of whatever page this
    // file first ran on, which that swap has since detached. The banner
    // would go on being un-hidden where nobody can see it.
    function bannerElement() {
        return document.getElementById("library-update-banner");
    }

    function showBanner(text) {
        const banner = bannerElement();
        if (!banner) return;
        document.getElementById("library-update-banner-text").textContent = text;
        banner.classList.remove("hidden");
    }

    // These two are the other way round: the buttons come and go WITH the
    // banner, and this file runs again for each new one, so binding to the
    // current pair here is exactly right.
    const bannerReload = document.getElementById("library-update-banner-reload");
    const bannerDismiss = document.getElementById("library-update-banner-dismiss");
    if (bannerReload) bannerReload.addEventListener("click", () => window.location.reload());
    if (bannerDismiss) {
        bannerDismiss.addEventListener("click", () => bannerElement().classList.add("hidden"));
    }

    function fadeOutAndRemove(el) {
        if (!el) return;
        el.style.transition = "opacity 0.4s ease";
        el.style.opacity = "0";
        setTimeout(() => el.remove(), 400);
    }

    // The series page currently open, if that's the screen we're on.
    // #series-page is an invisible marker rendered by series.html.
    function openSeriesId() {
        const marker = document.getElementById("series-page");
        return marker ? marker.dataset.seriesId : null;
    }

    // Rebuilds one home-page series card from the server's own card data,
    // so its cover and item count reflect the change. Returns a promise
    // that resolves true when the card was actually refreshed.
    // The home page's grid, while it is on screen. home.js leaves
    // `window.libraryGrid` behind when a boosted visit takes the page away
    // (nothing tears it down), and every event then acted on the detached
    // grid: a card fetch per mark-read on any page, and no "new content"
    // banner where one belonged -- measured on /stats after visiting home.
    function homeGrid() {
        const grid = window.libraryGrid;
        return grid && grid.isLive() ? grid : null;
    }

    function refreshCard(kind, id) {
        if (!homeGrid()?.hasCard(kind, id)) {
            return Promise.resolve(false);
        }
        return fetch(`/library/${kind}/${id}/card`)
            .then((r) => (r.ok ? r.json() : null))
            .then((card) => {
                if (!card) return false;
                return homeGrid()?.replaceCard(kind, id, buildCardElement(card)) || false;
            })
            .catch(() => false);
    }

    function refreshSeriesCard(seriesId) {
        return refreshCard("series", seriesId);
    }

    // Re-renders the open series page's item grid from the server, which
    // is what makes an added/removed item appear or disappear in its
    // correct place. The grid partial resolves natural/manual ordering and
    // pagination server-side (see series_items_grid), so nothing about
    // that ordering has to be duplicated here.
    let seriesGridRequest = 0;
    function refreshOpenSeriesGrid(seriesId) {
        const grid = document.getElementById("items-grid");
        if (!grid) return Promise.resolve(false);
        const request = ++seriesGridRequest;
        return fetch(`/series/${seriesId}/items-grid`)
            .then((r) => (r.ok ? r.text() : null))
            .then((html) => {
                if (!html) return false;
                // Still the same grid? A navigation could have happened
                // while this was in flight.
                const current = document.getElementById("items-grid");
                if (current !== grid || openSeriesId() !== String(seriesId)
                    || request !== seriesGridRequest) return false;
                current.outerHTML = html;
                wireHtmx(document.getElementById("items-grid"));
                // The replacement comes from the server unfiltered; if a
                // search is active it has to be re-applied, or the new grid
                // shows items the box says are filtered out.
                if (window.seriesSearch) window.seriesSearch.applyFilter();
                if (window.seriesSort) window.seriesSort.applySort();
                return true;
            })
            .catch(() => false);
    }

    // A card or strip entry for an item -- never a reader. #reader-root
    // carries data-item-id too, and a web novel's reader puts the NOVEL's
    // id there: deleting comic 1's file faded out and removed the open
    // reader of novel 1 (measured in both browsers).
    const itemSelector = (itemId) => `[data-item-id="${itemId}"]:not(#reader-root)`;

    function handleItemRemoved(itemId, seriesId) {
        document.querySelectorAll(itemSelector(itemId)).forEach(fadeOutAndRemove);
        // On the home page there is no per-item element to fade -- what
        // changes there is the owning series' cover and item count.
        // Coalesced for the same reason as the added side: deleting a
        // folder emits one event per file, and only the final state of the
        // card is worth a request.
        if (seriesId) scheduleRefresh(`card:${seriesId}`, () => refreshSeriesCard(seriesId));
    }

    // Importing a series delivers one item_added per file, back to back.
    // Acting on each one would refetch the same card (or the whole items
    // grid) a dozen times for one visible change, so they are coalesced:
    // the last event of a burst wins and one request is made. Long enough
    // to absorb a burst, short enough that a single added file still feels
    // immediate.
    const REFRESH_DEBOUNCE_MS = 500;
    const refreshTimers = new Map();

    function scheduleRefresh(key, run) {
        clearTimeout(refreshTimers.get(key));
        refreshTimers.set(key, setTimeout(() => {
            refreshTimers.delete(key);
            run();
        }, REFRESH_DEBOUNCE_MS));
    }

    function handleItemAdded(seriesId) {
        // The series page for this very series is open -- re-render its
        // grid so the new item shows up in place.
        if (seriesId && String(openSeriesId()) === String(seriesId)) {
            scheduleRefresh(`grid:${seriesId}`, () => {
                refreshOpenSeriesGrid(seriesId).then((done) => {
                    if (!done) showBanner("New content detected in the library.");
                });
            });
            return;
        }
        // The home page is open -- update the owning series' card. If that
        // card is still being inserted by handleSeriesAdded (the very
        // first item of a brand-new series), wait for the insertion first,
        // otherwise this would find no card and fall back to the banner
        // for a series that is about to appear.
        if (seriesId && homeGrid()) {
            scheduleRefresh(`card:${seriesId}`, () => {
                Promise.resolve(pendingSeriesCards.get(String(seriesId)))
                    .then(() => refreshSeriesCard(seriesId))
                    .then((done) => {
                        // The series isn't on screen at all (a standalone
                        // item, or a card that hasn't been rendered) --
                        // the banner is still the only honest thing to
                        // show.
                        if (!done) showBanner("New content detected in the library.");
                    });
            });
            return;
        }
        showBanner("New content detected in the library.");
    }

    // An existing file changed on disk (a new cover, a different embedded
    // title). Unlike item_added there is nothing to announce: if the
    // affected screen isn't open, the change will simply be there next
    // time it is. So this redraws what is on screen and is otherwise
    // silent -- no banner fallback.
    function handleItemUpdated(seriesId) {
        if (!seriesId) return;
        if (String(openSeriesId()) === String(seriesId)) {
            scheduleRefresh(`grid:${seriesId}`, () => refreshOpenSeriesGrid(seriesId));
            return;
        }
        scheduleRefresh(`card:${seriesId}`, () => refreshSeriesCard(seriesId));
    }

    // The series' own name or cover was edited by hand. Only the home
    // page's card shows either, and the page where the edit happened has
    // already been re-rendered by the redirect -- so this exists for the
    // OTHER screen (another tab, another device) still showing the old one.
    function handleSeriesUpdated(seriesId) {
        if (!seriesId) return;
        scheduleRefresh(`card:${seriesId}`, () => refreshSeriesCard(seriesId));
        // Its volumes in the "Reading" strip and the item search wear the
        // series' blurred cover too, so they are re-rendered with it --
        // only when one of them is on screen. A cover somebody just asked
        // to hide must not stay sharp until a reload.
        const own = `[data-series-id="${CSS.escape(String(seriesId))}"]`;
        if (document.querySelector(`#reading-grid ${own}`)) {
            scheduleRefresh("reading-grid", refreshReadingGrid);
        }
        const search = document.getElementById("series-search");
        if (search && window.htmx && document.querySelector(`#search-items ${own}`)) {
            window.htmx.trigger(search, "search");
        }
    }

    // ---- Reading progress ----
    // The percentage arrives in the event itself, so the common case (the
    // same book, a bit further along) is a width and a label, with no
    // request. Only a change of STATUS goes back to the server, because
    // what it changes is server-rendered markup: which entries belong in
    // the "Reading" section, and whether a card shows its Read badge.

    // Same rule as format_duration in app/template_filters.py, which is
    // what rendered this text server-side. The two have to agree, or a
    // card would visibly change format the first time it updated live.
    function formatDuration(seconds) {
        if (!(seconds > 0)) return "0s";
        if (seconds < 60) return `${Math.round(seconds)}s`;
        const minutes = seconds / 60;
        if (minutes < 60) return `${Math.round(minutes)}m`;
        const total = Math.round(minutes);
        const hours = Math.floor(total / 60);
        const rest = total % 60;
        return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`;
    }

    // The estimate is time LEFT, so it has to move with the percentage.
    // data-eta-total holds the projected time for the whole book, which is
    // exactly what makes that possible without asking the server: the
    // server's own estimate is that total times the fraction still to go
    // (see app/stats/estimates.py).
    //
    // For a web novel the estimate covers the VOLUME while the percentage
    // is still the CHAPTER's, so only part of it moves: data-eta-rest is
    // the volume beyond this chapter, which stays put until the reader
    // crosses out of it -- and that arrives as a new chapter_id, which
    // re-fetches the card from the server anyway. Zero everywhere else,
    // so this is the same arithmetic it always was.
    //
    // data-eta-scope names what is being counted ("volume 3"), because the
    // percentage beside it counts something narrower. The string is built
    // the same way here and in _reading_grid.html, for the same reason
    // formatDuration matches the server's filter: a card must not change
    // its wording the first time it updates live.
    // Text ETA counts the actual words after a saved anchor. New samples may
    // also change the learned pace, so neither number can be reconstructed
    // from the progress percentage retained by an already-open card.
    let textEstimatesBusy = false, textEstimatesDirty = false, textEstimatesGeneration = 0;
    function textEstimateKey(target) {
        return `${target.kind}:${target.id}:${target.volume_id || ""}`;
    }
    function scheduleTextEstimates() {
        textEstimatesGeneration++;
        textEstimatesDirty = true;
        scheduleRefresh("text-estimates", refreshTextEstimates);
    }
    async function refreshTextEstimates() {
        if (textEstimatesBusy) return;
        textEstimatesBusy = true;
        textEstimatesDirty = false;
        const generation = textEstimatesGeneration;
        const nodes = [...document.querySelectorAll("[data-text-eta-kind]")];
        const targets = new Map();
        for (const node of nodes) {
            const target = {kind: node.dataset.textEtaKind, id: Number(node.dataset.textEtaId)};
            if (node.dataset.textEtaVolume) target.volume_id = Number(node.dataset.textEtaVolume);
            targets.set(textEstimateKey(target), target);
        }
        try {
            const selected = [...targets.values()];
            for (let start = 0; start < selected.length; start += 100) {
                const response = await fetch("/library/reading-estimates", {
                    method: "POST", headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({targets: selected.slice(start, start + 100)}),
                });
                if (!response.ok) return false;
                const data = await response.json();
                // A newer progress/sample event invalidates this response even
                // if it arrives before the replacement request can start.
                if (generation !== textEstimatesGeneration) return false;
                const results = new Map((data.estimates || []).map(value => [value.key, value]));
                for (const node of nodes) {
                    if (!node.isConnected) continue;
                    const key = textEstimateKey({kind: node.dataset.textEtaKind,
                        id: Number(node.dataset.textEtaId), volume_id: Number(node.dataset.textEtaVolume) || undefined});
                    const value = results.get(key);
                    if (!value) continue;
                    node.hidden = value.remaining === null && !value.collecting;
                    const scope = node.dataset.etaScope ? ` in ${node.dataset.etaScope}` : "";
                    node.textContent = value.remaining === null ? "Collecting data" : `~${formatDuration(value.remaining)}`;
                    node.title = value.remaining === null ? "Collecting valid reading samples"
                        : `About ${formatDuration(value.remaining)} left${scope}, based on measured reading pace`;
                    const card = node.closest("[data-item-id]");
                    if (card) {
                        if (value.total === null) delete card.dataset.eta;
                        else card.dataset.eta = String(Math.round(value.total));
                    }
                }
            }
            return true;
        } catch { return false; }
        finally {
            textEstimatesBusy = false;
            if (textEstimatesDirty) scheduleRefresh("text-estimates", refreshTextEstimates);
        }
    }

    function applyEta(el, percent) {
        const eta = el.querySelector(".reading-eta");
        if (!eta || eta.dataset.textEtaKind) return;
        const total = parseFloat(eta.dataset.etaTotal) || 0;
        const rest = parseFloat(eta.dataset.etaRest) || 0;
        if (!(total > 0) && !(rest > 0)) return;
        const remaining = rest + total * (1 - Math.min(100, Math.max(0, percent)) / 100);
        const scope = eta.dataset.etaScope ? ` in ${eta.dataset.etaScope}` : "";
        eta.textContent = `~${formatDuration(remaining)}`;
        eta.title = `About ${formatDuration(remaining)} left${scope}, going by how fast you read`;
    }

    function applyProgress(el, percent, pagesRemaining) {
        const fill = el.querySelector(".progress-fill");
        if (fill) fill.style.width = `${percent}%`;
        const label = el.querySelector(".progress-label");
        if (label) label.textContent = `${percent}%`;
        const remaining = el.querySelector(".progress-remaining");
        if (remaining) {
            remaining.textContent =
                pagesRemaining === null || pagesRemaining === undefined
                    ? ""
                    : ` · ${pagesRemaining} page${pagesRemaining === 1 ? "" : "s"} left`;
        }
        applyEta(el, percent);
    }

    // Re-renders the home page's "Reading" section. The section stays in
    // the DOM even when empty (see library/index.html), so a book started
    // on another device has somewhere to appear.
    // Markup swapped in by this file is inserted with outerHTML, which
    // htmx knows nothing about -- it wires hx-* attributes when IT does the
    // swapping, or once at load. A grid replaced here therefore came back
    // with dead buttons: every hx-post inside it fell through to the plain
    // method/action the same form carries, so a click that should have
    // swapped one card reloaded the whole page instead. Silent, because
    // the fallback works; it just works the slow way.
    function wireHtmx(el) {
        if (el && window.htmx) window.htmx.process(el);
    }

    function refreshReadingGrid() {
        const section = document.getElementById("reading-section");
        if (!section) return Promise.resolve(false);
        return fetch("/library/reading-grid")
            .then((r) => (r.ok ? r.text() : null))
            .then((html) => {
                if (!html) return false;
                const grid = document.getElementById("reading-grid");
                if (!grid) return false;
                grid.outerHTML = html;
                const updated = document.getElementById("reading-grid");
                wireHtmx(updated);
                section.classList.toggle("hidden", !updated || updated.children.length === 0);
                return true;
            })
            .catch(() => false);
    }

    // The "Reading" strip is ordered most-recently-read first (see
    // _reading_items' final sort), and BOTH writers behind a progress event
    // set that very column to now: reading_progress.last_read_at for an
    // item, novel_reading_progress.updated_at for a novel. So the arrival
    // of the event is itself the new timestamp, and moving its card to the
    // front reproduces the server's ORDER BY exactly -- no date has to
    // travel in the event, and no round trip has to be paid for an order
    // the client can already derive.
    //
    // Without it the strip silently stopped being what it says it is: read
    // something on the phone while the laptop's home page is open and its
    // bar widened in place, third in a list whose whole promise is that the
    // first card is where you were last.
    //
    // One limit, measured: those columns are `datetime('now')`, so two
    // saves inside the SAME SECOND tie and the server has nothing to order
    // them by. This promotion is exact where the server is not, so a
    // refetch can put such a pair back the other way round. It takes
    // finishing with two different books in one second to reach, and the
    // alternative -- sub-second timestamps -- changes a stored format the
    // whole schema shares, for that.
    function promoteInReadingGrid(selector) {
        const grid = document.getElementById("reading-grid");
        if (!grid) return;
        const card = grid.querySelector(selector);
        // The card is the <a>, but the grid's child is the wrapper that
        // also holds the clear button -- moving the link alone would leave
        // the ✕ behind on another card.
        const entry = card && card.closest("#reading-grid > *");
        if (entry && entry !== grid.firstElementChild) grid.prepend(entry);
    }

    function refreshReadingSectionIfMissing(selector, statusChanged) {
        const section = document.getElementById("reading-section");
        if (!section) return;
        if (statusChanged || !section.querySelector(selector)) {
            scheduleRefresh("reading-grid", refreshReadingGrid);
        }
    }

    function handleProgressUpdated(data) {
        scheduleTextEstimates();
        const selector = itemSelector(data.item_id);
        let statusChanged = false;
        document.querySelectorAll(selector).forEach((el) => {
            if ((el.dataset.progressStatus || "") !== (data.status || "")) {
                statusChanged = true;
                return;
            }
            applyProgress(el, data.percent, data.pages_remaining);
        });

        refreshReadingSectionIfMissing(selector, statusChanged);
        // Only when the status held: a status change is already re-fetching
        // the whole section, and promoting a card that is about to leave it
        // (just finished elsewhere) would flash it to the front first.
        if (!statusChanged) promoteInReadingGrid(selector);

        // The series page's card carries a Read badge and a mark-read
        // toggle whose action flips with the status -- markup, not a
        // number, so the server renders it.
        if (statusChanged && data.series_id && String(openSeriesId()) === String(data.series_id)) {
            scheduleRefresh(`grid:${data.series_id}`, () => refreshOpenSeriesGrid(data.series_id));
        }
        // The home grid's card for that series counts what is UNREAD and
        // times it, so finishing something changes both numbers on it.
        //
        // Keyed off the status in the EVENT, not off `statusChanged`: that
        // flag needs an element on screen to disagree with, and an item
        // marked read from a series page is one the home page may be
        // showing nothing for at all -- not in the strip, because it was
        // never being read. The badge moves in whole items, so what has to
        // be caught is an item crossing the completed line, and that is
        // exactly the event whose status is not 'reading'. A percentage
        // arriving every few seconds while reading carries 'reading' and
        // costs nothing; between two crossings the time is behind by at
        // most what is left of the item in hand, which is the granularity
        // the number beside it counts in anyway.
        if (data.series_id && (data.status || "") !== "reading") {
            scheduleRefresh(`card:${data.series_id}`, () => refreshSeriesCard(data.series_id));
        }
    }

    // "Mark all read/unread" on a series arrives as one event for the
    // whole series rather than one per item (see library/router.py), so
    // there is no per-item status to compare -- everything it can affect
    // is re-rendered.
    function handleSeriesProgressUpdated(seriesId) {
        if (seriesId && String(openSeriesId()) === String(seriesId)) {
            scheduleRefresh(`grid:${seriesId}`, () => refreshOpenSeriesGrid(seriesId));
        }
        if (document.getElementById("reading-section")) {
            scheduleRefresh("reading-grid", refreshReadingGrid);
        }
        // "Mark all read" empties the unread badge on the home card and
        // takes the time under it away with it -- the largest single change
        // either number can make, and the one most obviously wrong to leave
        // on screen.
        if (seriesId) scheduleRefresh(`card:${seriesId}`, () => refreshSeriesCard(seriesId));
    }

    // Setting a novel's own status (completed, dropped, ...) is what takes
    // it out of the "Reading" section, or puts it back -- its saved
    // position never changes here, so there is nothing else to update.
    function handleNovelStatusUpdated(data) {
        if (document.getElementById("reading-section")) {
            scheduleRefresh("reading-grid", refreshReadingGrid);
        }
        // The status is also a badge on the novel's own card in the grid,
        // and something the header's filter and the hidden-statuses
        // setting act on -- so the card is rebuilt from the server, which
        // is what makes it change colour, or disappear, without a reload.
        // Refetched rather than patched from the event: the status in it
        // belongs to the reader who made the change, and this event
        // reaches every connected browser.
        if (data && data.novel_id) {
            scheduleRefresh(`novel-card:${data.novel_id}`,
                            () => refreshCard("novel", data.novel_id));
        }
    }

    // A novel has no per-item read status (a saved position IS "reading"),
    // so there is never a status change to chase -- only the number, and
    // the first save, which is what puts the novel in the section.
    function handleNovelProgressUpdated(data) {
        scheduleTextEstimates();
        const selector = `[data-novel-id="${data.novel_id}"]`;
        const cards = document.querySelectorAll(selector);
        // The percentage in the event is of the CHAPTER, so a card still
        // showing the previous chapter would render it against the wrong
        // one -- and the chapter's name and its time estimate are
        // server-rendered anyway. Same rule as data-progress-status for
        // items: carry the value when the markup is unchanged, pay for a
        // refetch only when it isn't.
        let chapterChanged = false;
        cards.forEach((el) => {
            if (el.dataset.chapterId !== undefined && data.chapter_id !== undefined && el.dataset.chapterId !== String(data.chapter_id)) {
                chapterChanged = true;
            } else {
                applyProgress(el, data.percent, null);
            }
        });
        // Through the same helper the item path uses, so the refetch goes
        // through scheduleRefresh and a burst of chapter crossings costs
        // one request rather than one each.
        refreshReadingSectionIfMissing(selector, chapterChanged);
        if (!chapterChanged) promoteInReadingGrid(selector);
        // Crossing into another chapter is exactly when the one behind is
        // marked read (see reader-text.js's onChapterAdvance), so it is
        // also when the home card's unread count drops by one. Same rule as
        // the item path above: the badge counts whole chapters, so only the
        // event that can change one pays for a refetch.
        if (chapterChanged && data.novel_id) {
            scheduleRefresh(`novel-card:${data.novel_id}`,
                            () => refreshCard("novel", data.novel_id));
        }
    }

    function handleSeriesRemoved(seriesId) {
        // Tell home.js's own `cards` array about the removal first -- it
        // drives sort/filter (including re-appending cards to the grid on
        // every sort), and a card it doesn't know is gone gets silently
        // re-inserted into the DOM the next time applySort() runs.
        homeGrid()?.removeCard("series", seriesId);

        // the series card on the home page (if that's the screen that's open)
        document.querySelectorAll(`.series-card[data-series-id="${seriesId}"]`).forEach(fadeOutAndRemove);

        // the series page itself, if that's what's currently open --
        // #series-page is just an invisible marker (see series.html), it
        // doesn't fade away on its own like the cards do
        const seriesPageMarker = document.getElementById("series-page");
        if (seriesPageMarker && seriesPageMarker.dataset.seriesId === String(seriesId)) {
            showBanner("This series was removed from the library. Returning to the library…");
            setTimeout(() => { window.location.href = "/"; }, 2500);
        }
    }

    // Same map as the badge_bg one in library/index.html, duplicated here
    // because the card markup below is built in JS rather than Jinja for a
    // series that arrives live via SSE. The class names have to stay
    // literal: app.css is a frozen stylesheet of the utility classes
    // actually in use (see AGENTS.md), so a class assembled at runtime
    // from pieces would have no rule behind it.
    const BADGE_BG = {
        reading: "bg-indigo-600",
        completed: "bg-green-600",
        dropped: "bg-red-600",
        stopped_reading: "bg-amber-600",
        on_hold: "bg-neutral-600",
        plan_to_read: "bg-blue-600",
    };

    // Same rule, same reason -- and a separate map because the two
    // vocabularies are separate (see app/status_labels.py).
    const PUBLICATION_BADGE_BG = {
        ongoing: "bg-teal-300",
        completed: "bg-violet-300",
        hiatus: "bg-yellow-300",
        canceled: "bg-rose-300",
    };

    // One builder for both kinds, because the server sends one shape for
    // both (see _series_card/_novel_card): a card that arrives live must
    // not look different from one the server rendered, and two builders
    // is how that stops being true.
    function buildCardElement(card) {
        const a = document.createElement("a");
        a.id = `library-card-${card.kind}-${card.id}`;
        a.setAttribute("hx-preload", "home-card-press");
        a.href = card.detail_url;
        a.className = "series-card block group";
        // Only a real series keeps data-series-id -- it is what the
        // live-removal handler matches on. A novel carries the other one.
        if (card.kind === "novel") a.dataset.novelId = String(card.id);
        else a.dataset.seriesId = String(card.id);
        a.dataset.name = card.name.toLowerCase();
        a.dataset.status = card.status || "";
        a.dataset.publication = card.publication_status || ""; // what the header's publication filter matches on
        a.dataset.added = card.added_at || "";
        a.dataset.modified = card.modified_at || "";
        a.dataset.lastRead = card.last_read_at || "";
        a.dataset.library = String(card.library_id); // matched against the library tabs, see home.js
        a.dataset.unreadCount = String(card.unread_count || 0); // what the "Unread" sort orders by

        const thumb = document.createElement("div");
        thumb.className = "cover";

        if (card.cover_url) {
            const img = document.createElement("img");
            // Set the policy before src: offscreen live cards defer like the HTML cards.
            img.loading = "lazy";
            img.src = card.cover_url;
            // Same class the server puts there for a blurred series (see
            // library/index.html) -- a card refetched after the checkbox
            // was ticked comes back through here.
            img.className = "w-full h-full object-cover" + (card.blur_cover ? " cover-blur" : "");
            thumb.appendChild(img);
        }

        // One row holding both badges -- the work's status on the left,
        // the reader's on the right -- and an empty slot in place of a
        // missing publication status so justify-between keeps a lone
        // tracking badge on the right. Same structure and same reasons as
        // library/index.html; a card that arrives live must not look
        // different from one the server rendered.
        if (card.publication_status || card.status) {
            const row = document.createElement("div");
            row.className = "absolute top-1 left-1 right-1 flex flex-wrap items-start justify-between gap-1";

            const publication = document.createElement("span");
            if (card.publication_status) {
                publication.className = `${PUBLICATION_BADGE_BG[card.publication_status] || "bg-neutral-300"} text-neutral-900 text-xs rounded px-1.5 py-0.5`;
                publication.textContent = card.publication_label || card.publication_status;
            }
            row.appendChild(publication);

            if (card.status) {
                const badge = document.createElement("span");
                badge.className = `${BADGE_BG[card.status] || "bg-neutral-600"} text-xs rounded px-1.5 py-0.5`;
                badge.textContent = card.status_label || card.status;
                row.appendChild(badge);
            }
            thumb.appendChild(row);
        }

        // What is left to read, not what the shelf holds -- and absent at
        // zero, which is what a finished series looks like. Same rule and
        // same markup as library/index.html.
        if (card.unread_count) {
            const countBadge = document.createElement("span");
            countBadge.className = "absolute bottom-1 right-1 bg-black/70 text-white text-xs rounded px-1.5 py-0.5";
            countBadge.textContent = String(card.unread_count);
            thumb.appendChild(countBadge);
        }

        // The tracking dot, drawn for every card including an unlinked one
        // -- finding what still needs linking is the whole point of it, so
        // "no dot" would answer the opposite question. Class names written
        // out literally, like the badge maps above: the frozen stylesheet
        // is scanned for them, and one assembled from a variable is a rule
        // that never existed. A new series is linked to nothing, so this
        // is grey in practice; it is computed rather than hardcoded
        // because the same builder redraws a card that already exists.
        const dot = document.createElement("span");
        const linked = card.tracked_count || 0;
        const total = card.tracker_total || 0;
        dot.className = "track-dot " + (
            total && linked >= total ? "text-emerald-300"
            : linked ? "text-sky-300"
            : "text-neutral-500");
        dot.title = linked
            ? `Tracking: linked to ${linked} of ${total} services`
            : "Tracking: not linked";
        thumb.appendChild(dot);

        const title = document.createElement("p");
        title.title = card.name;
        title.className = "card-title";
        title.textContent = card.name; // textContent, not innerHTML -- series names come from filenames on disk

        a.appendChild(thumb);
        a.appendChild(title);

        // How long the badge above is worth. Seconds travel rather than the
        // formatted string, the same way data-eta-total does on the
        // "Reading" strip -- formatDuration is the server's filter written
        // out again here (and a test compares the two), so a card that
        // arrives live reads exactly like one the server rendered.
        //
        // Null when nothing about this card can be estimated, which is a
        // different answer from zero: no line at all, rather than "~0s".
        // Deliberately NOT given the .reading-eta class the strip's card
        // carries -- that is the hook applyEta rewrites from a progress
        // event, using the CHAPTER percentage in it, and a novel is in both
        // grids under the same data-novel-id.
        if (card.remaining_seconds || card.text_eta) {
            const eta = document.createElement("p");
            eta.className = "text-xs text-neutral-500 truncate";
            const left = formatDuration(card.remaining_seconds);
            eta.textContent = card.remaining_seconds === null ? "Collecting data" : `~${left}`;
            eta.title = card.remaining_seconds === null ? "Collecting valid reading samples"
                : `About ${left} of this left to read, going by how fast you read`;
            if (card.text_eta) {
                eta.dataset.textEtaKind = card.kind;
                eta.dataset.textEtaId = String(card.id);
            }
            a.appendChild(eta);
        }
        return a;
    }

    // A newly created series arrives as a `series_added` event immediately
    // followed by an `item_added` for its first item, then one more for
    // every other file in it (see scanner.py). The card insertion below is
    // a fetch, so those item_added events land while it is still in
    // flight; this map lets handleItemAdded wait for it instead of finding
    // no card and falling back to the banner.
    //
    // This replaced a blanket "ignore item_added for this series for 10
    // seconds", which is what made the card freeze at whatever the series
    // held the moment it was created -- reproduced in a real browser: a
    // 12-volume series imported live showed a count badge of 1, and the
    // cover of whichever file the scanner happened to process FIRST rather
    // than the first by ordering. Only a reload fixed either, because the
    // events that would have corrected them were being dropped on purpose.
    const pendingSeriesCards = new Map();

    function handleSeriesAdded(seriesId) {
        // No grid on this page (not the home screen) -- nothing to insert
        // into, fall back to the discreet banner like item_added does.
        if (!homeGrid()) {
            showBanner("New content detected in the library.");
            return;
        }
        if (homeGrid().hasCard("series", seriesId)) return; // already on screen

        const key = String(seriesId);
        const inserting = fetch(`/library/series/${seriesId}/card`)
            .then((r) => (r.ok ? r.json() : null))
            .then((card) => {
                if (!card) return;
                // Always inserted regardless of the library tab
                // selected -- that filter is a client-side show/hide
                // (see home.js) applied to every card already in the DOM,
                // not a server-side restriction, so a card for a
                // currently-hidden library still needs to be here (just
                // hidden) for when the user switches back to it.
                homeGrid()?.addCard(buildCardElement(card));
            })
            .catch(() => {
                // Request failed -- fall back to the banner rather than
                // silently doing nothing.
                showBanner("New content detected in the library.");
            })
            .finally(() => pendingSeriesCards.delete(key));
        pendingSeriesCards.set(key, inserting);
    }

    // ---------- Scan progress (settings page) ----------
    // Only the settings page renders these rows; everywhere else the
    // lookup below finds nothing and this is inert, same as every other
    // handler in this file.

    // One renderer for every background job -- a library scan and the
    // whole-collection renumber report the same shape (total, processed,
    // what is being worked on right now), so the only thing that differs
    // is the word for a unit, which the payload carries. A page draws
    // whichever jobs it has markup for: the row is matched by
    // data-job-key, and a job with no row on this page is simply ignored.
    function renderJobProgress(job) {
        const row = document.querySelector(`.job-row[data-job-key="${job.job_key}"]`);
        if (!row) return;

        // Rows (and the section holding them) start hidden: there is
        // nothing to say until a job actually reports something.
        const section = row.closest("[data-job-section]");
        if (section) section.classList.remove("hidden");
        row.classList.remove("hidden");

        const total = job.total || 0;
        const processed = job.processed || 0;
        const percent = total ? Math.round((processed / total) * 100) : (job.running ? 0 : 100);
        const unit = job.unit || "items";
        const one = unit.replace(/s$/, "");

        row.querySelector(".job-bar").style.width = `${percent}%`;
        row.querySelector(".job-counts").textContent = job.running
            ? `${processed} / ${total} ${unit} (${percent}%)`
            : (total ? `Finished — ${total} ${total === 1 ? one : unit}` : "Finished");
        // textContent, not innerHTML -- this is a file name straight off
        // the user's disk, or a series name derived from one.
        row.querySelector(".job-current").textContent =
            job.running && job.current ? job.current : "";
    }

    // A job may already be running when this page opens; SSE only
    // delivers what happens next, so ask for the current state once.
    if (document.querySelector(".job-row")) {
        fetch("/library/job-status")
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => {
                if (data && data.jobs) data.jobs.forEach(renderJobProgress);
            })
            .catch(() => {
                // No initial state -- live events will still populate this
                // the moment a job reports anything.
            });
    }

    // The stream itself belongs to htmx's hx-sse extension: #library-events
    // in base.html carries hx-sse:connect, the extension opens it, and each
    // named message arrives here as a DOM event on that element with the
    // raw frame in `detail.data`.
    //
    // What the extension is here for is the two things a native EventSource
    // has no answer for: reconnect with exponential backoff and jitter
    // instead of one fixed interval, and dropping the connection entirely
    // while the tab is in the background (it reopens on return). The second
    // one is why MAX_PENDING_PER_STREAM exists on the server -- a phone that
    // slept with the tab open used to hold a stream that queued events
    // nobody was reading.
    //
    // The extension keeps Last-Event-ID across reconnects and sends it,
    // which is what lets the server say whether this page missed anything
    // (events_gap below) -- see the stream in library/router.py.
    //
    // Nothing is dispatched before these listeners exist, and that is NOT
    // free: htmx may connect the stream before this deferred file runs (it
    // initializes on a timer once its own deferred script has run). An
    // inline script beside the element in base.html holds every message
    // until `libraryEventsRelease` below.
    const handlers = {
        item_removed: (data) => handleItemRemoved(data.item_id, data.series_id),
        series_removed: (data) => handleSeriesRemoved(data.series_id),
        series_added: (data) => handleSeriesAdded(data.series_id),
        item_added: (data) => handleItemAdded(data.series_id),
        // A novel card whose own data changed -- today that is a tracker
        // link being made or dropped somewhere else. Coalesced like the
        // series twin, since two of them can land together.
        novel_updated: (data) => {
            if (data.novel_id) {
                scheduleRefresh(`novel-card:${data.novel_id}`,
                                () => refreshCard("novel", data.novel_id));
            }
        },
        series_updated: (data) => handleSeriesUpdated(data.series_id),
        item_updated: (data) => handleItemUpdated(data.series_id),
        reading_estimate_updated: () => scheduleTextEstimates(),
        progress_updated: handleProgressUpdated,
        novel_progress_updated: handleNovelProgressUpdated,
        series_progress_updated: (data) => handleSeriesProgressUpdated(data.series_id),
        novel_status_updated: handleNovelStatusUpdated,
        job_progress: renderJobProgress,
        // Handled by the shared event counter/preload invalidation below.
        account_updated: () => {},
        // The page is behind: its stream (re)connected after events it never
        // saw -- a backgrounded tab, a restore, a server restart, a page the
        // browser served from its own cache -- or fell too far behind to be
        // kept. Nothing missed is replayed, so the screen is re-rendered
        // from the server, the only thing that knows the current state --
        // morphed into place rather than reloaded (see resync above).
        //
        // Cannot loop: the gap is followed by the server's position, so
        // the next connection from this stream is current.
        events_gap: () => window.libraryResync(),
    };

    // Bound once per ELEMENT rather than per run of this file. The two
    // come apart under a whole-<body> swap: `hx-preserve` moves this very
    // element into the new page while this file is executed again, so
    // every navigation would add a second copy of all thirteen handlers
    // to the same element and one event would be handled once per page
    // visited (measured: 14 listeners on the first page, 53 after three
    // boosted navigations). On a full load the element is new and carries
    // no flag, which is the normal path and is unaffected.
    const stream = document.getElementById("library-events");
    if (stream && !stream.libraryEventsBound) {
        stream.libraryEventsBound = true;
        document.addEventListener("htmx:finally:swap", scheduleTextEstimates);
        scheduleTextEstimates();
        for (const [name, handle] of Object.entries(handlers)) {
            stream.addEventListener(name, (event) => {
                let data;
                try {
                    data = JSON.parse(event.detail.data);
                } catch (err) {
                    return; // not a payload this knows how to read
                }
                // Counted before it is handled, and counted for EVERY
                // event rather than only the ones that changed something
                // on this page: what the history cache needs to know is
                // that the library moved, and a handler that found
                // nothing to patch is exactly the case where the page it
                // would have patched is the one off screen. See the
                // cache:hit listener above.
                window.libraryEventsSeen = (window.libraryEventsSeen || 0) + 1;
                window.homePreload?.invalidate();
                handle(data);
            });
        }
        stream.libraryEventsRelease?.();
    }
})();
