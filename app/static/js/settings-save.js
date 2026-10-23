/**
 * "Saved", beside whichever settings form was just saved.
 *
 * The preferences on this page save with `hx-swap="none"` and answer 204
 * (see auth/router.py's _settings_saved): nothing on THIS page depends on
 * them, so re-rendering it to report a slider moved only costs the reader
 * their scroll position. What a save then has is no visible outcome at
 * all, which reads as a broken button -- so the form flashes a word.
 *
 * Delegated from the document rather than bound per form: the text
 * replacements below swap their own section through htmx, and a listener
 * bound to an element inside it would go with the swap.
 *
 * The 204 is what says it saved. A refusal comes back as a redirect the
 * browser follows, so this never has to render an error: the re-rendered
 * page carries the message.
 */
(() => {
    // Once per document: a boosted visit runs this file again, and the
    // listener is delegated, so the first copy serves the new page too.
    if (window.settingsSaveBound) return;
    window.settingsSaveBound = true;

    const VISIBLE_MS = 2000;
    const timers = new WeakMap();

    // htmx 4 dropped `detail.successful` -- the detail is `{ctx}` now, and
    // the status lives on ctx.response. Worth knowing that no tool catches
    // this: the shipped upgrade-check scans for event NAMES, so it
    // renamed the listener and left the condition reading `undefined`,
    // which is falsy -- the flash simply stops appearing and nothing
    // anywhere says why.
    //
    // `status < 400` is what htmx 2 meant by successful (it set
    // `successful = !isError`, and isError was the 4xx/5xx rule). A
    // network failure no longer reaches here at all: htmx 4 throws out of
    // the fetch and fires htmx:error instead, which is the right answer
    // for a word that means "saved".
    document.body.addEventListener("htmx:after:request", (event) => {
        const form = event.target.closest ? event.target.closest("form") : null;
        if (!form || !(event.detail?.ctx?.response?.status < 400)) return;
        const flash = form.querySelector(".save-flash");
        if (!flash) return;
        flash.classList.remove("hidden");
        clearTimeout(timers.get(flash));
        timers.set(flash, setTimeout(() => flash.classList.add("hidden"), VISIBLE_MS));
    });
})();
