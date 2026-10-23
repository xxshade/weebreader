// The icons beside the title open a tracking dialog, on both pages that
// carry one.
//
// Delegated from the document rather than bound to the buttons, because
// the buttons are REPLACED: a link or an unlink changes whether this
// shelf is tied to the service, so the dialog's response carries fresh
// copies of both icons as out-of-band swaps. A bound listener would go
// with the old node, and the failure would be silent -- the icon still
// there, still looking right, doing nothing. Same rule the stats page's
// calendar follows.
//
// The dialog itself is a native <dialog>, so Escape and the focus trap
// are the browser's rather than this app's. The BACKDROP is not: a modal
// dialog stays open on a click outside it unless it carries `closedby`,
// and it deliberately does not -- the box holds a half-typed editor, and
// a stray click is not a decision to throw that away. Measured in both
// browsers: Escape shut it, a click at (20,20) did not. So the pointer
// leaves through the ✕ or through Cancel, both of which are the
// [data-tracker-close] below.
(() => {
    // Once per document: a boosted visit runs this file again, and every
    // listener here is delegated, so the first copy already serves the new
    // page.
    if (window.trackerToggleBound) return;
    window.trackerToggleBound = true;

    const DIALOG = "tracker-dialog";

    function dialog() {
        return document.getElementById(DIALOG);
    }

    document.addEventListener("click", (event) => {
        const opener = event.target.closest("[data-tracker-dialog]");
        if (opener) {
            const box = dialog();
            // Already open means another service's icon was clicked while
            // this dialog was up: showModal() on an open dialog throws,
            // and the htmx swap replaces the contents either way.
            if (box && !box.open) box.showModal();
            emptyPanel(opener.dataset.trackerLabel || "");
            return;
        }
        if (event.target.closest("[data-tracker-close]")) dialog()?.close();
    });

    // The panel the OTHER service left behind is not a loading state, it
    // is wrong -- the two icons swap the same element, and opening
    // MyAnimeList after MangaUpdates left MangaUpdates' chapter, volume
    // and score on screen, under a MyAnimeList heading, until the request
    // came back. Measured against a stub answering instantly and against
    // the real thing: the editor is behind TWO calls to the catalogue (a
    // list read and a rating read, see service.remote_state), so the wrong
    // numbers sat there for about a second and were plausible enough to
    // press Update on.
    //
    // Emptied here rather than by the server, because the server's answer
    // IS the thing being waited for. Only the icon does this: a search, a
    // link, a send and an unlink all swap the panel of the service whose
    // panel is already up, and blanking for those would flash the page for
    // nothing.
    function emptyPanel(label) {
        const panel = document.getElementById("tracker-panel");
        if (!panel) return;
        // The ✕ is MOVED, not rebuilt: it is the only way out of a modal
        // dialog that a pointer has, and a second copy of that markup
        // here is a second thing to keep in step with the panel.
        const closer = panel.querySelector("[data-tracker-close]");
        panel.textContent = "";
        const note = document.createElement("p");
        note.className = "text-sm text-neutral-500";
        // Named, because this is also what says the dialog changed
        // service: with a bare "Loading…" the only difference between the
        // two icons for that second is the heading that is not there yet.
        note.textContent = label ? `Reading ${label}…` : "Loading…";
        const row = document.createElement("div");
        row.className = "flex items-center gap-2";
        row.append(note);
        // Carries its own ml-auto, so it lands on the right of this row
        // exactly as it does on the panel's heading.
        if (closer) row.append(closer);
        panel.append(row);
    }

    // Focus the search box once the panel has actually arrived, rather
    // than on the click: at click time the dialog is still empty, and
    // there is nothing to focus. Only when there IS one -- a linked
    // service opens on an editor whose first box is a number, and
    // stealing focus into it puts a caret in a field nobody asked to
    // change.
    //
    // `after:process` and not `after:swap`, which is the event this
    // obviously wanted: htmx 4 aims `htmx:after:swap` at the ELEMENT THAT
    // TRIGGERED the request -- the icon -- not at what was swapped in, so
    // matching on the target's id found nothing and the box was never
    // focused. Measured in both browsers: the events arrive as
    // after:request, before:swap, after:settle, after:process
    // (target #tracker-panel), after:swap (target the icon), and
    // document.activeElement is the DIALOG throughout, which is what
    // showModal() leaves focused when nothing inside asks for it.
    document.addEventListener("htmx:after:process", (event) => {
        if (event.target?.id !== "tracker-panel") return;
        event.target.querySelector("input[name='q']")?.focus();
    });
})();
