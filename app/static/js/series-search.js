/**
 * Search box and "Unread only" for the series page's item grid -- the same
 * client-side, no-refetch pattern as the home page's series search (see
 * home.js), over the two things a volume can be looked up by.
 *
 * Anything short of `completed` counts as unread, a halfway-through volume
 * included: the question the box asks is "what is left", and a volume you
 * are in the middle of has not been read. The state comes off each card's
 * data-progress-status, which the grid already renders -- library-events.js
 * compares against the same attribute to decide whether a live update needs
 * the server.
 *
 * Cards are re-queried on every filter run rather than cached once at load
 * -- the mark-all-read/unread buttons and the per-item read toggle now
 * swap #items-grid (or a single card) via htmx, which replaces those DOM
 * nodes. A cached array would end up pointing at detached nodes, the same
 * bug fixed on the home page's series search (see home.js's
 * removeCard).
 */
(() => {
    const searchInput = document.getElementById("item-search");
    if (!searchInput) return;

    // Persisted per series, so searching one doesn't silently filter the
    // next one you open. The home page's search is stored the same way
    // (see home.js's SEARCH_KEY); this one previously wasn't stored at all
    // AND never ran on load, so a reload always came back unfiltered even
    // though the browser had restored the text in the box -- the box said
    // one thing and the grid showed another.
    const marker = document.getElementById("series-page");
    const STORAGE_KEY = marker ? `series-search:${marker.dataset.seriesId}` : null;
    // Per series, for the same reason the search above it is: opening the
    // next series and finding it already narrowed to what you have not read
    // would read as the rest of it having disappeared. It is also the rule
    // this page's sort follows -- which order, and which slice, a series
    // wants is a property of that series.
    const unreadOnly = document.getElementById("item-unread-only");
    const UNREAD_KEY = marker ? `series-unread-only:${marker.dataset.seriesId}` : null;

    function applyFilter() {
        const grid = document.getElementById("items-grid");
        if (!grid) return;
        const query = searchInput.value.trim().toLowerCase();
        const wantUnread = !!(unreadOnly && unreadOnly.checked);
        for (const card of grid.querySelectorAll("[data-item-id]")) {
            const title = card.dataset.title || "";
            const read = (card.dataset.progressStatus || "") === "completed";
            const matches = (!query || title.includes(query)) && (!wantUnread || !read);
            card.classList.toggle("hidden", !matches);
        }
        // The icon says whether the grid is narrowed, since the box that
        // does it is behind a click. A SEARCH does not light it: the two
        // controls here are independent (unlike the home page, where a
        // search sets the filters aside), so the icon reports only the one
        // that lives in the panel. Keep this rule identical to the
        // pre-paint block's in series.html.
        window.filtersPanel?.setActive(wantUnread);
    }

    if (STORAGE_KEY) {
        try {
            const saved = localStorage.getItem(STORAGE_KEY);
            if (saved) searchInput.value = saved;
        } catch {
            // localStorage unavailable (private mode etc.) -- start empty
        }
    }

    if (unreadOnly && UNREAD_KEY) {
        // Restored here as well as in series.html's pre-paint block, not
        // instead of it. The block is what stops the grid painting
        // unfiltered for a frame; this is what keeps the box's state true
        // when that block could not run at all (localStorage throwing
        // inside it aborts the whole thing). Both read the same key and
        // reach the same answer, which is the rule every duplicated
        // filter here follows.
        try {
            if (localStorage.getItem(UNREAD_KEY) === "1") unreadOnly.checked = true;
        } catch {
            // localStorage unavailable (private mode etc.) -- start off
        }
    }

    if (unreadOnly) {
        unreadOnly.addEventListener("change", () => {
            if (UNREAD_KEY) {
                try {
                    localStorage.setItem(UNREAD_KEY, unreadOnly.checked ? "1" : "0");
                } catch {
                    // proceed without persisting
                }
            }
            applyFilter();
        });
    }

    searchInput.addEventListener("input", () => {
        if (STORAGE_KEY) {
            try {
                localStorage.setItem(STORAGE_KEY, searchInput.value);
            } catch {
                // proceed without persisting
            }
        }
        applyFilter();
    });

    // Runs on load too, not only on input: the grid has to actually match
    // whatever is in the box (restored above, or by the browser itself).
    // Also re-applied after htmx swaps #items-grid -- mark-all-read/unread
    // and the live SSE update replace those nodes with a freshly rendered,
    // unfiltered grid.
    applyFilter();

    // library-events.js replaces #items-grid directly (not through htmx)
    // when the SSE feed reports an item added to the series on screen, so
    // it calls this afterwards to re-filter the freshly rendered grid.
    window.seriesSearch = { applyFilter };

    // Bound once per document, and through window.seriesSearch so it is
    // always THIS page's filter that runs: <body> outlives a boosted visit,
    // and a listener per visit kept every earlier series page alive and
    // filtering its detached grid.
    if (!window.seriesSearchBound) {
        window.seriesSearchBound = true;
        document.body.addEventListener("htmx:after:swap", () => window.seriesSearch?.applyFilter());
    }
})();
