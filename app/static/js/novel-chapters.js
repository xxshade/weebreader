/**
 * The chapter filter on a novel's page. Entirely client-side: every
 * chapter is already rendered (this page has never paginated), so finding
 * one is a show/hide over what is here, with no request and no navigation
 * -- the same shape as the home page's search over series cards.
 *
 * Three rules that are not obvious:
 *
 * A TEXT SEARCH forces the volumes holding matches open, and remembers
 * what was open before. The volumes are <details> and all but the one you
 * are reading start closed, so a match inside a closed volume would
 * otherwise be "found" and still invisible. "Unread only" deliberately
 * does NOT do this: it is a way of looking at the list you already have,
 * not a search for one chapter, and having it fling ten collapsed volumes
 * open is exactly the wall of chapters collapsing them was for. So an
 * empty search box means every volume goes back to -- or stays at --
 * whatever it was last set to, by the server or by the reader's own
 * clicking.
 *
 * "Unread only" SURVIVES A RELOAD. It is a way of reading a long novel
 * ("what is left"), not a momentary lookup, and it was being forgotten on
 * every navigation back to the page. Kept PER NOVEL -- turning it on for
 * one used to narrow every other novel too. Stored in a COOKIE, so the server
 * renders the attribute that hides read rows before the first byte of the
 * list (app/ui_prefs.py); the foot-of-page block in detail.html reads that
 * attribute back and must produce the same answer this file does.
 *
 * Read state is read from each row's toggle button, not from a copy taken
 * at load: novel-reader.js flips that button's data-read when a chapter is
 * marked, so "Unread only" follows a chapter marked read while the filter
 * is on instead of quietly describing the page as it was on arrival.
 */
