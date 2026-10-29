/** Settings display controls: fullscreen and cover-blur preview. */
(() => {
    setupFullscreenToggle();
    setupCoverBlurPreview();

    /**
     * The row is rendered hidden and only revealed here, where the
     * Fullscreen API can actually be asked about. A checkbox that does
     * nothing on this device is worse than no checkbox -- the same call
     * fullscreen.js already makes for the reader's own button.
     *
     * It reads the SAME preference the reader's toggle writes, defaults
     * included: unchecking it stores an explicit "off" rather than
     * clearing the key, since clearing would go back to the on-by-default
     * for a touch device and undo the click.
     */
    function setupFullscreenToggle() {
        const row = document.getElementById("app-fullscreen-row");
        const toggle = document.getElementById("app-fullscreen-toggle");
        const api = window.readerFullscreen;
        // Nothing to switch off inside an installed app the manifest
        // already opens chrome-free -- the preference only governs a
        // browser tab, and that is where it stays reachable.
        if (!row || !toggle || !api || !api.supported || api.inAppFullscreen()) return;
        row.classList.remove("hidden");
        row.classList.add("flex");
        toggle.checked = api.wanted();
        toggle.addEventListener("change", () => api.remember(toggle.checked));
    }

    /**
     * The cover-blur slider, shown doing it to a real cover.
     *
     * Unlike everything else in this file the value itself is SERVER
     * state -- it has to be, or a blurred cover would be drawn sharp for
     * the frame before a script got to it -- so nothing is stored here
     * and the Save button is still what applies it. This only moves the
     * sample under the slider, because "18px" tells you nothing about the
     * one thing the setting decides: whether the cover is still
     * recognisable.
     *
     * The same variable name the grid publishes (--cover-blur), so the
     * preview and the real thing cannot drift apart.
     */
    function setupCoverBlurPreview() {
        const slider = document.getElementById("cover-blur");
        const preview = document.getElementById("cover-blur-preview");
        // No preview when the library has no cover to show one with.
        if (!slider || !preview) return;
        slider.addEventListener("input", () => {
            preview.style.setProperty("--cover-blur", `${slider.value}px`);
        });
    }
})();
