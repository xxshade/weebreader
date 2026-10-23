/**
 * The tooltip over the statistics page's daily charts ("Time read per day",
 * "Pages per day"), and the keyboard walk along them.
 *
 * Each day's column carries what to say in `data-tip`, rendered by the
 * server (see _daily_activity.html): the day, one row per kind read, and the
 * total. This only shows it -- next to the column under the pointer, or the
 * one the keyboard is on -- so there is nothing to compute here and nothing
 * that can disagree with the column's own aria-label.
 *
 * Everything is DELEGATED from the document: the charts live inside
 * #daily-activity, which htmx replaces whole whenever the window changes.
 * The guard is for the other swap -- a boosted visit re-runs this file in
 * the same document, and a second set of listeners would draw every tooltip
 * twice.
 *
 * Built with textContent throughout. The labels are the app's own today,
 * but a tooltip is exactly where a name from somewhere else ends up later.
 */
(() => {
    if (window.statsDailyTooltip) return;
    window.statsDailyTooltip = true;

    const COLUMN = "[data-daily-chart] > [data-tip]";
    let tip = null;
    let active = null;

    // One element per document, re-made if a swap of <body> took it away.
    // It carries `viz` so the palette's --series-N are defined inside it:
    // it lives on <body>, outside the chart's own .viz ancestor.
    function tipElement() {
        if (tip && tip.isConnected) return tip;
        tip = document.createElement("div");
        tip.className = "viz viz-tip";
        tip.setAttribute("role", "tooltip");
        tip.hidden = true;
        document.body.appendChild(tip);
        return tip;
    }

    function row(value, label, slot) {
        const line = document.createElement("div");
        line.className = "viz-tip-row";
        if (slot !== null) {
            const key = document.createElement("span");
            key.className = `viz-tip-key viz-${slot}`;
            line.appendChild(key);
        }
        const strong = document.createElement("strong");
        strong.textContent = value;
        const name = document.createElement("span");
        name.textContent = label;
        line.append(strong, name);
        return line;
    }

    function show(column) {
        let data;
        try {
            data = JSON.parse(column.dataset.tip);
        } catch {
            return;
        }
        if (active && active !== column) active.removeAttribute("data-active");
        active = column;
        column.setAttribute("data-active", "");

        const box = tipElement();
        box.replaceChildren();
        const day = document.createElement("div");
        day.className = "viz-tip-day";
        day.textContent = data.day;
        box.appendChild(day);
        const rows = data.rows || [];
        for (const [label, value, slot] of rows) {
            box.appendChild(row(value, label, slot));
        }
        // An empty day is its "nothing read" alone. With one kind the
        // total would only repeat that kind's number, so it earns a line
        // of its own from two kinds up.
        if (!rows.length) {
            box.appendChild(row(data.total, "", null));
        } else if (rows.length > 1) {
            const total = row(data.total, "total", null);
            total.classList.add("viz-tip-total");
            box.appendChild(total);
        }
        box.hidden = false;
        place(column, box);
    }

    // Above the column, centred on it, kept inside the viewport. Below it
    // only when there is no room above -- the columns sit near the top of
    // a phone screen once the page is scrolled to them.
    function place(column, box) {
        const r = column.getBoundingClientRect();
        const w = box.offsetWidth;
        const h = box.offsetHeight;
        const margin = 8;
        let left = r.left + r.width / 2 - w / 2;
        left = Math.max(margin, Math.min(left, window.innerWidth - w - margin));
        let top = r.top - h - margin;
        if (top < margin) top = r.bottom + margin;
        box.style.left = `${Math.round(left)}px`;
        box.style.top = `${Math.round(top)}px`;
    }

    function hide() {
        if (active) active.removeAttribute("data-active");
        active = null;
        if (tip) tip.hidden = true;
    }

    document.addEventListener("pointerover", (event) => {
        const column = event.target.closest?.(COLUMN);
        if (column) show(column);
        else if (active && !event.target.closest?.("[data-daily-chart]")) hide();
    });

    document.addEventListener("pointerleave", hide);

    // A tap elsewhere on a phone: there is no pointerout to rely on there.
    document.addEventListener("pointerdown", (event) => {
        if (active && !event.target.closest?.(COLUMN)) hide();
    });

    document.addEventListener("focusin", (event) => {
        const column = event.target.closest?.(COLUMN);
        if (column) show(column);
    });

    document.addEventListener("focusout", (event) => {
        if (event.target.closest?.(COLUMN)) hide();
    });

    // A scroll moves the column out from under a fixed tooltip.
    window.addEventListener("scroll", () => { if (active) hide(); }, { passive: true });

    // Roving tabindex: one column per chart is a tab stop, the arrows walk
    // the rest. Tabbing through ninety days one by one to get past a chart
    // is not a way to read it.
    document.addEventListener("keydown", (event) => {
        const column = event.target.closest?.(COLUMN);
        if (!column) return;
        const all = Array.from(column.parentElement.querySelectorAll(":scope > [data-tip]"));
        const at = all.indexOf(column);
        const next = { ArrowLeft: at - 1, ArrowRight: at + 1, Home: 0, End: all.length - 1 }[event.key];
        if (next === undefined) {
            if (event.key === "Escape") hide();
            return;
        }
        event.preventDefault();
        const target = all[Math.max(0, Math.min(next, all.length - 1))];
        for (const c of all) c.tabIndex = c === target ? 0 : -1;
        target.focus();
    });
})();
