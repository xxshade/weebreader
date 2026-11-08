/**
 * Text-replacement editing, save feedback and draft preservation.
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
 * 2. After a rule is added, its unchanged find/replace boxes are emptied
 *    while a newer draft is retained. The target is NOT reset: adding several rules to one novel is the normal case,
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
                    input.value = input.dataset.savedValue ?? input.defaultValue;
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

})();

// Retained across boosted visits and full-page resyncs. No detached form is
// kept strongly after its request completes; draft capture lasts one morph.
(() => {
    if (window.textReplacementSettings) {
        window.textReplacementSettings.refresh();
        return;
    }
    const pending = new WeakMap(), disabled = new WeakMap();
    let active = 0, message = "";
    const forms = 'form[action^="/settings/text-replacements"]';
    function refresh() {
        for (const button of document.querySelectorAll(forms + ' button')) {
            if (active) {
                if (!disabled.has(button)) disabled.set(button, button.disabled);
                button.disabled = true;
            } else if (disabled.has(button)) {
                button.disabled = disabled.get(button);
                disabled.delete(button);
            }
        }
        for (const form of document.querySelectorAll(forms)) form.setAttribute('aria-busy', active ? 'true' : 'false');
        const status = document.getElementById('replacement-save-status');
        if (status) status.textContent = active ? 'Saving…' : message;
    }
    const sourceForm = event => (event.detail?.ctx?.sourceElement || event.target)?.closest?.(forms);
    document.addEventListener('htmx:before:request', event => {
        const form = sourceForm(event);
        if (!form) return;
        if (pending.has(form)) { event.preventDefault(); return; }
        pending.set(form, form.id === 'add-replacement'
            ? Object.fromEntries(['target', 'find_text', 'replace_text'].map(name => [name, form.elements[name].value])) : null);
        active++; refresh();
    });
    function finish(event, failed) {
        const form = sourceForm(event);
        if (!form || !pending.has(form)) return;
        const submitted = pending.get(form);
        pending.delete(form); active--;
        const status = event.detail?.ctx?.response?.status;
        const saved = !failed && status >= 200 && status < 300;
        message = saved ? 'Saved.' : status === 409 ? 'Not saved. Check the replacement below.' : 'Could not save. Try again.';
        if (saved && submitted && Object.entries(submitted).every(([name, value]) => form.elements[name].value === value)) {
            for (const name of ['find_text', 'replace_text']) form.elements[name].value = '';
            if (form.isConnected) form.elements.find_text.focus();
        }
        refresh();
    }
    document.addEventListener('htmx:after:request', event => finish(event, false));
    document.addEventListener('htmx:error', event => finish(event, true));
    document.addEventListener('htmx:after:process', refresh);
    window.textReplacementSettings = {
        refresh,
        capture() {
            const form = document.getElementById('add-replacement');
            return form ? Object.fromEntries(['target', 'find_text', 'replace_text'].map(name => [name, form.elements[name].value])) : null;
        },
        restore(draft) {
            const form = document.getElementById('add-replacement');
            if (!form || !draft) return;
            for (const name of ['target', 'find_text', 'replace_text']) form.elements[name].value = draft[name];
            form.elements.target.dispatchEvent(new Event('change', {bubbles:true}));
            refresh();
        },
    };
    refresh();
})();
