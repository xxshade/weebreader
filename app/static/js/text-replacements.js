/**
 * The two things the text-replacement list needs a browser for.
 *
 * Everything else is htmx: adding, editing and removing a rule each POST
 * and swap #replacement-rules in place, so the page never reloads and the
 * rest of the settings page (an avatar just chosen, a limit half-typed)
 * is left alone.
 *
 * 1. The pencil toggles a row between its view and its edit form. Both are
 *    rendered by the server (see _replacement_rules.html) -- toggling a
 *    class beats fetching a second copy of the row, and it is the same
 *    shape the series page's edit panel uses.
 *
 * 2. After a rule is added, the find/replace boxes are emptied but the
 *    target is NOT: adding several rules to one novel is the normal case,
 *    and re-picking it every time would be the annoying half of the job.
 *
 * Delegated to document, both of them: htmx replaces the whole list on
 * every change, so a listener bound to a row would go with the row.
 */
(() => {
    function row(el) {
        return el.closest("[data-rule-id]");
    }

    function setEditing(container, editing) {
        container.querySelector("[data-rule-view]")?.classList.toggle("hidden", editing);
        const form = container.querySelector("[data-rule-edit-form]");
        form?.classList.toggle("hidden", !editing);
        if (editing) form?.querySelector("input[name=find_text]")?.focus();
    }

    // The delegated half once per document (a boosted visit runs this file
    // again); the add form below is this page's own element, so it is
    // bound on every run.
    if (!window.textReplacementsBound) {
        window.textReplacementsBound = true;

        document.addEventListener("click", (e) => {
            const edit = e.target.closest("[data-rule-edit]");
            if (edit) {
                const container = row(edit);
                if (container) setEditing(container, true);
                return;
            }
            const cancel = e.target.closest("[data-rule-cancel]");
            if (cancel) {
                const container = row(cancel);
                if (!container) return;
                // Put the inputs back to what the server last rendered, so
                // cancelling twice cannot leave edited text behind in a row
                // that is showing its saved values.
                for (const input of container.querySelectorAll("[data-rule-edit-form] input")) {
                    input.value = input.defaultValue;
                }
                setEditing(container, false);
            }
        });

        document.addEventListener("keydown", (e) => {
            if (e.key !== "Escape") return;
            const container = e.target.closest && e.target.closest("[data-rule-edit-form]") && row(e.target);
            if (container) setEditing(container, false);
        });
    }

    const addForm = document.getElementById("add-replacement");
    if (addForm) {
        addForm.addEventListener("htmx:after:request", (e) => {
            // Only on a real save. A failed request leaves what was typed
            // where it is, which is the only way to retry it.
            //
            // htmx 4 replaced `detail.successful` with the request context
            // (see settings-save.js for why this is the one migration step
            // no tool catches).
            if (!e.detail?.ctx?.response || e.detail.ctx.response.status >= 400) return;
            for (const name of ["find_text", "replace_text"]) {
                const input = addForm.querySelector(`input[name=${name}]`);
                if (input) input.value = "";
            }
            addForm.querySelector("input[name=find_text]")?.focus();
        });
    }
})();
