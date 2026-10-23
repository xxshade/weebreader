/**
 * Arms the whole-app fullscreen behaviour on this page (see fullscreen.js
 * for what it does, and why it cannot happen on load).
 *
 * A file of its own rather than a line at the bottom of fullscreen.js:
 * that module is the reader's toggle as well, and a module should not
 * start listening on the document merely because something imported it.
 * Every page that wants the behaviour loads this and says so.
 *
 * The readers load it too, on top of the arming they already do from
 * inside the chapter iframe -- events do not bubble out of an iframe (see
 * AGENTS.md), so those listeners cover the text and this one covers the
 * reader's own chrome. Both call the same once-per-document function.
 */
(() => {
    if (window.readerFullscreen) window.readerFullscreen.armGlobal();
})();