(() => {
    // Per novel: the id is on <main>, which carries no id of its own and so
    // is never handed the previous page's attributes by a swap.
    const novelId = document.querySelector("main")?.dataset.novelId;
    const box = document.getElementById("chapter-filter");
    const unreadOnly = document.getElementById("chapter-unread-only");
    // Dropped before the early return as well, so the document-wide
    // observer at the foot of this file never calls the PREVIOUS novel's
    // filter over this page's rows.
    window.novelChaptersApply = null;
    if (!box || !unreadOnly) return;  // drag mode, or a novel with no chapters

    // Rows are looked up when the filter RUNS rather than once here: a
    // re-sync of the page (library-events.js) morphs a fresh render into
    // place, and a chapter it adds is a row this list would never see.
    const rows = () => document.querySelectorAll("[data-chapter-search]");
    const sections = Array.from(document.querySelectorAll("[data-volume-section]"));

    // Which volumes are meant to be open when nothing is being searched
    // for. Seeded from what the server rendered (the volume you are
    // reading, else the first one) and then kept in step with the reader's
    // own clicking below, so a search can borrow every volume and hand
    // them all back in the state they were actually in.
    const desiredOpen = new Map(
        sections.map((s) => [s, s.querySelector("details")?.open || false]));
    // How many of a volume's pending `toggle` events this file caused. It
    // is a COUNT and not a flag because <details> queues that event as a
    // task rather than firing it inline: by the time it arrives, apply()
    // has long since returned, so a flag set around the assignment would
    // always be back down and every volume a search forced open would be
    // recorded below as the reader's own choice. Two applies can also be
    // in flight at once (typing is one event per keystroke), which is what
    // rules out a boolean per element too.
    const selfInflicted = new Map();

    function isRead(row) {
        return row.querySelector(".read-toggle")?.dataset.read === "true";
    }

    function apply() {
        const query = box.value.trim().toLowerCase();
        const wantUnread = unreadOnly.checked;
        const filtering = query !== "" || wantUnread;

        for (const row of rows()) {
            const matches = (!query || (row.dataset.chapterSearch || "").includes(query))
                && (!wantUnread || !isRead(row));
            row.classList.toggle("hidden", !matches);
        }

        for (const section of sections) {
            const hits = section.querySelectorAll("[data-chapter-search]:not(.hidden)").length;
            // A volume with nothing left in it is hidden outright: an empty
            // accordion row is a dead end that still costs a line and still
            // invites a click.
            section.classList.toggle("hidden", filtering && hits === 0);
            const details = section.querySelector("details");
            // Only a text search touches this. Everything else -- "unread
            // only", a chapter being marked, clearing the box -- leaves
            // each volume however the reader last left it.
            const want = query ? hits > 0 : desiredOpen.get(section);
            if (details && details.open !== want) {
                selfInflicted.set(details, (selfInflicted.get(details) || 0) + 1);
                details.open = want;
            }
        }

        // The icon reports the control that is HIDDEN, not the one in
        // plain sight: the search box is right there in the header row and
        // says for itself that it is narrowing the list. Keep this rule
        // identical to the pre-paint block's in detail.html.
        window.filtersPanel?.setActive(wantUnread);
    }

    box.addEventListener("input", apply);
    unreadOnly.addEventListener("change", () => {
        // A cookie, so the SERVER can open the list already filtered --
        // see ui_prefs.py. One per novel, and only while it is on: off is
        // the default, so a novel's cookie is deleted rather than kept at
        // "0" to ride along on every request.
        if (novelId) {
            const key = window.uiPrefs.unreadKey(novelId);
            if (unreadOnly.checked) window.uiPrefs.write(key, "1");
            else window.uiPrefs.remove(key);
        }
        apply();
    });

    // Opening or closing a volume by hand is a decision, and it is what a
    // search has to give back when it is cleared -- so it is recorded, and
    // the ones apply() caused itself are subtracted off first.
    for (const section of sections) {
        const details = section.querySelector("details");
        if (details) {
            details.addEventListener("toggle", () => {
                const mine = selfInflicted.get(details) || 0;
                if (mine > 0) selfInflicted.set(details, mine - 1);
                else desiredOpen.set(section, details.open);
            });
        }
    }

    // A chapter marked read while "Unread only" is on has to leave the list
    // it no longer belongs in -- and a whole volume marked read has to take
    // all of its chapters with it.
    //
    // Watched as an ATTRIBUTE change rather than hung off the click:
    // novelToggleRead is async and flips data-read only once the POST comes
    // back (see novel-reader.js), so anything scheduled from the click runs
    // while the button still says what it said before. Measured: a chapter
    // marked read on a 763-row filtered list stayed on it. A timeout long
    // enough to win that race would be a guess about the network; the
    // attribute changing is the actual event.
    //
    // No loop: this only ever toggles classes, and only data-read is
    // observed.
    //
    // Installed once per DOCUMENT, and looking the filter up when it
    // FIRES. This file runs again after a whole-<body> swap while
    // document.body does not, so an observer created per run left the
    // previous page's one watching -- and its closure held THAT page's
    // checkbox, a control the swap had already detached. Measured on
    // repeated boosted visits to /novels/{id}: 3, then 4, then 5, against
    // a flat 2 from reorder.js in the same run. Same guard, and the same
    // reason, as the pageshow handler in library-events.js.
    window.novelChaptersApply = apply;
    if (!window.novelChaptersWatchBound) {
        window.novelChaptersWatchBound = true;
        new MutationObserver(() => {
            // The CURRENT page's checkbox and the current page's apply.
            // A page with no filter (drag mode, a novel with no chapters)
            // has neither, and the null above is what says so.
            if (document.getElementById("chapter-unread-only")?.checked) {
                window.novelChaptersApply?.();
            }
        }).observe(document.body, {
            subtree: true, attributes: true, attributeFilter: ["data-read"],
        });
    }

    // The inline block in detail.html has already applied the saved
    // checkbox before paint; this re-run is what lights the filters icon
    // and what keeps the two implementations honest -- if they ever
    // disagreed, the page would visibly change under this call.
    apply();
})();
