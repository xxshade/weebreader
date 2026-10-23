/**
 * The settings page's display controls: the app theme, whether to hide the
 * browser's toolbar on a touch device, and the live sample under the
 * cover-blur slider.
 *
 * The theme itself is applied by a one-line attribute on <html>, which
 * the SERVER renders from a cookie (app/ui_prefs.py) -- which is why a
 * reload doesn't flash the previous theme, and why no script in <head>
 * races the parser any more. It is re-applied here the moment the user
 * picks one, so the settings page recolours under them instead of needing
 * a reload to show what they chose.
 *
 * Still a device-level preference, like the reader's own theme and the
 * thumbnail size: a cookie is per browser, not per account.
 */
(() => {
    const select = document.getElementById("app-theme-select");
    if (!select) return;

    const KEY = "app-theme";
    setupFullscreenToggle();
    setupCoverBlurPreview();

    function apply(theme) {
        // "dark" is the default the stylesheet already paints, so it's the
        // absence of the attribute rather than a value of its own -- one
        // less selector to keep in sync in theme.css.
        if (theme === "dark") delete document.documentElement.dataset.appTheme;
        else document.documentElement.dataset.appTheme = theme;
    }

    select.value = window.uiPrefs.read(KEY) || "dark";

    select.addEventListener("change", () => {
        window.uiPrefs.write(KEY, select.value);
        apply(select.value);
    });

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
