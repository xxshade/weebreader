// A button that shows and hides a panel in place: the order icon beside a
// series' or a novel's title and the controls it opens under it.
//
//   <button data-disclosure aria-controls="<id>" aria-expanded="...">
//
// The panel is the element `aria-controls` names; `hidden` is its state
// and `aria-expanded` says so. The server decides how each starts (both
// pages render theirs open while dragging), so this reads the panel
// rather than keeping a state of its own.
//
// Not a popover: nothing but the button closes it -- no click outside, no
// Escape -- because what is in it is used while looking at what is
// outside it (an order tried on the grid, a drag that needs "Done" in
// view).
//
// Delegated from the document, once per document, and the panel is looked
// up on each click: a boosted visit runs this file again and brings a new
// button and panel, and a listener per visit would toggle the panel open
// and straight back shut.
(() => {
    if (window.disclosureBound) return;
    window.disclosureBound = true;

    document.addEventListener("click", (event) => {
        const button = event.target.closest("[data-disclosure]");
        if (!button) return;
        const panel = document.getElementById(button.getAttribute("aria-controls"));
        if (!panel) return;
        const open = !panel.classList.toggle("hidden");
        button.setAttribute("aria-expanded", String(open));
    });
})();
