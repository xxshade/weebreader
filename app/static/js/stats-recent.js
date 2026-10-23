/**
 * The "Most recent" filter on /stats. Entirely client-side, on top of what
 * the server already rendered -- the rows carry data-kind, so switching is
 * a show/hide with no refetch and no navigation, the same shape as the
 * home page's library filter.
 *
 * The one rule that is not obvious: the list still fills to
 * data-recent-limit after a switch. That is why the server sends the top
 * N of EVERY kind and hides the overflow (see stats/queries.py's
 * recent_items) -- filtering the eight rows an unfiltered list would show
 * answers "of the last eight things, which were comics", not "the last
 * eight comics", and the second is what the control reads as.
 *
 * Entries (one per series) are in recency order across all kinds, so
 * "show the first N that match" is the whole rule, identical for every
 * selection -- which is also what lets the inline pre-paint block in
 * stats.html compute exactly the same visibility this script will compute
 * on its deferred run. Opening a series onto its items is a <details>,
 * with nothing to do here.
 */
(() => {
    const list = document.getElementById("recent-list");
    // Absent when only one kind has been read: there is nothing to choose
    // between, so the template does not draw the row of buttons.
    const nav = document.getElementById("recent-filter-nav");
    if (!list || !nav) return;

    const KEY = "stats-recent-filter";
    const ACTIVE_CLASSES = ["bg-neutral-800", "text-neutral-100"];
    const INACTIVE_CLASSES = ["text-neutral-400", "hover:text-neutral-100", "hover:bg-neutral-900"];
    const limit = Number(list.dataset.recentLimit) || 0;
    const buttons = Array.from(nav.querySelectorAll("[data-recent-filter]"));

    let selected = "all";
    try {
        selected = localStorage.getItem(KEY) || "all";
    } catch {
        // localStorage unavailable (private mode etc.) -- fall back to "all"
    }
    // A saved kind whose button is gone -- everything of that kind dropped
    // out of the list, or was deleted -- would otherwise leave the section
    // empty with no control showing why.
    if (!buttons.some((btn) => btn.dataset.recentFilter === selected)) selected = "all";

    function apply() {
        let shown = 0;
        for (const row of list.querySelectorAll(":scope > [data-kind]")) {
            const visible = (selected === "all" || row.dataset.kind === selected) && shown < limit;
            if (visible) shown++;
            row.classList.toggle("hidden", !visible);
        }
        for (const btn of buttons) {
            const isActive = btn.dataset.recentFilter === selected;
            btn.classList.remove(...ACTIVE_CLASSES, ...INACTIVE_CLASSES);
            btn.classList.add(...(isActive ? ACTIVE_CLASSES : INACTIVE_CLASSES));
        }
    }

    nav.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-recent-filter]");
        if (!btn) return;
        selected = btn.dataset.recentFilter;
        try {
            localStorage.setItem(KEY, selected);
        } catch {
            // proceed without persisting
        }
        apply();
    });

    apply();
})();
