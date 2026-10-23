/**
 * The filters popover, shared by the three pages that have one: the home
 * grid, a series' items and a novel's chapters.
 *
 * Only the SHELL is here -- opening, closing, and the button's colour.
 * What each page filters by is its own business and lives in its own
 * script; this file never reads a control.
 *
 * The colour is the part worth explaining. Once the controls are behind a
 * click, the icon is the only thing on screen saying the list is narrowed
 * -- the same job the tracker icon's three colours do. Each page decides
 * what "narrowed" means and calls setActive(); the page's own pre-paint
 * block sets the class first, because an icon corrected after the first
 * frame is a visible flip (measured on the home page with that half
 * removed: frame 1 grey, frames 2-4 indigo).
 *
 * setActive is exported rather than computed here for the same reason
 * applyFilters lives per page: "is anything filtering" is a different
 * question on a grid of series, a grid of volumes and a list of chapters,
 * and one file guessing for all three is how the icon starts lying.
 */
(() => {
    const LIT = "text-indigo-400";
    const IDLE = "text-neutral-500";

    // Looked up when used, never held: a boosted visit replaces all three
    // with the next page's, and this file's listeners outlive the page
    // they were bound on.
    const parts = () => ({
        control: document.getElementById("filters-control"),
        btn: document.getElementById("filters-btn"),
        popover: document.getElementById("filters-popover"),
    });

    // No panel on this page (or none rendered -- a novel in drag mode has
    // no filter at all): setActive then does nothing, so a caller does not
    // have to check.
    window.filtersPanel = {
        setActive(active) {
            const { btn } = parts();
            if (!btn) return;
            btn.classList.toggle(LIT, !!active);
            btn.classList.toggle(IDLE, !active);
        },
    };

    // Once per document. A boosted visit runs this file again, and each
    // run used to add another pair of document listeners holding the old
    // page's elements -- nine copies after four visits (measured).
    if (window.filtersPanelBound) return;
    window.filtersPanelBound = true;

    const isOpen = (popover) => !popover.classList.contains("hidden");

    function setOpen({ btn, popover }, open) {
        popover.classList.toggle("hidden", !open);
        btn.setAttribute("aria-expanded", String(open));
    }

    // The button toggles; a click anywhere outside closes, and Escape
    // closes -- the same three ways every other popover here behaves
    // (thumbnail-size.js, the user menu, the /stats range).
    document.addEventListener("click", (e) => {
        const panel = parts();
        if (!panel.control || !panel.btn || !panel.popover) return;
        if (panel.btn.contains(e.target)) setOpen(panel, !isOpen(panel.popover));
        else if (isOpen(panel.popover) && !panel.control.contains(e.target)) setOpen(panel, false);
    });

    document.addEventListener("keydown", (e) => {
        const panel = parts();
        if (e.key === "Escape" && panel.popover && isOpen(panel.popover)) setOpen(panel, false);
    });
})();
