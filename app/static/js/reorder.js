/**
 * Shared manual reordering, used by both the series item grid
 * (series-reorder.js) and a novel's chapter list (novel-reorder.js).
 *
 * This was hand-written against the native HTML5 drag-and-drop API, which
 * is a mouse API: `dragstart` does not fire from touch. So manual ordering
 * silently did not exist on a phone -- confirmed on Vivaldi for Android,
 * and measured here (a mouse drag fires one dragstart, a synthesized touch
 * drag fires none, on desktop and on an emulated phone alike). That is not
 * something to reimplement: SortableJS is the library for exactly this,
 * uses native DnD where it works and pointer events where it does not, and
 * is vendored beside htmx (see src/vendor/README.md).
 *
 * What is left here is what is genuinely ours: which containers are drop
 * zones, what counts as an item inside one, and the shape of the request
 * each endpoint wants.
 */
(() => {
    // Which container selectors have already been registered, for the whole
    // life of the DOCUMENT. reorder.js is loaded once from base.html's
    // <head>, but its CALLERS live with their pages, so a whole-<body> swap
    // re-executes them and each one calls makeReorderable again. Every call
    // used to install another document-wide MutationObserver: measured under
    // an hx-boost-style navigation, home -> series -> home -> novel -> home
    // -> series, 4 observers and climbing, and moving this file to <head>
    // did NOT change that (4 either way) -- the observer is created per
    // CALL, not per file.
    //
    // A second registration needs to do nothing: the first one's observer is
    // still watching, and it arms the containers the swap brought in.
    const registered = new Set();

    // Touch has to distinguish "drag this card" from "scroll the page",
    // and the only signal available is time: hold, then move. A mouse has
    // a dedicated button for it and needs no delay -- delayOnTouchOnly is
    // what keeps the desktop drag instant.
    const TOUCH_HOLD_MS = 200;

    /**
     * containerSelector -- CSS selector for a drop zone. Keep the
     *   "reordering is currently on" condition in it (e.g.
     *   '[data-manual-sort="true"]'): turning it on re-renders the page,
     *   so the selector is what decides whether anything is armed at all.
     * itemSelector      -- CSS selector for one item inside a container.
     * buildPayload      -- (container, itemElements) => request body. The
     *                      POST target is the container's data-reorder-url.
     */
    window.makeReorderable = function makeReorderable({ containerSelector, itemSelector, buildPayload }) {
        if (registered.has(containerSelector)) return;
        registered.add(containerSelector);

        // ":scope >" -- only DIRECT children count as items. Real bug this
        // fixes, inherited from the novel list's original standalone
        // version: a chapter row nests a read/unread <button> that carries
        // its own data-chapter-id (see novels/detail.html), so a plain
        // querySelectorAll("[data-chapter-id]") collected every chapter
        // TWICE. The endpoint's validation compares set(chapter_ids)
        // against the volume's ids, so the duplicates slipped through it,
        // and the surviving order was still monotonic -- but the offsets
        // then ran 0..2N-1 instead of 0..N-1, so the rewritten block was
        // twice as wide as the one that volume actually owned and spilled
        // into the NEXT volume's order_index range, which is exactly what
        // that endpoint documents itself as not doing.
        const itemsIn = (container) => Array.from(container.querySelectorAll(`:scope > ${itemSelector}`));

        function persistOrder(container) {
            fetch(container.dataset.reorderUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(buildPayload(container, itemsIn(container))),
            }).catch(() => {
                // Best-effort -- a failed save just means the order reverts to
                // the server's on next reload; nothing actionable to show for a
                // background reorder save.
            });
        }

        function arm(root) {
            if (!window.Sortable || !root || !root.querySelectorAll) return;
            const containers = root.matches && root.matches(containerSelector)
                ? [root]
                : Array.from(root.querySelectorAll(containerSelector));
            for (const container of containers) {
                // Re-arming an already-armed container stacks two
                // instances on it and saves the order twice per drop.
                //
                // OUR OWN flag on the element, not Sortable.get(). That
                // registry belongs to one copy of the library, and a swap
                // re-executes this page's <script src> for it -- so a
                // second copy arrives with an empty registry and is blind
                // to the instance the first one created. Measured under an
                // hx-boost-style navigation, second visit to the same
                // page: both containers carried TWO expandos,
                // Sortable<t1> and Sortable<t2>, i.e. exactly the stacking
                // this line exists to prevent. The element is the one
                // thing every copy agrees on.
                if (container.dataset.reorderArmed === "1") continue;
                container.dataset.reorderArmed = "1";
                window.Sortable.create(container, {
                    draggable: itemSelector,
                    animation: 120,
                    // A group name of its own per container, which is
                    // Sortable's default and the property a novel depends
                    // on: its volumes are separate <ul>s, and the reorder
                    // endpoint only accepts one volume's chapters at a
                    // time, so a chapter must not be draggable into the
                    // next volume.
                    delay: TOUCH_HOLD_MS,
                    delayOnTouchOnly: true,
                    // Without this, a finger that wobbles by a pixel during
                    // the hold cancels the drag before it starts.
                    touchStartThreshold: 5,
                    // The gap the dragged item leaves behind. Named
                    // explicitly because Sortable's default class
                    // (sortable-ghost) has no rule in the frozen app.css
                    // and would do nothing at all -- see AGENTS.md.
                    ghostClass: "opacity-40",
                    onEnd: (event) => {
                        if (event.oldIndex === event.newIndex) return; // picked up and put back
                        persistOrder(container);
                    },
                });
            }
        }

        // Containers come and go -- htmx swaps #items-grid wholesale on
        // mark-all-read/unread, and library-events.js replaces grids
        // itself -- and Sortable binds to an element, not to the document,
        // so a swapped-in grid arrives with no drag at all. Binding once at
        // load is therefore not enough.
        //
        // A MutationObserver rather than an htmx event. Under htmx 2 that
        // was forced: htmx:load fired for ITS OWN swaps, but
        // library-events.js swaps by outerHTML and calls htmx.process,
        // which did not fire it (measured: armed at load True, armed after
        // that swap False, and the grid did not drag). htmx 4 closes that
        // particular hole -- htmx:after:process fires once for every
        // subtree processed, htmx.process included -- so the observer is
        // now a choice rather than the only option, and it stays one:
        // watching the DOM covers every path, including the next one
        // nobody remembers to hook up.
        new MutationObserver((records) => {
            for (const record of records) {
                for (const node of record.addedNodes) {
                    if (node.nodeType === Node.ELEMENT_NODE) arm(node);
                }
            }
        }).observe(document.documentElement, { childList: true, subtree: true });

        // Sortable is in base.html's <head> ahead of this file, so it has
        // always run by now.
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", () => arm(document));
        } else {
            arm(document);
        }
    };
})();
