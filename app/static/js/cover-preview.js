// Shows a chosen cover BEFORE it is uploaded, in the slot the current one
// occupies (see _series_edit.html).
//
//   input[type=file][data-cover-preview="<slot id>"]
//     <slot>  [data-cover-current]   what the series has now
//             [data-cover-new]       an <img>, hidden until a file is chosen
//     [data-cover-name]              the chosen file's name, or why not
//
// The file never leaves the browser for this: an object URL of it is
// painted, and the only upload is still the Upload button. Choosing
// nothing again (Cancel in the picker) puts the current cover back.
//
// Delegated from the document and loaded from <head>, once per document,
// for the reason dialogs.js gives: the panel is re-rendered by htmx on
// every save, and the page itself is boosted.
(() => {
    if (window.coverPreview) return;
    window.coverPreview = true;

    let shown = null;

    document.addEventListener("change", (event) => {
        const input = event.target;
        if (!(input instanceof HTMLInputElement) || !input.dataset.coverPreview) return;
        const slot = document.getElementById(input.dataset.coverPreview);
        const form = input.closest("form");
        const name = form?.querySelector("[data-cover-name]");
        const current = slot?.querySelector("[data-cover-current]");
        const preview = slot?.querySelector("[data-cover-new]");
        if (!slot || !preview) return;

        // One object URL alive at a time: each holds the whole file in
        // memory until it is revoked or the document goes.
        if (shown) URL.revokeObjectURL(shown);
        shown = null;

        const file = input.files?.[0];
        if (!file) {
            preview.hidden = true;
            preview.removeAttribute("src");
            if (current) current.hidden = false;
            if (name) name.textContent = "No file chosen";
            return;
        }
        shown = URL.createObjectURL(file);
        // A file the browser cannot draw (HEIC in Chromium, a PDF picked
        // through "All files") still uploads -- the server is what decodes
        // it -- so the preview says so instead of showing a broken image.
        preview.onerror = () => {
            preview.hidden = true;
            if (current) current.hidden = false;
            if (name) name.textContent = `No preview, Upload to try it: ${file.name}`;
        };
        preview.onload = () => {
            if (name) name.textContent = `Preview, not uploaded yet: ${file.name}`;
        };
        preview.src = shown;
        preview.hidden = false;
        if (current) current.hidden = true;
        if (name) name.textContent = file.name;
    });
})();
