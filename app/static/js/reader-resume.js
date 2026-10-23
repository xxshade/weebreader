/** Catch a restored or newly visible reader up to another device.
 * Text readers compare section/block/character positions, so even a one-word
 * advance matters. Comics and legacy clients retain percentage comparison.
 * History snapshots seed their own displayed position; a live document uses
 * its last save. Only a strictly newer server position triggers replacement.
 */
(() => {
    if (window.readerResume) return;  // once per document

    // Set by whichever reader is on this page, and read when the handler
    // below fires rather than captured, so there is nothing to go stale.
    let watched = null;

    // Set by htmx's history cache, consumed by the `watch()` below.
    //
    // A restored page runs its scripts again, so `watch()` is called with
    // the percentage THE SNAPSHOT was rendered at -- which is also what
    // the restored reader puts on screen, since it re-initialises itself
    // from `data-start-percent`. That makes it the right baseline: the
    // question is whether the server is ahead of what this page SHOWS.
    //
    // The hook is `htmx:history:cache:hit` and not the extension's own
    // `cache:after:restore`, because the browsers disagree about when the
    // restored scripts run. Measured, Back into a boosted reader:
    //
    //     Chromium   cache:hit -> watch(20.0) -> cache:after:restore
    //     Firefox    cache:hit -> cache:after:restore -> watch(20.0)
    //
    // `cache:hit` is first in both, so it is the only one `watch()` can
    // rely on finding already set.
    //
    // Cleared by a FORWARD navigation, so a hit on some other page cannot
    // leave this armed for the next reader opened normally -- that one is
    // rendered by the server and has nothing to ask about. A restore
    // emits no `before:history:update` (see the stamp in
    // library-events.js), so the two cannot cancel each other out.
    let restoring = false;
    document.addEventListener("htmx:history:cache:hit", () => { restoring = true; });
    document.addEventListener("htmx:before:history:update", () => { restoring = false; });

    window.readerResume = {
        /**
         * url     -- the progress endpoint to ask.
         * percent -- what this page was rendered at, 0-100.
         */
        watch(url, percent, position = null) {
            watched = { url, sent: Number(percent) || 0, position, root: document.getElementById("reader-root") };
            if (restoring) {
                restoring = false;
                catchUp();
            }
        },
        /** Every progress POST reports its percentage here. */
        sent(percent, position = null) {
            if (watched && Number.isFinite(percent)) { watched.sent = percent; watched.position = position; }
        },
    };

    window.addEventListener("pageshow", (event) => {
        if (watched?.root) delete watched.root.dataset.resumePending;
        if (event.persisted || cameBackInHistory()) catchUp();
    });

    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") catchUp();
    });

    /**
     * Was this document reached by a Back or a Forward?
     *
     * `persisted` answers that for the back/forward cache only. The HTTP
     * cache serves a whole new document that nothing else on a reader can
     * tell from a freshly rendered one: there is no stream here, so there
     * is no cursor to compare a render stamp against the way
     * library-events.js does it for the library pages. Asking is the only
     * way to find out.
     *
     * It costs one small GET per traversal into a reader, and no more: a
     * page the server rendered a moment ago answers with the percentage
     * it was rendered at, which is not AHEAD of itself, so `catchUp`
     * stops there. And `location.replace` is an ordinary navigation, so
     * the page it lands on reads `"navigate"` and cannot ask again.
     *
     * NOT `transferSize === 0`, which reads 0 for a cache hit in the
     * field and 0 for everything under a driven browser -- the trap
     * library-events.js records in full. `type` is what the navigation
     * WAS, not what it measured.
     */
    function cameBackInHistory() {
        const [entry] = performance.getEntriesByType("navigation");
        return entry?.type === "back_forward";
    }

    async function catchUp() {
        if (!watched?.root?.isConnected) return;
        const baseline = watched;
        const sentPosition = baseline.position;
        const sentPercent = baseline.sent;
        let saved;
        try {
            const response = await fetch(baseline.url, { headers: { Accept: "application/json" } });
            if (!response.ok) return;
            saved = await response.json();
        } catch {
            // Offline, or the server gone. A reader whose position is
            // already on screen has nothing to gain from saying so.
            return;
        }
        if (watched !== baseline || !baseline.root.isConnected || baseline.position !== sentPosition || baseline.sent !== sentPercent) return;
        const there = Number(saved?.progress_percent);
        if (!saved?.resume_url || !Number.isFinite(there)) return;
        // A whole percent of slack. The two ends round differently -- an
        // item reader posts `Math.round(fraction * 100)` and a novel
        // `Math.round(fraction * 10000) / 100` -- and a reload for a
        // rounding step would be a reader page that throws itself away
        // every time it is restored.
        const valid = p => Array.isArray(p) && p.length === 3 && p.every(n => Number.isSafeInteger(n) && n >= 0);
        if (valid(saved.position) && valid(baseline.position)) {
            const difference = saved.position.map((n, i) => n - baseline.position[i]).find(n => n !== 0) || 0;
            if (difference <= 0) return;
        } else if (there <= baseline.sent + 1) return;
        baseline.root.dataset.resumePending = "true";
        window.location.replace(saved.resume_url);
    }
})();
