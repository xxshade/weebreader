/**
 * Type-to-filter combobox, built over a plain <select data-searchable>.
 *
 * Progressive enhancement, not a replacement: the <select> stays in the
 * form and stays the thing that is submitted. Without JS you get the
 * native control, which is exactly what was there before. That matters
 * here because the settings forms are plain POSTs -- there is no htmx and
 * no fetch to fall back on.
 *
 * The options are read from the <select> itself rather than passed in, so
 * the template stays the single source of truth for what can be picked.
 *
 * No library: this project has no bundler and no npm (see AGENTS.md), and
 * a combobox is a text input, a filtered list, and four keys.
 *
 * `required` MOVES to the visible input. A hidden <select> that fails
 * validation cannot be focused, and Chrome then refuses to submit the form
 * at all, logging "An invalid form control with name=... is not focusable"
 * and showing the user nothing. `required` alone cannot cover the other
 * half -- text typed that matches nothing FILLS the box without picking
 * anything -- so setCustomValidity carries that rule, kept current on
 * every change rather than applied at submit time (see syncValidity).
 */
(() => {
    // Accent- and case-insensitive: a library is full of names nobody wants
    // to reproduce exactly to find them.
    function fold(text) {
        return text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
    }

    function enhance(select) {
        const options = Array.from(select.options).filter((o) => o.value);
        if (!options.length) return; // nothing to search
        const placeholder = (Array.from(select.options).find((o) => !o.value) || {}).textContent || "Search…";
        const controlClass = select.className;
        const wasRequired = select.required;

        const wrap = document.createElement("div");
        wrap.className = "relative";
        wrap.dataset.searchableWrap = "";
        select.parentNode.insertBefore(wrap, select);
        wrap.appendChild(select);
        select.classList.add("hidden");
        select.required = false;
        select.tabIndex = -1;

        const input = document.createElement("input");
        input.type = "text";
        input.className = controlClass;
        input.placeholder = placeholder.trim();
        input.autocomplete = "off";
        input.spellcheck = false;
        input.required = wasRequired;
        input.setAttribute("role", "combobox");
        input.setAttribute("aria-autocomplete", "list");
        input.setAttribute("aria-expanded", "false");
        if (select.getAttribute("aria-label")) input.setAttribute("aria-label", select.getAttribute("aria-label"));
        wrap.appendChild(input);

        const list = document.createElement("ul");
        list.className = "hidden absolute left-0 right-0 mt-1 z-20 bg-neutral-900 border border-neutral-700 "
            + "rounded overflow-y-auto shadow-lg";
        // Inline: app.css is frozen and has no top-full or max-h-* rule, so
        // those classes would silently do nothing (see AGENTS.md).
        list.style.top = "100%";
        list.style.maxHeight = "15rem";
        list.setAttribute("role", "listbox");
        wrap.appendChild(list);

        const rows = options.map((option) => {
            const li = document.createElement("li");
            li.className = "px-3 py-2 text-sm text-neutral-300 cursor-pointer hover:bg-neutral-800";
            li.textContent = option.textContent.trim();
            li.dataset.value = option.value;
            li.setAttribute("role", "option");
            li.dataset.haystack = fold(li.textContent);
            list.appendChild(li);
            return li;
        });

        const empty = document.createElement("li");
        empty.className = "px-3 py-2 text-sm text-neutral-500";
        empty.textContent = "Nothing matches.";
        empty.hidden = true;
        list.appendChild(empty);

        let active = -1;

        function visibleRows() {
            return rows.filter((li) => !li.hidden);
        }

        function renderActive() {
            const shown = visibleRows();
            for (const li of rows) li.classList.remove("bg-neutral-800");
            const current = shown[active];
            if (current) {
                current.classList.add("bg-neutral-800");
                // Keeps the keyboard cursor inside the scrolling list --
                // without it, arrowing past the tenth name walks off the
                // bottom of a box that never scrolls.
                current.scrollIntoView({ block: "nearest" });
            }
            for (const li of rows) li.setAttribute("aria-selected", li === current ? "true" : "false");
        }

        function filter() {
            const query = fold(input.value.trim());
            for (const li of rows) li.hidden = !!query && !li.dataset.haystack.includes(query);
            const shown = visibleRows();
            empty.hidden = shown.length > 0;
            active = shown.length ? 0 : -1;
            renderActive();
        }

        function open() {
            list.classList.remove("hidden");
            input.setAttribute("aria-expanded", "true");
        }

        function close() {
            list.classList.add("hidden");
            input.setAttribute("aria-expanded", "false");
        }

        function labelFor(value) {
            const row = rows.find((li) => li.dataset.value === value);
            return row ? row.textContent : "";
        }

        // Whether the box is in a state the form may submit, recomputed on
        // every change rather than decided when the form is submitted.
        //
        // It used to be decided at submit time, by a listener that called
        // preventDefault() and stopImmediatePropagation() to stop htmx
        // issuing the request anyway. That worked only while this script's
        // listener was registered BEFORE htmx's, and htmx 4 ended that: it
        // binds from its own DOMContentLoaded pass, which is registered
        // while htmx.min.js runs -- first among the deferred scripts -- so
        // its handler went first, read a form that was still valid, and
        // posted. The message was then set on an input nobody would ever
        // see it on. Measured on the settings page with text matching no
        // option: htmx 2 issued 0 requests, htmx 4 issued 1.
        //
        // Native constraint validation has no such ordering. An input that
        // is ALREADY invalid stops the browser's own submit, and stops
        // htmx too -- it calls form.reportValidity() before issuing a
        // request and gives up when that fails. Nothing here has to know
        // htmx exists.
        function syncValidity() {
            if (!wasRequired || select.value) {
                input.setCustomValidity("");
            } else if (input.value.trim()) {
                // Text that matches nothing is not a choice -- and it is
                // exactly the case `required` cannot catch, because a box
                // with text in it is filled in as far as the browser knows.
                input.setCustomValidity("Pick one from the list.");
            } else {
                // Empty: `required` says it better than this could.
                input.setCustomValidity("");
            }
        }

        function choose(li) {
            select.value = li.dataset.value;
            select.dispatchEvent(new Event("change", { bubbles: true }));
            input.value = li.textContent;
            syncValidity();
            close();
        }

        input.addEventListener("focus", () => {
            // The whole list on focus, not only what the current text
            // matches: clicking the box is "show me what there is".
            input.select();
            filter();
            open();
        });

        input.addEventListener("input", () => {
            // Typing after a pick un-picks: the box would otherwise still
            // be submitting the old choice while showing different text.
            select.value = "";
            syncValidity();
            filter();
            open();
        });

        input.addEventListener("keydown", (e) => {
            const shown = visibleRows();
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                if (!shown.length) return;
                open();
                active = (active + (e.key === "ArrowDown" ? 1 : shown.length - 1) + shown.length) % shown.length;
                renderActive();
            } else if (e.key === "Enter") {
                if (list.classList.contains("hidden") || !shown[active]) return;
                e.preventDefault(); // Enter picks; it does not also submit the form
                choose(shown[active]);
            } else if (e.key === "Escape") {
                close();
                input.value = labelFor(select.value);
            }
        });

        // mousedown, not click: a click fires after blur, and blur is what
        // restores the text -- the pick would be undone before it happened.
        list.addEventListener("mousedown", (e) => {
            const li = e.target.closest("li[data-value]");
            if (!li) return;
            e.preventDefault();
            choose(li);
        });

        input.addEventListener("blur", () => {
            close();
            // Half-typed text is not a choice. Put back whatever is
            // actually selected, so what the box says and what the form
            // submits cannot disagree.
            input.value = labelFor(select.value);
            syncValidity();
        });

        // Reached by the one document listener below, rather than each box
        // adding its own: those outlived the page on a boosted visit.
        wrap.searchableClose = close;

        const form = select.form;
        if (form) {
            // A native reset leaves the select empty; the box has to follow.
            form.addEventListener("reset", () => {
                setTimeout(() => {
                    input.value = labelFor(select.value);
                    syncValidity();
                }, 0);
            });
        }

        input.value = labelFor(select.value);
        syncValidity();
    }

    for (const select of document.querySelectorAll("select[data-searchable]")) enhance(select);

    // A click outside a box closes its list. Once per document -- a
    // boosted visit runs this file again -- and over the boxes on the page
    // NOW, so nothing here holds on to a previous page's elements.
    if (window.searchableSelectBound) return;
    window.searchableSelectBound = true;
    document.addEventListener("click", (e) => {
        for (const wrap of document.querySelectorAll("[data-searchable-wrap]")) {
            if (!wrap.contains(e.target)) wrap.searchableClose?.();
        }
    });
})();
