/**
 * Drag-and-drop manual reordering for a novel's chapter list, active only
 * while "Reorder manually" is turned on (see /novels/{id}/manual-sort/toggle
 * in app/novels/router.py) -- the template only marks rows draggable="true"
 * in that state.
 *
 * All the mechanics live in reorder.js, shared with the series item grid.
 * Each volume's <ul> is its own independent drop zone -- one Sortable
 * instance each, with its own group -- so a chapter can only ever be
 * reordered within its own volume, matching the reorder endpoint (which
 * only accepts a single volume's chapters at a time).
 */
(() => {
    // An IIFE, and not decoration: a swap re-executes this file in the SAME
    // document, and a top-level `const` would throw "already declared" on
    // the second run. series-reorder.js declares the same name, so the two
    // would collide the first time one document showed both pages.
    const spec = {
        containerSelector: 'ul[data-manual-sort="true"]',
        // li[...] rather than a bare [data-chapter-id]: each row also contains a
        // read/unread button carrying the same attribute -- see reorder.js's
        // itemsIn for what that used to do to the saved order.
        itemSelector: "li[data-chapter-id]",
        buildPayload: (container, items) => ({
            volume_id: parseInt(container.dataset.volumeId, 10),
            chapter_ids: items.map((r) => parseInt(r.dataset.chapterId, 10)),
        }),
    };

    // reorder.js is in base.html's <head>: a parse runs it before this
    // page's own deferred scripts, and a boosted visit finds it already run.
    window.makeReorderable(spec);
})();
