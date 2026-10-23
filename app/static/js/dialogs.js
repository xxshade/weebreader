// The app's plain dialogs: "New library" on the home page and "Edit name
// and cover" on a series page. The tracking dialog keeps its own script
// (tracker-toggle.js), because it has more to do than open and close.
//
//   [data-dialog-open="<id>"]  opens that <dialog> as a modal
//   [data-dialog-close]        closes the <dialog> it sits in
//   [data-dialog-focus]        what gets the caret when one opens
//
// Delegated from the document, and loaded from <head> so it runs ONCE per
// document: the pages that carry these dialogs are boosted, a whole-<body>
// swap re-executes a page's own scripts, and a document listener added on
// every visit would open a dialog once per visit made.
//
// A native <dialog>, so Escape and the focus trap are the browser's. The
// backdrop does NOT close it, the same rule the tracking dialog follows:
// both of these hold a half-typed form, and a stray click outside is not
// a decision to throw that away. So a pointer leaves through the ✕ or a
// Cancel, which are the [data-dialog-close] above.
(() => {
    if (window.appDialogs) return;
    window.appDialogs = true;

    document.addEventListener("click", (event) => {
        const opener = event.target.closest("[data-dialog-open]");
        if (opener) {
            const box = document.getElementById(opener.dataset.dialogOpen);
            if (!box) return;
            // `open` already means it came back from a refusal without
            // htmx and was rendered open; showModal() on it would throw.
            if (!box.open) box.showModal();
            // Not `autofocus`: htmx honours that on every swap, and the
            // edit dialog's forms swap the panel they sit in -- saving the
            // description would throw the caret back into the name.
            box.querySelector("[data-dialog-focus]")?.focus();
            return;
        }
        if (event.target.closest("[data-dialog-close]")) {
            event.target.closest("dialog")?.close();
        }
    });
})();
