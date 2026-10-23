/**
 * Sort control for the series page's item grid.
 *
 * Client-side and no-refetch, the same treatment as the search box beside
 * it (series-search.js) and the home page's sort (home.js): every card is
 * already in the DOM, so reordering is appendChild in a loop.
 *
 * Three orders, and the reason there are three:
 *
 *   manual    what you dragged the items into. Only offered once the
 *             series actually has a manual order -- otherwise it is an
 *             option that means nothing.
 *   original  number in series, then title -- what the scanner worked out
 *             on its own. Stays reachable even while a manual order is in
 *             force, which is why the rank comes from the server on each
 *             card (data-original-order): the DOM only ever holds ONE of
 *             the two orders, so the other cannot be recovered from it.
 *   eta       the estimated time to read the whole item. Sorted on the
 *             PROJECTED TOTAL (data-eta), not the time left, so the grid
 *             does not reshuffle itself as you read. Cards with no
 *             estimate go last in BOTH directions -- "unknown" is not a
 *             value to be at either end of.
 *
 * The default is manual when there is one, original otherwise -- which is
 * exactly the order the server already rendered, so a page nobody has
 * changed the sort on never reorders at all.
 *
 * The SELECT may be absent while the button beside it is not. A comic has
 * no reading-time estimate by construction, so a comic series nobody has
 * dragged into shape has only "Original order" to offer and series.html
 * renders no select at all. The direction button stays, because reversing
 * the one order there is still answers a real question -- so everything
 * below treats the field as "original" when there is nothing to read it
 * from, rather than giving up on the control.
 *
 * The control is not rendered at all while the drag-to-reorder mode is on
 * (see series.html): dragging edits the manual order, and doing that
 * through a grid displayed in some other order would save nonsense.
 */
(() => {
    const control = document.getElementById("series-sort-control");
    if (!control) return;
    const select = document.getElementById("series-sort-select");
    const directionButton = document.getElementById("series-sort-direction");
    const arrows = {
        asc: document.getElementById("series-sort-arrow-asc"),
        desc: document.getElementById("series-sort-arrow-desc"),
    };

    // Keyed by series, like the search box beside it: which order a series
    // wants is a property of that series -- one is a manual pile someone
    // dragged into shape, the next numbers itself correctly -- not of the
    // device. A series never sorted opens in the order the server already
    // rendered (manual when it has one, original otherwise), which is why
    // nothing is written here until the user picks something.
    //
    // Stored as "field-dir", the same shape the home page's sort uses.
    const KEY = `series-item-sort:${control.dataset.seriesId}`;
    // The first version of this saved one order for every series, under
    // this key. It is removed rather than migrated: it holds one series'
    // answer, and there is no way to tell which series that was.
    try {
        localStorage.removeItem("series-item-sort");
    } catch {
        // localStorage unavailable -- nothing to clean up either
    }
    const hasManual = control.dataset.hasManual === "true";
    const DATA_KEY = { manual: "manualOrder", original: "originalOrder", eta: "eta" };
    let direction = "asc";

    function currentField() {
        // No select means there was never more than one order to be in.
        const field = select ? select.value : "original";
        // A manual order the series doesn't have is not a sort -- fall
        // back rather than reordering by an attribute no card carries.
        return field === "manual" && !hasManual ? "original" : field;
    }

    function renderDirection() {
        if (arrows.asc && arrows.desc) {
            arrows.asc.style.display = direction === "desc" ? "none" : "block";
            arrows.desc.style.display = direction === "desc" ? "block" : "none";
        }
        if (directionButton) {
            const label = direction === "desc" ? "Descending" : "Ascending";
            directionButton.title = label;
            directionButton.setAttribute("aria-label", `Sort direction: ${label.toLowerCase()}`);
        }
        renderToggle();
    }

    // The icon that opens these controls is lit while the grid is in any
    // order but the default -- the one the server rendered -- because
    // with the controls behind it nothing else on screen says so. The
    // same job, and the same two classes, as the filters icon
    // (filters-panel.js). Painted with the arrows, since every change of
    // field or direction redraws them. The pre-paint block in series.html
    // sets it first and must keep the same rule.
    const defaultField = hasManual ? "manual" : "original";
    function renderToggle() {
        const toggle = document.getElementById("series-order-toggle");
        if (!toggle) return;
        const lit = currentField() !== defaultField || direction === "desc";
        toggle.classList.toggle("text-indigo-400", lit);
        toggle.classList.toggle("text-neutral-500", !lit);
    }

    function applySort() {
        const grid = document.getElementById("items-grid");
        if (!grid) return;
        const key = DATA_KEY[currentField()] || "originalOrder";
        const dir = direction;
        // Cards are re-queried on every run rather than cached: htmx and
        // the SSE update both replace these nodes (see series-search.js
        // for the same note). The comparator carries the direction itself
        // instead of reverse()-ing the result, so running this twice on
        // its own output is a true no-op -- which it has to be, since the
        // pre-paint block in series.html already ran it once before this
        // deferred file loaded.
        const sorted = Array.from(grid.querySelectorAll("[data-item-id]")).sort((a, b) => {
            const av = a.dataset[key];
            const bv = b.dataset[key];
            if ((av === undefined) !== (bv === undefined)) return av === undefined ? 1 : -1;
            if (av === undefined) return 0;
            const cmp = Number(av) < Number(bv) ? -1 : Number(av) > Number(bv) ? 1 : 0;
            return dir === "desc" ? -cmp : cmp;
        });
        // appendChild moves a node already in the DOM -- this reorders the
        // cards without touching the hidden/visible state the search set.
        for (const card of sorted) grid.appendChild(card);
    }

    function persist() {
        try {
            localStorage.setItem(KEY, `${currentField()}-${direction}`);
        } catch {
            // proceed without persisting
        }
    }

    try {
        const saved = localStorage.getItem(KEY);
        if (saved) {
            const [field, dir] = saved.split("-");
            // The saved FIELD is only adopted when the select still offers
            // it: a series sorted by reading time and then emptied of the
            // format that has one would otherwise sort by an attribute no
            // card carries. The DIRECTION is always adopted -- it applies
            // to whatever order is in force, select or no select.
            if (field && select && select.querySelector(`option[value="${field}"]`)) {
                select.value = field;
            }
            if (dir === "asc" || dir === "desc") direction = dir;
        }
    } catch {
        // localStorage unavailable (private mode etc.) -- server's order stands
    }

    select?.addEventListener("change", () => {
        direction = "asc"; // every field here reads smallest-first the first time
        renderDirection();
        persist();
        applySort();
    });

    if (directionButton) {
        directionButton.addEventListener("click", () => {
            direction = direction === "desc" ? "asc" : "desc";
            renderDirection();
            persist();
            applySort();
        });
    }

    renderDirection();
    applySort();

    // Re-applied after the grid is replaced: mark-all-read/unread swap it
    // through htmx, and library-events.js swaps it directly when the SSE
    // feed reports an item added to the series on screen.
    // The arrows too: a re-sync of the whole page (library-events.js)
    // morphs their inline style back to what the server rendered.
    window.seriesSort = { applySort, renderDirection };
    // Once per document, through window.seriesSort so it is this page's
    // grid that is sorted: <body> outlives a boosted visit, and a listener
    // per visit kept every earlier series page alive.
    if (!window.seriesSortBound) {
        window.seriesSortBound = true;
        document.body.addEventListener("htmx:after:swap", () => {
            window.seriesSort?.renderDirection();
            window.seriesSort?.applySort();
        });
    }
})();
