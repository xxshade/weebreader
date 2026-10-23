/**
 * Hides the mobile browser's toolbar, on every screen.
 *
 * Chrome and Firefox on Android auto-hide their toolbar when the DOCUMENT
 * scrolls. Every reader here is 100dvh + `overflow: hidden` -- from
 * `body:has(#reader-root)` in theme.css, not from a class on <body> --
 * with the scrolling happening in an inner container (#custom-scroll,
 * #page-scroll), so the document itself never scrolls and that heuristic
 * can never fire.
 * The Fullscreen API is the only thing left that removes the toolbar in a
 * plain browser tab -- CSS has no say over the browser's own chrome.
 *
 * The catch is that it requires a user gesture, and a gesture does NOT
 * survive a navigation: the tap that opened the reader happened in the
 * PREVIOUS document, so a reader can never enter fullscreen on load,
 * however the preference is set. Hence the shape of this module: an
 * explicit toggle, plus a remembered preference that re-enters on the
 * first gesture inside the next reader (a tap to turn the page, a swipe, a
 * key) instead of on load.
 *
 * Installed as a PWA the manifest asks for `display: fullscreen`, which is
 * the only way to get this WITHOUT a gesture -- the app simply opens with
 * no browser chrome at all, on every screen, and none of the code below is
 * involved. Everything here is the browser-tab half, where the Fullscreen
 * API and its gesture requirement are all there is.
 *
 * On a touch device it is the DEFAULT rather than something to turn on:
 * `armGlobal` re-enters on the first tap of each page, since fullscreen is
 * a property of a document and navigating away ends it. On a desktop
 * browser nothing happens unless the reader's toggle was used -- taking
 * over the whole screen because someone clicked a link would be hostile.
 */
