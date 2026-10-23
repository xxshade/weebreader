/** Reading visits and account-scoped, acknowledged delivery.
 * Checkpoints extend a visit; they never create another page visit or cap.
 * Only ordinary navigation can finish a text pace sample. */
(() => {
    window.createReadingTimeTracker = function createReadingTimeTracker(options = {}) {
        const endpoint = options.endpoint || "/reading-time";
        const account = document.querySelector("main[data-reading-account]")?.dataset.readingAccount;
        const id = () => crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)),
            value => value.toString(16).padStart(2,'0')).join('');
        const session = id();
        const prefix = `reading-activity:v1:${account}:`;
        const memory = new Map();
        let current = null, lastPayload = null, anchor = null, claiming = null;
        let stopped = false, sending = false, lastClaim = -Infinity, lastFlush = 0;
        const now = () => anchor === null ? null : anchor + performance.now() / 1000;

        function notice(message) {
            let node = document.getElementById("reading-sync-status");
            if (!node) {
                node = document.createElement("div");
                node.id = "reading-sync-status";
                node.setAttribute("role", "status");
                node.style.cssText = "position:fixed;bottom:1rem;left:1rem;right:1rem;z-index:100;background:#262626;color:#fff;padding:.5rem;border-radius:.25rem;font-size:.8rem";
                document.body.append(node);
            }
            node.textContent = message;
        }

        function persist(value) {
            memory.set(value.id, value);
            try { localStorage.setItem(prefix + value.id, JSON.stringify(value)); }
            catch { notice("Reading activity could not be saved on this device. Keep this tab open to sync."); }
        }
        function pending() {
            const result = new Map(memory);
            try {
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    if (!key?.startsWith(prefix)) continue;
                    const value = JSON.parse(localStorage.getItem(key));
                    if (value && !value.rejected && (!result.has(value.id) || result.get(value.id).revision < value.revision)) result.set(value.id, value);
                }
            } catch { notice("Some reading activity could not be restored on this device."); }
            return [...result.values()].filter(v => !v.rejected).slice(0, 20);
        }
        function acknowledge(ack) {
            const local = memory.get(ack.id);
            if (local && local.revision <= ack.revision) memory.delete(ack.id);
            try {
                const value = JSON.parse(localStorage.getItem(prefix + ack.id));
                if (value && value.revision <= ack.revision) localStorage.removeItem(prefix + ack.id);
            } catch { notice("Reading activity was synced, but local storage could not be updated."); }
        }

        function snapshot() {
            if (!current) return;
            const intervals = current.intervals.map(v => [...v]);
            if (current.visibleSince !== null) intervals.push([current.visibleSince, now()]);
            if (!intervals.length) return;
            const signature = JSON.stringify([intervals,current.value.closed,current.value.completed,current.value.traversed_end]);
            if (signature === current.lastSignature) return;
            current.lastSignature = signature;
            persist({...current.value, intervals, revision: ++current.revision});
        }
        function pause() {
            if (!current || current.visibleSince === null) return;
            current.intervals.push([current.visibleSince, now()]);
            current.visibleSince = null;
            snapshot();
        }
        function begin(payload) {
            if (anchor === null || stopped) return;
            current = {value: {...payload, id: id(), session_id: session}, key: keyOf(payload),
                revision: 0, intervals: [], visibleSince: document.visibilityState === "hidden" ? null : now()};
        }
        function end(completed = false, traversedEnd = null) {
            if (!current) return;
            current.value.closed = true;
            current.value.completed = completed;
            if (traversedEnd) current.value.traversed_end = traversedEnd;
            pause();
            snapshot();
            current = null;
        }
        function keyOf(payload) {
            return JSON.stringify(payload);
        }
        async function activate(force = false) {
            if (stopped || document.visibilityState === "hidden" || !account) return;
            if (claiming) return claiming;
            if (!force && performance.now() - lastClaim < 1000) return;
            lastClaim = performance.now();
            claiming = (async () => {
                try {
                    const response = await fetch("/reading-session", {method: "POST", headers: {"Content-Type":"application/json"},
                        body: JSON.stringify({session_id: session, claim_id: id()})});
                    if (!response.ok) throw new Error("Session not confirmed");
                    const value = await response.json();
                    if (anchor === null) anchor = value.now - performance.now() / 1000;
                    if (!current && lastPayload && !stopped) begin(lastPayload);
                    await flush();
                } catch {
                    if (anchor === null) notice("Reading activity is waiting for a connection.");
                }
                finally { claiming = null; }
            })();
            return claiming;
        }
        async function flush(useBeacon = false) {
            if (!account || sending) return;
            const activities = pending();
            if (!activities.length) return;
            const body = JSON.stringify({activities});
            if (useBeacon && body.length < 60000 && navigator.sendBeacon) {
                // A beacon only confirms submission to the browser, never receipt.
                // Keep the outbox until a later acknowledged fetch.
                try { navigator.sendBeacon(endpoint, new Blob([body], {type:"application/json"})); }
                catch { /* Submission is unconfirmed; retain the outbox. */ }
                return;
            }
            sending = true;
            try {
                const response = await fetch(endpoint, {method:"POST", headers:{"Content-Type":"application/json"}, body,
                    keepalive: useBeacon && body.length < 60000});
                if (!response.ok) return;
                const value = await response.json();
                for (const ack of value.accepted || []) acknowledge(ack);
                for (const rejected of value.rejected || []) {
                    const v = activities.find(a => a.id === rejected);
                    if (v) persist({...v, rejected:true});
                    notice("Some reading activity could not be saved. Your reading position is saved separately.");
                }
            } catch { /* Persisted activity is retried after reconnect or reopening. */ }
            finally { sending = false; }
        }
        function at(payload, transition = {}) {
            if (!payload) return;
            lastPayload = payload;
            if (current && current.key === keyOf(payload)) return;
            end(transition.completed === true, transition.traversedEnd);
            begin(payload);
            if (anchor === null) activate();
            flush();
        }
        function onVisibilityChange() {
            if (document.visibilityState === "hidden") { pause(); flush(true); }
            else {
                activate(true);
                if (current && current.visibleSince === null && anchor !== null) current.visibleSince = now();
            }
        }
        function onPageHide() { pause(); flush(true); clearInterval(timer); }
        function onInteraction(event) { if (event.isTrusted) activate(); }
        function onOnline() { if (anchor === null) activate(true); flush(); }
        function tick() {
            snapshot();
            if (performance.now() - lastFlush >= 30000) { lastFlush = performance.now(); flush(); }
        }
        let timer = setInterval(tick, 5000);
        function onPageShow(event) {
            if (event.persisted && !stopped) {
                clearInterval(timer);
                timer = setInterval(tick, 5000);
                onVisibilityChange();
            }
        }
        document.addEventListener("visibilitychange", onVisibilityChange);
        window.addEventListener("pagehide", onPageHide);
        window.addEventListener("online", onOnline);
        window.addEventListener("pageshow", onPageShow);
        document.addEventListener("pointerdown", onInteraction, {passive:true});
        document.addEventListener("keydown", onInteraction);
        function stop() {
            end();
            flush(true);
            clearInterval(timer);
            document.removeEventListener("visibilitychange", onVisibilityChange);
            window.removeEventListener("pagehide", onPageHide);
            window.removeEventListener("online", onOnline);
            window.removeEventListener("pageshow", onPageShow);
            document.removeEventListener("pointerdown", onInteraction);
            document.removeEventListener("keydown", onInteraction);
            stopped = true;
        }
        activate(true);
        function finish() {
            end(true);
            lastPayload = null;
            flush();
        }
        return {at, stop, activate, flush, finish, interrupt: () => end(false)};
    };
    window.createReadingTimeTracker.protocolVersion = 2;
    window.createReadingTimeTracker.assetUrl = document.currentScript?.src;
})();
