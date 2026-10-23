/**
 * Tap support for the settings page's help bubbles (see _tooltip.html),
 * and for the same bubble on a series page's item cards (the item's author
 * and description, in _item_card.html).
 *
 * The bubble itself is CSS: `:hover` for a mouse, `:focus-within` for a
 * keyboard. This adds the two things CSS cannot express.
 *
 * 1. `[data-open]`, for a tap. Touch has no hover, and iOS Safari does
 *    not reliably focus a <button> on tap.
 *
 * 2. `[data-hover]`, a grace period before closing. The bubble spans the
 *    whole row (see theme.css), so its middle is far to the right of the
 *    marker that opened it, and a pointer travelling there diagonally
 *    leaves `.tip` on the way -- measured: with CSS `:hover` alone, a
 *    pointer walked in steps from the "?" to the middle of the Reading
 *    time bubble arrived after it had already closed, so the link to
 *    /stats inside it could not be clicked. Straight down works, because
 *    the marker is stretched to the row's full height and the bubble
 *    starts at its bottom edge (measured gap: 0px on six of seven, 8px on
 *    the one inside a <label>, which is that label's own padding). The
 *    delay covers the diagonal.
 *
 * With the script missing, both bubbles' TEXT still shows on hover and on
 * keyboard focus; only the tap and the diagonal are lost, and the link
 * stays reachable by tabbing into it (focus moves inside `.tip`, so
 * `:focus-within` holds the bubble open).
 *
 * Delegated from the document because the marker can arrive with an htmx
 * swap, and because "click anywhere else to dismiss" needs a document
 * listener regardless.
 */
(() => {
    // Once per document. A boosted visit runs this file again in the same
    // document, and a second copy of a delegated toggle undoes the first:
    // the tap opened the bubble and the copy closed it again (measured in
    // both browsers -- it worked on every other visit to the page).
    if (window.settingsTooltipBound) return;
    window.settingsTooltipBound = true;

    function closeAll(except) {
        for (const tip of document.querySelectorAll('.tip[data-open="true"]')) {
            if (tip === except) continue;
            delete tip.dataset.open;
            tip.querySelector("[data-tip-toggle]")?.setAttribute("aria-expanded", "false");
        }
    }

    document.addEventListener("click", (e) => {
        const toggle = e.target.closest("[data-tip-toggle]");
        if (toggle) {
            // One of these markers sits inside a <label> (the full-screen
            // row), where a click would otherwise reach the checkbox and
            // flip the setting on the way to reading about it.
            e.preventDefault();
            const tip = toggle.closest(".tip");
            closeAll(tip);
            const open = tip.dataset.open !== "true";
            if (open) tip.dataset.open = "true";
            else delete tip.dataset.open;
            toggle.setAttribute("aria-expanded", String(open));
            return;
        }
        // Same reason: a click inside the bubble must not toggle the
        // checkbox the bubble is explaining. A link in there still works.
        if (e.target.closest(".tip-body") && !e.target.closest("a")) e.preventDefault();
        else closeAll(null);
    });

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeAll(null);
    });

    // Long enough for a hand to cross the width of a bubble, short enough
    // that a bubble left behind does not linger over what is underneath.
    const CLOSE_DELAY_MS = 300;
    let hideTimer = null;

    document.addEventListener("pointerover", (e) => {
        const tip = e.target.closest?.(".tip");
        if (!tip) return;
        clearTimeout(hideTimer);
        tip.dataset.hover = "true";
    });

    document.addEventListener("pointerout", (e) => {
        const tip = e.target.closest?.(".tip");
        if (!tip) return;
        // Moving between the button, its svg and the bubble is all still
        // inside the same marker -- only leaving it entirely counts.
        if (e.relatedTarget && tip.contains(e.relatedTarget)) return;
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => { delete tip.dataset.hover; }, CLOSE_DELAY_MS);
    });
})();