window.readerFullscreen = (() => {
    // Named for the reader because that is where it started; it is now the
    // whole app's preference. Kept as-is so nobody's saved choice resets.
    const KEY = "reader-fullscreen";

    // A phone or tablet: the browser has a toolbar eating the screen, and
    // the gesture that would enter fullscreen is the same tap you were
    // making anyway. A desktop browser is neither.
    function onTouchDevice() {
        return window.matchMedia("(pointer: coarse)").matches;
    }

    // Unset means "yes on a touch device": that is the thing being asked
    // for, and having to find a toggle on every install to get it is not
    // the same feature. An explicit "0" from the reader's button or the
    // settings page still wins -- turning it off has to stay possible.
    function wanted() {
        try {
            const stored = localStorage.getItem(KEY);
            if (stored === "1") return true;
            if (stored === "0") return false;
        } catch {
            // localStorage unavailable (private mode etc.) -- fall through
        }
        return onTouchDevice();
    }
    function remember(on) {
        try {
            localStorage.setItem(KEY, on ? "1" : "0");
        } catch {
            // proceed without persisting -- the toggle still works for this session
        }
    }

    const supported = !!document.fullscreenEnabled;
    const isOn = () => document.fullscreenElement !== null;

    // Installed with `display: fullscreen`, the app ALREADY has no chrome
    // and the Fullscreen API has nothing left to hide -- but it does not
    // know that: `document.fullscreenElement` is null, because the display
    // mode is the manifest's, not an element's. Requesting it anyway put a
    // second, weaker fullscreen on top of the real one: it ended on every
    // navigation, so the system bar slid back in and the next tap re-entered
    // it a couple of seconds later, announced by Android's toast on every
    // page turn. Nothing to enter, nothing to offer.
    //
    // `display-mode: fullscreen` also matches while the API is in use, so
    // the second half is what separates "the manifest gave us this" from
    // "we asked for it ourselves".
    function inAppFullscreen() {
        return window.matchMedia("(display-mode: fullscreen)").matches && !isOn();
    }

    // Both directions can reject (denied, already in that state, a gesture
    // the browser didn't count) and neither rejection is worth surfacing:
    // the button re-syncs from the fullscreenchange event either way.
    async function enter() {
        try {
            await document.documentElement.requestFullscreen({ navigationUI: "hide" });
            return true;
        } catch {
            return false; // stayed windowed
        }
    }
    async function exit() {
        try {
            await document.exitFullscreen();
        } catch {
            /* stayed fullscreen */
        }
    }

    // Entered at most once per document. Leaving fullscreen through the
    // browser's own affordance (Escape, the swipe-down on Android) must not
    // be undone by the next tap -- that would be a fight the user cannot
    // win. The preference itself survives, so the next chapter or book
    // still opens fullscreen; it is only this document that gives up.
    let autoEnterDone = false;
    let userLeft = false;
    document.addEventListener("fullscreenchange", () => {
        if (autoEnterDone && !isOn()) userLeft = true;
    });

    // The attempt is spent only when it actually WORKED. A gesture that the
    // browser does not count as user activation makes requestFullscreen
    // reject, and marking the document done there would swallow the real
    // gesture that follows -- which is how a touchstart-armed version left
    // the whole app windowed until the reader's own button was pressed.
    let attempting = false;
    async function armFromGesture() {
        if (autoEnterDone || userLeft || attempting || !supported) return;
        if (inAppFullscreen()) return;
        if (!wanted()) return;
        if (isOn()) {
            autoEnterDone = true;
            return;
        }
        // One tap delivers several of the events armGlobal listens to;
        // without this they would each fire a request of their own.
        attempting = true;
        const entered = await enter();
        attempting = false;
        if (entered) autoEnterDone = true;
    }

    // The toggle the page is showing RIGHT NOW, looked up when the
    // listener below fires rather than closed over when it is bound.
    //
    // `setupButton` runs again for every reader opened -- it is called
    // from the reader engines, which are in page content -- and under
    // hx-boost `document` outlives every one of them. A
    // `fullscreenchange` listener per call would leave each past visit's
    // sync writing "Exit full" into a button that is no longer in the
    // page. Measured over three reader visits: **2, 3 and 4** of them
    // before this, against the 1 that belongs to this document and a
    // flat 2 with it.
    //
    // Same shape, same reason, as the MutationObserver in
    // novel-chapters.js: one per document, and the current element found
    // when it is needed. The `isConnected` check is what makes a reader
    // that has already left silent -- `setupButton` is not called on the
    // way out, so nothing else would clear this.
    let currentButton = null;
    let syncBound = false;

    function syncButton() {
        if (!currentButton?.isConnected) return;
        currentButton.textContent = isOn() ? "Exit full" : "Fullscreen";
    }

    /**
     * Wires the reader's toggle button. Hidden outright where the API
     * isn't available (iOS Safari on iPhone, notably) and where the installed
     * app is already chrome-free, since a button that
     * cannot do anything is worse than no button.
     */
    function setupButton(button) {
        if (!button) return;
        if (!supported || inAppFullscreen()) {
            button.classList.add("hidden");
            return;
        }
        currentButton = button;
        // On the BUTTON, so it goes away with the button -- the reason
        // this one needs no guard of its own.
        button.addEventListener("click", () => {
            const goingOn = !isOn();
            remember(goingOn);
            // A manual toggle IS the gesture, so nothing else in this
            // document should try to enter on top of it afterwards.
            autoEnterDone = true;
            goingOn ? enter() : exit();
        });
        if (!syncBound) {
            syncBound = true;
            document.addEventListener("fullscreenchange", syncButton);
        }
        syncButton();
    }

    /**
     * Makes the first gesture anywhere on this page re-enter fullscreen.
     *
     * Fullscreen belongs to a document, so every navigation drops out of
     * it and no amount of preference can bring it back before a gesture
     * happens. This is the closest a browser tab can get to "always
     * fullscreen": one tap in, on each page.
     *
     * The event list is not a matter of taste: only an "activation
     * triggering" event grants the transient activation requestFullscreen
     * demands, and for touch input `pointerdown` and `touchstart` are NOT
     * among them -- Chrome activates on the release. So this listens to
     * the same events the readers' own arming does, capture-phase so that
     * a handler calling stopPropagation cannot hide the gesture, and stays
     * registered until one attempt succeeds.
     *
     * Touch devices only. On a desktop browser a click on a link is not a
     * request to take over the screen; the reader's toggle is still there
     * for anyone who wants it.
     */
    const GESTURE_EVENTS = ["click", "touchend", "pointerup", "keydown"];
    const GESTURE_OPTS = { capture: true, passive: true };
    function onGesture() {
        armFromGesture().then(() => {
            if (autoEnterDone || userLeft) {
                for (const type of GESTURE_EVENTS) {
                    document.removeEventListener(type, onGesture, GESTURE_OPTS);
                }
            }
        });
    }
    function armGlobal() {
        if (!supported || !onTouchDevice() || inAppFullscreen()) return;
        for (const type of GESTURE_EVENTS) {
            document.addEventListener(type, onGesture, GESTURE_OPTS);
        }
    }

    return { setupButton, armFromGesture, armGlobal, wanted, remember, supported, inAppFullscreen };
})();
