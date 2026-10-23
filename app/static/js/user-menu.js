/**
 * Toggle behavior for the header's user dropdown (username / Settings /
 * Log out). Plain click-to-open, click-outside-or-Escape-to-close -- no
 * framework needed for a menu this small.
 */
(() => {
    // Once per document, with the elements looked up when used: the header
    // is inside the <body> a boosted visit replaces, and each run of this
    // file used to add another pair of document listeners holding the old
    // header (seventeen after four laps of the app, measured).
    if (window.userMenuBound) return;
    window.userMenuBound = true;

    const parts = () => ({
        menu: document.getElementById("user-menu"),
        btn: document.getElementById("user-menu-btn"),
        dropdown: document.getElementById("user-menu-dropdown"),
    });
    const isOpen = (dropdown) => !dropdown.classList.contains("hidden");

    function setOpen({ btn, dropdown }, open) {
        dropdown.classList.toggle("hidden", !open);
        btn.setAttribute("aria-expanded", String(open));
    }

    document.addEventListener("click", (e) => {
        const menu = parts();
        if (!menu.menu || !menu.btn || !menu.dropdown) return;
        if (menu.btn.contains(e.target)) setOpen(menu, !isOpen(menu.dropdown));
        else if (isOpen(menu.dropdown) && !menu.menu.contains(e.target)) setOpen(menu, false);
    });

    document.addEventListener("keydown", (e) => {
        const menu = parts();
        if (e.key === "Escape" && menu.dropdown && isOpen(menu.dropdown)) setOpen(menu, false);
    });
})();
