// "Show more" for text that is clamped to a few lines in CSS.
//
// The clamp itself is the stylesheet's (.clamp-3), so the short version is
// what the first frame draws. This script does the one thing CSS cannot:
// decide whether the text actually overflows AT THIS WIDTH, and only then
// offer the button -- a "Show more" under a one-line description is worse
// than no button at all.
//
// Delegated from the document, and re-measured on htmx settles, because
// the element it lives in is replaced out of band: the series edit panel
// swaps the description when it is saved, and so does a tracker link that
// brought a description over. A listener bound to the button would go
// with the old node, silently.
(() => {
    const CLAMP = "clamp-3";

    function measure(box) {
        const text = box.querySelector(".clamp-text");
        const toggle = box.querySelector(".clamp-toggle");
        if (!text || !toggle) return;
        // Only meaningful while the text IS clamped: expanded, the two
        // heights are equal by definition and the button would hide
        // itself mid-read.
        if (!text.classList.contains(CLAMP)) return;
        // A pixel of slack: sub-pixel line heights make scrollHeight
        // exceed clientHeight by a fraction on text that fits exactly.
        const fits = text.scrollHeight - text.clientHeight <= 1;
        // WHICH way to hide it depends on who made room for it. The server
        // reserves the slot for text long enough to possibly need the
        // button (see templates/_clamp.html), and there `display: none`
        // would give the slot back and move the page -- the very shift the
        // reservation exists to avoid. Where no slot was reserved there is
        // nothing to keep, and the button appearing is the old behaviour:
        // rare now, for words far longer than the threshold assumed.
        if (toggle.hasAttribute("data-reserved")) {
            toggle.classList.toggle("clamp-toggle-unneeded", fits);
        } else {
            toggle.classList.toggle("hidden", fits);
        }
    }

    function measureAll(root) {
        for (const box of (root || document).querySelectorAll("[data-clamp]")) measure(box);
    }

    // Measured on every run -- a boosted visit brings a new page to
    // measure -- but BOUND once per document: a second copy of the click
    // listener toggled the text straight back, so "Show more" worked on
    // every other visit to a series.
    if (window.readMoreBound) {
        measureAll();
        return;
    }
    window.readMoreBound = true;

    document.addEventListener("click", (event) => {
        const toggle = event.target.closest(".clamp-toggle");
        if (!toggle) return;
        const box = toggle.closest("[data-clamp]");
        const text = box && box.querySelector(".clamp-text");
        if (!text) return;
        const expanded = text.classList.toggle(CLAMP) === false;
        toggle.textContent = expanded ? "Show less" : "Show more";
        toggle.setAttribute("aria-expanded", String(expanded));
        if (!expanded) measure(box);
    });

    // A narrower window fits fewer words per line, so text that needed no
    // button can start needing one (and the other way round).
    let resizing = null;
    window.addEventListener("resize", () => {
        clearTimeout(resizing);
        resizing = setTimeout(() => measureAll(), 150);
    });

    // Swapped-in copies arrive already clamped and with the button
    // hidden; this is what asks whether it should be. Measured over the
    // whole document rather than the event's target, because an
    // out-of-band swap lands OUTSIDE what htmx was pointed at -- which is
    // this element's whole situation. There are a handful of them on a
    // page, so the sweep costs nothing worth saving.
    //
    // htmx 4 renamed both of these. The second one is NOT the rename the
    // upgrade guide's table gives (htmx:load -> htmx:after:init): that
    // event fires once per element htmx initialises, so a swap carrying
    // eight buttons would sweep the document eight times for one arrival.
    // htmx:after:process fires once per processed root, which is what
    // htmx:load meant here -- library-events.js calls htmx.process() on
    // markup it inserted itself, and this is how that markup gets
    // measured.
    document.addEventListener("htmx:after:settle", () => measureAll());
    document.addEventListener("htmx:after:process", () => measureAll());

    measureAll();
})();
