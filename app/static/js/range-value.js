/**
 * Puts the current number beside every slider.
 *
 * A range input says "somewhere between these ends" and nothing else, so
 * a setting like "Width" or "Size" could only be judged by looking at the
 * result and guessing. The value is rendered from the input itself rather
 * than written into each template, so a slider gains one by adding an
 * attribute and cannot drift out of step with what it is set to:
 *
 *     <input type="range" data-range-value="px" ...>
 *
 * The attribute's value is the suffix ("px", "%", or empty). Decimals come
 * from `step`, so a 0.1-step slider reads 1.6 rather than 1.6000000000001
 * or a bare 2 where every other stop shows a decimal.
 *
 * The readout is created here rather than in the templates: it belongs to
 * the input, five templates would have to agree on its markup, and one of
 * them forgetting would be invisible until someone dragged that slider.
 * The width is fixed in an inline style because app.css is frozen (see
 * AGENTS.md) and a min-w-* utility for it has no rule -- without it the
 * label reflows on every drag as "80%" becomes "100%".
 *
 * Deliberately NOT applied to the reader's Bottom gap, which has a readout
 * of its own: that one shows the total gap in force, detected plus manual,
 * which is a different number from the slider's.
 */
(() => {
    function decimalsOf(step) {
        const text = String(step || "");
        const dot = text.indexOf(".");
        return dot < 0 ? 0 : text.length - dot - 1;
    }

    function readoutFor(input) {
        const existing = input.nextElementSibling;
        if (existing && existing.classList.contains("range-value")) return existing;
        const span = document.createElement("span");
        span.className = "range-value text-xs text-neutral-500";
        span.style.display = "inline-block";
        span.style.minWidth = "3rem";
        span.style.textAlign = "right";
        // aria-hidden: the slider already announces its own value, and a
        // screen reader saying it twice is worse than not showing it.
        span.setAttribute("aria-hidden", "true");
        input.insertAdjacentElement("afterend", span);
        return span;
    }

    function render(input) {
        const places = decimalsOf(input.step);
        const number = places ? Number(input.value).toFixed(places) : input.value;
        readoutFor(input).textContent = `${number}${input.dataset.rangeValue}`;
    }

    function refresh() {
        for (const input of document.querySelectorAll("input[type=range][data-range-value]")) {
            render(input);
        }
    }

    // Delegated, so a slider that arrives later (an htmx swap, a panel
    // built at runtime) needs no wiring of its own.
    document.addEventListener("input", (e) => {
        const input = e.target;
        if (input && input.matches && input.matches("input[type=range][data-range-value]")) render(input);
    });

    // On DOMContentLoaded rather than immediately: the deferred scripts
    // that own these sliders set their saved values during their own load,
    // and this has to read what they set, not the markup's default.
    //
    // The test is against "complete", NOT `=== "loading"`. While a
    // deferred script runs, readyState is already "interactive" and
    // DOMContentLoaded has not fired yet -- so a "loading" test takes the
    // immediate branch and reads the markup's default. Measured: the
    // thumbnail slider read 170px (a range with no value attribute starts
    // at its midpoint) beside a handle sitting at 140.
    if (document.readyState === "complete") {
        refresh();
    } else {
        document.addEventListener("DOMContentLoaded", refresh);
    }

    // And after every htmx swap. A boosted visit replaces the page without
    // loading one, so neither branch above runs again: the slider arrives
    // with no readout, and the delegated `input` above only creates one
    // once it is dragged. Measured in both browsers: 140px on a full load
    // of home or a series, nothing at all on either reached by a link.
    //
    // This alone is right only for a slider whose value is in the markup.
    // after:process fires BEFORE the swapped-in scripts run, so a slider
    // whose saved value a script sets reads its default here (measured:
    // 260px, the midpoint, beside a thumbnail slider at 180). Such a script
    // calls window.rangeValues.refresh() once it has set the value -- see
    // thumbnail-size.js.
    document.addEventListener("htmx:after:process", refresh);

    // For anything that moves a slider from code afterwards.
    window.rangeValues = { refresh };
})();
