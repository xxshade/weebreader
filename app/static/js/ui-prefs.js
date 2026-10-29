/**
 * The three display preferences the server renders the document from.
 *
 * They used to be localStorage plus an inline <script> in <head> racing the
 * parser to set an attribute before the first paint. localStorage is
 * invisible to the server, so a script was the only thing that could
 * correct the page. A cookie rides along with the document request, so
 * app/ui_prefs.py renders the attribute already right and nothing runs
 * before paint at all. The theme lands on <html>, which is the browser's
 * choice; the per-page two land on <main>, which is what a body swap
 * replaces.
 *
 * This file WRITES them; ui_prefs.py READS them. The names have to agree
 * and a test pins that they do.
 *
 * Loaded first among the deferred scripts (base.html), because the owners
 * that persist a change -- appearance.js, thumbnail-size.js,
 * novel-chapters.js -- call window.uiPrefs, and deferred scripts run in
 * document order.
 */
window.uiPrefs = (() => {
    // A year, and per browser rather than per account: which theme and how
    // big the covers are belong to the device you are reading on, which is
    // the same reason these were never on the server. SameSite=Lax because
    // nothing here is worth sending on a cross-site request. No Secure,
    // because the app is also served over plain HTTP in development and a
    // cookie the browser silently refuses is worse than one that leaks a
    // theme name. No HttpOnly either -- the scripts below have to write it.
    const MAX_AGE = 31536000;
    const THEME = "app-theme";
    const HOME_THUMB = "thumbnail-size";
    const UNREAD = "novel-chapters-unread-only";

    function read(name) {
        const prefix = `${encodeURIComponent(name)}=`;
        for (const part of document.cookie.split("; ")) {
            if (part.startsWith(prefix)) return decodeURIComponent(part.slice(prefix.length));
        }
        return null;
    }

    function write(name, value) {
        window.homePreload?.invalidate();
        document.cookie = `${encodeURIComponent(name)}=${encodeURIComponent(value)}` +
            `; path=/; max-age=${MAX_AGE}; samesite=lax`;
    }

    function remove(name) {
        window.homePreload?.invalidate();
        document.cookie = `${encodeURIComponent(name)}=; path=/; max-age=0; samesite=lax`;
    }

    // "Unread only" is per novel: one cookie per novel it is ON for, named
    // after the novel (ui_prefs.unread_only reads the same name).
    function unreadKey(novelId) {
        return `${UNREAD}:${novelId}`;
    }

    // Retire old theme storage while retaining thumbnail migration.
    function migrate() {
        document.documentElement.dataset.appTheme = "amoled";
        if (read(THEME) !== null) remove(THEME);
        try { localStorage.removeItem(THEME); } catch { /* unavailable storage */ }
        const pageThumb = document.querySelector("main")?.dataset.thumbSizeKey
            || HOME_THUMB;
        // The one cookie "Unread only" used to be for EVERY novel. It
        // cannot be carried to any one of them, so it goes: it would
        // otherwise sit there saying nothing, sent with every request.
        if (read(UNREAD) !== null) remove(UNREAD);
        for (const name of new Set([HOME_THUMB, pageThumb])) {
            if (read(name) !== null) continue;
            let stored = null;
            try {
                stored = localStorage.getItem(name);
            } catch {
                // localStorage unavailable (private mode etc.) -- nothing to carry
            }
            if (stored === null) continue;
            write(name, stored);
        }

    }

    migrate();
    return { read, write, remove, unreadKey };
})();
