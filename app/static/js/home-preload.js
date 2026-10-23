/** Reuse explicit press preloads only while their page and inputs are current. */
(() => {
    if (window.homePreload) return;
    let generation = 0;
    const pending = new Map();
    const cardFor = target => target.closest?.('#series-grid a.series-card[hx-preload], #user-menu-dropdown a[hx-preload]');

    function invalidate() {
        generation++;
        for (const card of pending.keys()) {
            if (card._htmx) delete card._htmx.preload;
        }
        pending.clear();
    }
    window.homePreload = { invalidate };

    function press(event) {
        const card = cardFor(event.target);
        if (!card) return;
        if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey
                || (event.type === 'mousedown' && event.button !== 0)
                || (event.touches && event.touches.length !== 1)) {
            return;
        }
        if (card._htmx?.preload?.expiresAt <= Date.now()) delete card._htmx.preload;
        const version = generation;
        // The native extension listens only to this explicit event. If this
        // guard has not loaded, ordinary links work without speculative reuse.
        card.dispatchEvent(new Event('home-card-press'));
        const entry = card._htmx?.preload;
        if (entry && version === generation && card.isConnected) {
            // A canceled press may leave the same native entry for another press.
            // Keep the inputs of its original request, including shared cookies.
            if (pending.get(card)?.entry !== entry) {
                pending.set(card, { entry, version, page: document.querySelector('main'),
                                    cookies: document.cookie });
            }
        }
    }
    document.addEventListener('mousedown', press, true);
    document.addEventListener('touchstart', press, { capture: true, passive: true });

    document.addEventListener('htmx:before:request', event => {
        const ctx = event.detail.ctx;
        const card = cardFor(ctx.sourceElement);
        if (!card) {
            if (ctx.request.method !== 'GET') invalidate();
            return;
        }
        const saved = pending.get(card);
        pending.delete(card);
        // The native extension's before-request hook has already selected its
        // cached promise. Choose a fresh fetch instead whenever it is unsafe.
        const current = () => saved && saved.version === generation
            && saved.page === document.querySelector('main') && card.isConnected
            && saved.entry.action === ctx.request.action && saved.cookies === document.cookie;
        ctx.fetch = async (url, options) => {
            // Expiry governs selection, not how long an accepted transfer takes.
            if (current() && Date.now() < saved.entry.expiresAt) {
                try {
                    const response = await saved.entry.prefetch;
                    if (current() && response.ok && !response.redirected
                            && response.headers.get('Content-Type')?.includes('text/html')) {
                        // Fetch resolves at headers. A library event can arrive
                        // while the body is streaming; keep the original response
                        // intact for htmx, then check validity after the transfer.
                        await response.clone().arrayBuffer();
                        if (current()) return response;
                    }
                } catch {
                    // A failed speculative request must not fail the real visit.
                }
            }
            return fetch(url, options);
        };
    });

    for (const name of ['htmx:sse:before:connection', 'htmx:sse:error', 'htmx:sse:close',
        'htmx:before:history:update']) document.addEventListener(name, invalidate);
    window.addEventListener('pagehide', invalidate);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') invalidate();
    });
})();
