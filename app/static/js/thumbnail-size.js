/**
 * Adjustable thumbnail size, with live preview, for any grid marked with
 * the "thumb-grid" class (the home page's "Reading" strip and series
 * grid, and the series page's item grid). Those grids use
 * `grid-template-columns: repeat(auto-fill, minmax(var(--thumb-size), 1fr))`
 * (see the inline style next to each grid) -- changing the CSS variable is
 * all it takes to resize every card immediately, no reload and no
 * server round-trip.
 *
 * Each page that has this control keeps its own saved size (see the KEY
 * below): a library of series covers and one series' worth of volumes are
 * not the same thing to want big. A page never adjusted inherits the home
 * page's size, and only then the stylesheet's default.
 *
 * The slider itself only shows up in a small popover, opened by clicking
 * the grid icon (id="thumb-size-btn") -- same click-to-open,
 * click-outside-or-Escape-to-close pattern as the header's user menu (see
 * user-menu.js). The saved size is applied to the page on load regardless
 * of whether the popover is ever opened -- only the CONTROL is hidden by
 * default, not the effect of whatever was chosen last time. It is a cookie
 * now, so the SERVER has already applied it to <main> before this runs;
 * what is left here is the slider's own position, and the one load right
 * after the value was carried over from the old storage.
 */
(() => {
    // Which key this page saves under is decided by the page itself, via
    // the `thumb_size_key` block in base.html (which publishes it here on
    // <main> -- this file is shared, so it has no other way to tell which
    // page it is on). The series page overrides it; everything
    // else keeps the original key.
    const HOME_KEY = "thumbnail-size";
    // <main>, not <html>: the key and the size are per PAGE, and
    // <main> is the half a whole-<body> swap replaces (see base.html).
    // Every .thumb-grid is inside it, so the variable still reaches
    // them all by inheritance.
    const page = document.querySelector("main");
    const KEY = (page && page.dataset.thumbSizeKey) || HOME_KEY;
    const DEFAULT_SIZE = 140;

    function applySize(px) {
        if (page) page.style.setProperty("--thumb-size", `${px}px`);
    }

    // Never adjusted on THIS page -- inherit the home page's size rather
    // than jumping back to the default, so a library sized up once still
    // opens its series at that size. Writing the slider here is what stops
    // inheriting, and only for this page. ui_prefs.thumb_size() reads the
    // same two cookies in the same order, and the server has ALREADY
    // applied the answer to <main>; this stays so the slider knows where
    // to sit, and to cover the one load after the cookie was migrated.
    let saved = DEFAULT_SIZE;
    const stored = window.uiPrefs.read(KEY) || window.uiPrefs.read(HOME_KEY);
    if (stored) saved = parseInt(stored, 10) || DEFAULT_SIZE;
    applySize(saved);

    const slider = document.getElementById("thumb-size-slider");
    if (slider) {
        slider.value = saved;
        // The number beside the slider (range-value.js) is drawn from its
        // value. On a full load it waits for DOMContentLoaded and reads
        // this; on a boosted visit it has already drawn the markup's
        // default by the time this runs, so it is told to look again.
        window.rangeValues?.refresh();
        slider.addEventListener("input", () => {
            const px = parseInt(slider.value, 10);
            applySize(px);
            window.uiPrefs.write(KEY, String(px));
        });
    }

    // Visible series covers compete with nearby lazy requests on large grids.
    // Query the current page: this same handler survives boosted navigation.
    function prioritizeSeriesCovers() {
        const grid = document.getElementById("items-grid");
        if (!grid || grid.childElementCount <= 32) return;
        for (const image of grid.querySelectorAll('img[loading="lazy"]')) {
            if (image.complete && image.naturalWidth) continue;
            const rect = image.getBoundingClientRect();
            if (!rect.width) continue;
            if (rect.top >= innerHeight) break;
            if (rect.bottom > 0) {
                image.fetchPriority = "high";
                image.loading = "eager";
                // Start decoding when bytes arrive without delaying navigation.
                // A failed/replaced source still follows native image behavior.
                image.decode().catch(() => {});
            }
        }
    }
    if (document.readyState !== "complete") prioritizeSeriesCovers();
    if (!window.seriesCoverPriorityBound) {
        window.seriesCoverPriorityBound = true;
        // htmx resets the outgoing scroll position before this event.
        document.addEventListener("htmx:after:swap", event => {
            if (event.target === document.body) prioritizeSeriesCovers();
        });
    }

    // The popover: looked up when used, and bound once per document. A
    // boosted visit runs this file again, and each run used to add another
    // pair of document listeners holding the previous page's elements.
    if (window.thumbSizePopoverBound) return;
    window.thumbSizePopoverBound = true;

    const parts = () => ({
        control: document.getElementById("thumb-size-control"),
        btn: document.getElementById("thumb-size-btn"),
        popover: document.getElementById("thumb-size-popover"),
    });
    const isOpen = (popover) => !popover.classList.contains("hidden");

    function setOpen({ btn, popover }, open) {
        popover.classList.toggle("hidden", !open);
        btn.setAttribute("aria-expanded", String(open));
    }

    document.addEventListener("click", (e) => {
        const panel = parts();
        if (!panel.control || !panel.btn || !panel.popover) return; // no grid on this page
        if (panel.btn.contains(e.target)) setOpen(panel, !isOpen(panel.popover));
        else if (isOpen(panel.popover) && !panel.control.contains(e.target)) setOpen(panel, false);
    });

    document.addEventListener("keydown", (e) => {
        const panel = parts();
        if (e.key === "Escape" && panel.popover && isOpen(panel.popover)) setOpen(panel, false);
    });
})();
