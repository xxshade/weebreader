/**
 * Name search + library, reading and status filters on the
 * home page. Entirely client-side, on top of what the server already
 * rendered (no re-fetch/no navigation) -- the series cards already carry
 * data-name/data-status/data-library, so filtering is just a show/hide.
 *
 * Requested rule: search ignores BOTH the status filter and the
 * user's hidden-statuses setting from /settings (it searches all
 * already-rendered cards, not only the ones that would pass either one);
 * those two only kick in when the search field is empty. This is why
 * hidden statuses can't be excluded server-side -- the card still needs
 * to reach the DOM for search to find it. The library filter is
 * different: it stays in effect even while searching (it used to be a
 * hard server-side partition -- every card in the DOM belonged to the
 * selected library -- so search was always implicitly scoped to it;
 * this keeps that same scoping now that every library's cards are
 * always in the DOM at once).
 */
(() => {
    const searchInput = document.getElementById("series-search");
    const statusFilter = document.getElementById("status-filter");
    const publicationFilter = document.getElementById("publication-filter");
    const sortSelect = document.getElementById("sort-select");
    const sortDirectionButton = document.getElementById("sort-direction");
    const sortArrows = {
        asc: document.getElementById("sort-arrow-asc"),
        desc: document.getElementById("sort-arrow-desc"),
    };
    // Direction lives on the button, not in the select: listing every field
    // twice made eight options say four things. Kept in one variable rather
    // than read off the DOM so the comparator has a single source.
    let sortDirection = "asc";
    const grid = document.getElementById("series-grid");
    const emptyState = document.getElementById("series-empty-state");
    const libraryNav = document.getElementById("library-filter-nav");

    if (!searchInput || !statusFilter || !grid) return;

    let cards = Array.from(grid.querySelectorAll(".series-card"));

    // Statuses the user chose to hide from browsing (see /settings) --
    // rendered server-side as a comma-separated data attribute on the
    // grid itself.
    const hiddenStatuses = new Set(
        (grid.dataset.hiddenStatuses || "").split(",").filter(Boolean)
    );

    // Same device-level-preference reasoning as FILTER_KEY below. Browsers
    // do restore an <input>'s value on reload by themselves, which is why
    // this search appeared to survive already -- but that restoration is
    // not something the pre-paint script in index.html can rely on having
    // happened yet, and it never applied on the series page at all. Storing
    // it makes both "survives a reload" and "is applied before the first
    // paint" true by construction instead of by browser behaviour.
    const SEARCH_KEY = "library-search";
    try {
        const savedSearch = localStorage.getItem(SEARCH_KEY);
        if (savedSearch) searchInput.value = savedSearch;
    } catch {
        // localStorage unavailable (private mode etc.) -- start empty
    }

    const FILTER_KEY = "library-status-filter"; // device-level display
    // preference (whatever was last selected in the filter) -- not
    // account data, so localStorage is appropriate here, same pattern
    // already used in the reader for mode/fit.
    try {
        const saved = localStorage.getItem(FILTER_KEY);
        if (saved) statusFilter.value = saved;
    } catch {
        // localStorage unavailable (private mode etc.) -- fall back to the default
    }

    // The work's own status, saved the same way and for the same reason:
    // it is a device-level display preference, not account data.
    const PUBLICATION_KEY = "library-publication-filter";
    try {
        const saved = localStorage.getItem(PUBLICATION_KEY);
        if (saved && publicationFilter) publicationFilter.value = saved;
    } catch {
        // localStorage unavailable (private mode etc.) -- fall back to the default
    }

    // The one control that overrides the account's hidden statuses,
    // without changing them: /settings decides what "hidden" means, this
    // decides whether that is being honoured right now. Saved like every
    // other filter here -- a device-level display preference, not account
    // data.
    const SHOW_HIDDEN_KEY = "library-show-hidden";
    const showHiddenToggle = document.getElementById("show-hidden-filter");
    let showHidden = false;
    try {
        showHidden = localStorage.getItem(SHOW_HIDDEN_KEY) === "1";
    } catch {
        // localStorage unavailable (private mode etc.) -- honour the setting
    }
    if (showHiddenToggle) showHiddenToggle.checked = showHidden;

    // Shares the home filters' scope and persistence; series/chapter views
    // keep their own independent unread preferences.
    const UNREAD_KEY = "library-unread-only";
    const unreadToggle = document.getElementById("home-unread-only");
    let unreadOnly = false;
    try {
        unreadOnly = localStorage.getItem(UNREAD_KEY) === "1";
    } catch {
        // Storage unavailable: the checkbox still works for this visit.
    }
    if (unreadToggle) {
        unreadToggle.checked = unreadOnly;
        unreadToggle.addEventListener("change", () => {
            unreadOnly = unreadToggle.checked;
            try {
                localStorage.setItem(UNREAD_KEY, unreadOnly ? "1" : "0");
            } catch {
                // Apply the selection without persisting it.
            }
            applyFilters();
        });
    }

    function syncHiddenStatusOptions() {
        // The hidden statuses are rendered as real options carrying
        // data-hidden-status, so this switch has something to bring back:
        // filtering BY a hidden status is only meaningful while they are
        // being shown.
        for (const option of statusFilter.querySelectorAll("option[data-hidden-status]")) {
            option.hidden = !showHidden;
        }
        // And one cannot stay selected after its option leaves: the select
        // would go on filtering by a value that is no longer on it, which
        // empties the grid with nothing on screen saying why.
        if (!showHidden && hiddenStatuses.has(statusFilter.value)) {
            statusFilter.value = "all";
            try {
                localStorage.setItem(FILTER_KEY, "all");
            } catch {
                // proceed without persisting
            }
        }
    }

    if (showHiddenToggle) {
        showHiddenToggle.addEventListener("change", () => {
            showHidden = showHiddenToggle.checked;
            try {
                localStorage.setItem(SHOW_HIDDEN_KEY, showHidden ? "1" : "0");
            } catch {
                // proceed without persisting
            }
            syncHiddenStatusOptions();
            applyFilters();
        });
    }

    // ---- Library filter (the tabs over the grid) ----
    const LIBRARY_KEY = "library-selected-filter"; // same device-level-preference reasoning as FILTER_KEY/SORT_KEY

    let selectedLibrary = "all";
    try {
        selectedLibrary = localStorage.getItem(LIBRARY_KEY) || "all";
    } catch {
        // localStorage unavailable -- fall back to "all"
    }
    // ?library=<id> selects one on arrival, which is what a link INTO this
    // page from elsewhere needs: the novel page's "back to Web Novels" sent
    // the param and nothing read it, so it landed on whatever library had
    // been chosen last -- a link whose whole promise is the destination.
    //
    // Adopted only when a button of that name exists, so a stale bookmark
    // or a deleted library cannot filter every card away with nothing on
    // screen saying why. And persisted, so the selection this makes is the
    // ordinary one from then on rather than a mode that survives only
    // until the next click. The pre-paint block in index.html does this
    // too, and must reach the same answer.
    const requested = new URLSearchParams(window.location.search).get("library");
    if (requested && libraryNav
        && libraryNav.querySelector(`[data-library-filter="${CSS.escape(requested)}"]`)) {
        selectedLibrary = requested;
        try {
            localStorage.setItem(LIBRARY_KEY, selectedLibrary);
        } catch {
            // proceed without persisting
        }
    }

    // aria-pressed is both the state and the look (.library-tab in
    // theme.css). The Rescan beside the tabs is the selected library's own:
    // one form per library, all hidden but that one. Keep identical to the
    // pre-paint block in index.html.
    function setLibraryButtonStates() {
        if (!libraryNav) return;
        for (const btn of libraryNav.querySelectorAll("[data-library-filter]")) {
            btn.setAttribute("aria-pressed", String(btn.dataset.libraryFilter === selectedLibrary));
        }
        for (const form of document.querySelectorAll("[data-rescan-for]")) {
            form.hidden = form.dataset.rescanFor !== selectedLibrary;
        }
    }

    if (libraryNav) {
        libraryNav.addEventListener("click", (e) => {
            const btn = e.target.closest("[data-library-filter]");
            if (!btn) return;
            selectedLibrary = btn.dataset.libraryFilter;
            try {
                localStorage.setItem(LIBRARY_KEY, selectedLibrary);
            } catch {
                // proceed without persisting
            }
            setLibraryButtonStates();
            applyFilters();
        });
    }
    setLibraryButtonStates();

    function applyFilters() {
        const query = searchInput.value.trim().toLowerCase();
        const status = statusFilter.value;
        const publication = publicationFilter ? publicationFilter.value : "all";
        let visibleCount = 0;

        for (const card of cards) {
            const name = card.dataset.name || "";
            const cardStatus = card.dataset.status || "";
            const cardLibrary = card.dataset.library || "";
            const matchesLibrary = selectedLibrary === "all" || cardLibrary === selectedLibrary;
            // A search looks through everything the HEADER's filters would
            // have narrowed -- the status selects, unread-only and hidden
            // statuses are set aside while typing, so a title you can name
            // is always findable. The library tabs are the
            // exception: they are what the search is scoped BY.
            const matchesSearchOrStatus = query
                ? name.includes(query)
                : (showHidden || !hiddenStatuses.has(cardStatus))
                  && (status === "all" || cardStatus === status)
                  && (publication === "all" || (card.dataset.publication || "") === publication)
                  && (!unreadOnly || Number(card.dataset.unreadCount || 0) > 0);
            const visible = matchesLibrary && matchesSearchOrStatus;
            card.classList.toggle("hidden", !visible);
            if (visible) visibleCount++;
        }

        if (emptyState) emptyState.classList.toggle("hidden", visibleCount > 0 || cards.length === 0);
        syncFilterIndicator(query, status, publication);
    }

    // The filters button is coloured by whether anything is actually
    // narrowing the grid, because behind a popover that is the only thing
    // left saying so -- the same job the tracker icon's three colours do,
    // and the reason this is worth more than the button itself: this page
    // hides cards for several independent reasons, and "why is my series
    // missing" is the question it has to answer without a click.
    //
    // Lit means "not at its defaults", one rule over all header controls.
    //
    // A SEARCH puts it back to grey even with filters set, because a
    // search sets them aside (see the comment in applyFilters): lit while
    // the query is what is really acting would make the icon say the
    // opposite of what the grid is doing, which is exactly the confusion
    // it exists to prevent. Clearing the box brings both back at once.
    function syncFilterIndicator(query, status, publication) {
        const narrowing = !query
            && (status !== "all" || publication !== "all" || showHidden || unreadOnly);
        window.filtersPanel?.setActive(narrowing);
    }

    searchInput.addEventListener("input", () => {
        try {
            localStorage.setItem(SEARCH_KEY, searchInput.value);
        } catch {
            // proceed without persisting
        }
        applyFilters();
    });
    statusFilter.addEventListener("change", () => {
        try {
            localStorage.setItem(FILTER_KEY, statusFilter.value);
        } catch {
            // proceed without persisting
        }
        applyFilters();
    });
    if (publicationFilter) {
        publicationFilter.addEventListener("change", () => {
            try {
                localStorage.setItem(PUBLICATION_KEY, publicationFilter.value);
            } catch {
                // proceed without persisting
            }
            applyFilters();
        });
    }

    syncHiddenStatusOptions();
    applyFilters();

    // ---- Sort (entirely client-side, same treatment as the status filter
    // above: reorders the already-rendered cards in place, persisted in
    // localStorage, no reload and no URL change) ----

    const dataKeyFor = {
        name: "name",
        added: "added",
        modified: "modified",
        reading: "lastRead",
        // The option's value is still "items" -- it is what readers already
        // have saved in localStorage, and renaming it would quietly send
        // every one of them back to sorting by name. Its LABEL and the
        // number it reads are what changed (see library/index.html).
        items: "unreadCount",
    };

    // Fields whose values are numbers, not text. Without this a series of
    // 9 volumes sorts after one of 10, because "9" > "10" as strings.
    const numericFields = new Set(["items"]);

    // Which way a field is usually wanted the first time it is picked: a
    // name reads A-Z, a date reads newest-first. Only used when the FIELD
    // changes -- flipping the button afterwards is remembered as-is.
    const naturalDirectionFor = { name: "asc", added: "desc", modified: "desc", reading: "desc",
                                  items: "desc" };

    function currentField() {
        return sortSelect ? sortSelect.value : "name";
    }

    function renderSortDirection() {
        if (sortArrows.asc && sortArrows.desc) {
            sortArrows.asc.style.display = sortDirection === "desc" ? "none" : "block";
            sortArrows.desc.style.display = sortDirection === "desc" ? "block" : "none";
        }
        if (sortDirectionButton) {
            const label = sortDirection === "desc" ? "Descending" : "Ascending";
            sortDirectionButton.title = label;
            sortDirectionButton.setAttribute("aria-label", `Sort direction: ${label.toLowerCase()}`);
        }
    }

    function applySort() {
        const field = currentField();
        const dir = sortDirection;
        const dataKey = dataKeyFor[field] || "name";
        const numeric = numericFields.has(field);
        // Direction is baked into the comparator itself, rather than
        // sorting ascending and then reverse()-ing the whole array --
        // reverse() also flips the relative order of TIED cards (e.g. two
        // series added in the same rescan, same added_at down to the
        // second), which isn't idempotent: running this same function a
        // second time on its own output (which is exactly what happens on
        // every load, since index.html's inline pre-paint script runs this
        // once and then this deferred script runs it again) would flip
        // those ties right back, producing a visible reorder on load for
        // any tied cards. A stable sort with the sign flipped for "desc"
        // always returns 0 for a tie regardless of direction, so ties stay
        // exactly where they already are -- applying this twice is a true
        // no-op.
        const sorted = [...cards].sort((a, b) => {
            const av = numeric ? Number(a.dataset[dataKey] || 0) : (a.dataset[dataKey] || "");
            const bv = numeric ? Number(b.dataset[dataKey] || 0) : (b.dataset[dataKey] || "");
            const cmp = av < bv ? -1 : av > bv ? 1 : 0;
            return dir === "desc" ? -cmp : cmp;
        });
        // appendChild on a node already in the DOM moves it -- this
        // just reorders the existing cards, it doesn't touch the
        // hidden/visible state applyFilters already set.
        for (const card of sorted) grid.appendChild(card);
    }

    if (sortSelect) {
        const SORT_KEY = "library-sort"; // device-level display preference, same reasoning as FILTER_KEY

        function persistSort() {
            try {
                localStorage.setItem(SORT_KEY, `${currentField()}-${sortDirection}`);
            } catch {
                // proceed without persisting
            }
        }

        try {
            // Still stored as "field-dir", the same string the select used
            // to hold on its own, so a preference saved before the button
            // existed restores unchanged. index.html's pre-paint script
            // parses it the same way.
            const savedSort = localStorage.getItem(SORT_KEY);
            if (savedSort) {
                const [savedField, savedDir] = savedSort.split("-");
                if (savedField) sortSelect.value = savedField;
                if (savedDir === "asc" || savedDir === "desc") sortDirection = savedDir;
            }
        } catch {
            // localStorage unavailable -- fall back to the default (name-asc)
        }

        sortSelect.addEventListener("change", () => {
            // Picking a different field resets to that field's natural
            // direction: carrying "ascending" over from Name into Date
            // added would silently ask for the oldest content first, which
            // is never what the pick meant.
            sortDirection = naturalDirectionFor[currentField()] || "asc";
            renderSortDirection();
            persistSort();
            applySort();
        });

        if (sortDirectionButton) {
            sortDirectionButton.addEventListener("click", () => {
                sortDirection = sortDirection === "desc" ? "asc" : "desc";
                renderSortDirection();
                persistSort();
                applySort();
            });
        }
    }

    renderSortDirection();

    applySort();

    // ---- Live insertion (used by library-events.js when the SSE feed
    // reports a newly-added series -- inserts the new card at its correct
    // sorted position and applies the current filter/search state to it,
    // instead of just telling the user to reload) ----
    // Keyed by KIND as well as id, because the grid holds both series and
    // novels and they carry different id attributes: only a real series
    // gets data-series-id (the live-removal handler matches on it), so a
    // lookup that assumed one would never find a novel card.
    function indexOfCard(kind, id) {
        const key = kind === "novel" ? "novelId" : "seriesId";
        return cards.findIndex((c) => c.dataset[key] === String(id));
    }

    window.libraryGrid = {
        // Whether this grid is the one on screen. The object outlives the
        // page on `window` (a boosted visit does not unload), so callers ask
        // before acting -- see homeGrid() in library-events.js.
        isLive() {
            return grid.isConnected;
        },
        // After library-events.js has morphed a fresh render of this page
        // into place (see resync there): the morph put back the server's
        // card order, its `hidden` classes and its unpressed tabs, and may
        // have added or dropped cards, so `cards` is re-read and every
        // piece of saved state applied again -- the same things the
        // pre-paint block and this file's first run did. A no-op once the
        // home page has been left: this object outlives it on `window`.
        resync() {
            if (!grid.isConnected) return;
            cards = Array.from(grid.querySelectorAll(".series-card"));
            syncHiddenStatusOptions();
            renderSortDirection();
            setLibraryButtonStates();
            applySort();
            applyFilters();
        },
        hasCard(kind, id) {
            return indexOfCard(kind, id) !== -1;
        },
        addCard(cardEl) {
            cards.push(cardEl);
            grid.appendChild(cardEl);
            htmx.process(cardEl);
            applySort();
            applyFilters();
        },
        // Called by library-events.js right before it fades out/removes a
        // series card from the DOM -- without this, the removed element
        // stayed in `cards` forever, and the next applySort() (e.g. the
        // user changing the sort-select, or an unrelated series arriving
        // live) would grid.appendChild() it right back into the DOM, since
        // appendChild on a detached node re-inserts it. That's what made a
        // removed series "come back" on reorder/search instead of staying
        // gone until a real reload.
        removeCard(kind, id) {
            const index = indexOfCard(kind, id);
            if (index !== -1) cards.splice(index, 1);
        },
        // Swaps one card for a freshly built one, keeping its position in
        // `cards` -- used when a series' contents change (an item added or
        // removed) and only its cover/count need to catch up. Goes through
        // here rather than a bare DOM replace so `cards`, which drives
        // sort/filter, doesn't keep pointing at the detached old node.
        replaceCard(kind, id, cardEl) {
            const index = indexOfCard(kind, id);
            if (index === -1) return false;
            cards[index].replaceWith(cardEl);
            cards[index] = cardEl;
            htmx.process(cardEl);
            // Sorted again, not only filtered: the fresh card carries the
            // values the sorts read (data-modified, data-last-read,
            // data-unread-count), and a new volume is exactly what changes
            // "Date modified". Without this the card updated in place and
            // kept its old position until a reload -- measured in both
            // browsers, a series given a new volume stayed second under
            // "Date modified" with the newer date already on its card. A
            // card whose value did not change stays put (the sort is stable).
            applySort();
            applyFilters();
            return true;
        },
    };
})();
