/**
 * Drag-and-drop manual reordering for the series item grid, active only
 * while "Reorder manually" is turned on (see /series/{id}/manual-sort/toggle
 * in library/router.py) -- the template only marks cards draggable="true" in
 * that state, and the data-manual-sort condition baked into the container
 * selector below keeps this inert otherwise.
 *
 * All the mechanics live in reorder.js, shared with the novel chapter list.
 * A grid needs no declaration of its own any more -- SortableJS works the
 * geometry out from the elements, where the hand-written version needed to
 * be told whether "nearest" meant 2D or vertical-only.
 */
(() => {
    // An IIFE, and not decoration: a swap re-executes this file in the SAME
    // document, and a top-level `const` would throw "already declared" on
    // the second run. novel-reorder.js declares the same name, so the two
    // would collide the first time one document showed both pages.
    const spec = {
        containerSelector: '#items-grid[data-manual-sort="true"]',
        itemSelector: "div[data-item-id]",
        buildPayload: (container, items) => ({
            item_ids: items.map((c) => parseInt(c.dataset.itemId, 10)),
        }),
    };

    // reorder.js is in base.html's <head>: a parse runs it before this
    // page's own deferred scripts, and a boosted visit finds it already run.
    window.makeReorderable(spec);
})();
