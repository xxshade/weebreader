/**
 * The custom date range picker behind the statistics page's "Custom…"
 * button. The preset windows next to it are ordinary links that htmx turns
 * into a swap; only this one needs a script, because a range has no single
 * URL to link to until it has been picked.
 *
 * Every listener is DELEGATED from the document. The picker lives inside
 * #daily-activity, which htmx replaces wholesale on every window change --
 * bound listeners would die with it, and re-arming on a swap event would be a
 * second thing to keep in step with the swap. Delegation has no such state:
 * the panel is found at event time, and a swap simply leaves it closed
 * again, which is the right state after a window has just been applied.
 *
 * The week runs Sunday..Saturday. getDay() already numbers days that way,
 * so the leading blanks are exactly the first-of-month's getDay().
 *
 * Dates are handled as YYYY-MM-DD strings and only ever built from local
 * getFullYear/getMonth/getDate. toISOString() is UTC, so it names the wrong
 * day for anyone west of Greenwich for part of every day -- which is
 * precisely the "off by one day" a calendar cannot afford.
 */
(() => {
    // Once per document, like the rest of the page's delegated scripts: a
    // second copy re-run by a boosted visit toggled the panel shut on the
    // same click that opened it. Nothing here holds an element, so the
    // first copy serves every later visit.
    if (window.statsRangeBound) return;
    window.statsRangeBound = true;

    const MONTHS = ["January", "February", "March", "April", "May", "June",
                    "July", "August", "September", "October", "November", "December"];
    const DOW = ["S", "M", "T", "W", "T", "F", "S"];

    // Selection state, kept here rather than on the DOM because the panel's
    // whole grid is re-rendered on every hover and every click.
    let start = null;      // "YYYY-MM-DD", the first click
    let end = null;        // the second click
    let hover = null;      // previewed end while only a start is picked
    let month = null;      // {y, m} of the LEFT of the two months shown

    const iso = (y, m, d) =>
        `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;

    function parse(text) {
        const [y, m, d] = text.split("-").map(Number);
        return { y, m: m - 1, d };
    }

    function panel() {
        return document.getElementById("range-picker");
    }

    function root() {
        return document.getElementById("daily-activity");
    }

    function today() {
        return root()?.dataset.today || iso(new Date().getFullYear(),
                                            new Date().getMonth(), new Date().getDate());
    }

    /** Days between two ISO dates, inclusive of both ends. */
    function span(a, b) {
        const first = parse(a), last = parse(b);
        const ms = Date.UTC(last.y, last.m, last.d) - Date.UTC(first.y, first.m, first.d);
        return Math.round(ms / 86400000) + 1;
    }

    function addMonths({ y, m }, delta) {
        const d = new Date(y, m + delta, 1);
        return { y: d.getFullYear(), m: d.getMonth() };
    }

    /** The selection as an ordered pair, treating a hovered day as the end
     *  so the band previews before the second click lands. */
    function selection() {
        if (!start) return [null, null];
        const other = end || hover;
        if (!other) return [start, start];
        return other < start ? [other, start] : [start, other];
    }

    function renderMonth({ y, m }, max) {
        const cells = [];
        const lead = new Date(y, m, 1).getDay();
        const length = new Date(y, m + 1, 0).getDate();
        const [from, to] = selection();

        for (const label of DOW) cells.push(`<div class="cal-dow">${label}</div>`);
        for (let i = 0; i < lead; i++) cells.push('<div class="cal-cell"></div>');

        for (let day = 1; day <= length; day++) {
            const value = iso(y, m, day);
            const inRange = from && value >= from && value <= to;
            const cellClasses = ["cal-cell"];
            if (inRange) {
                cellClasses.push("is-in-range");
                // The band's ends are the ends of the SELECTION, but also the
                // ends of a WEEK and of the month: without that the tint would
                // run to the edge of the grid and start again on the next row
                // as one unbroken block.
                if (value === from || day === 1 || new Date(y, m, day).getDay() === 0) {
                    cellClasses.push("is-range-start");
                }
                if (value === to || day === length || new Date(y, m, day).getDay() === 6) {
                    cellClasses.push("is-range-end");
                }
            }
            const dayClasses = ["cal-day"];
            if (value === from || value === to) dayClasses.push("is-endpoint");
            if (value === today()) dayClasses.push("is-today");
            const disabled = value > max ? " disabled" : "";
            cells.push(
                `<div class="${cellClasses.join(" ")}">` +
                `<button type="button" class="${dayClasses.join(" ")}" data-cal-day="${value}"${disabled}>` +
                `${day}</button></div>`);
        }
        // Padded to a fixed six rows (42 cells, the most any month needs)
        // so navigating months never changes the panel's height, which
        // would make the charts below it jump.
        for (let i = lead + length; i < 42; i++) cells.push('<div class="cal-cell"></div>');
        return cells.join("");
    }

    function render() {
        const host = document.getElementById("cal-months");
        if (!host || !month) return;
        const max = today();
        const right = addMonths(month, 1);
        // The right-hand month stops AT the present month: every day past
        // today is disabled, so going further only ever produces a grid of
        // dead cells -- an arrow that appears to work and does nothing.
        const now = parse(max);
        const atMax = iso(right.y, right.m, 1) >= iso(now.y, now.m, 1);

        // No class on the wrapper: .cal-months' grid places it, and a class
        // with no rule is the trap app.css is guarded against.
        host.innerHTML = [month, right].map((shown, i) => `
            <div>
                <div class="cal-head">
                    ${i === 0
                        ? '<button type="button" class="cal-nav" data-cal-nav="-1" aria-label="Previous month">&lsaquo;</button>'
                        : '<span class="cal-nav" aria-hidden="true"></span>'}
                    <span class="cal-title">${MONTHS[shown.m]} ${shown.y}</span>
                    ${i === 1
                        ? `<button type="button" class="cal-nav" data-cal-nav="1" aria-label="Next month"${atMax ? " disabled" : ""}>&rsaquo;</button>`
                        : '<span class="cal-nav" aria-hidden="true"></span>'}
                </div>
                <div class="cal-grid">${renderMonth(shown, max)}</div>
            </div>`).join("");

        const summary = document.getElementById("cal-summary");
        const apply = document.querySelector('[data-cal-action="apply"]');
        const maxDays = Number(root()?.dataset.maxDays) || 366;
        if (summary) {
            if (!start) summary.textContent = "Pick a start date";
            else if (!end) summary.textContent = "Pick an end date";
            else {
                const days = span(start, end);
                summary.textContent = days > maxDays
                    ? `${start} → ${end} · ${days} days, showing the last ${maxDays}`
                    : `${start} → ${end} · ${days} day${days === 1 ? "" : "s"}`;
            }
        }
        if (apply) apply.disabled = !(start && end);
    }

    function open() {
        const box = panel();
        const toggle = document.getElementById("range-custom-toggle");
        if (!box) return;
        // Opens on the window already being shown, so a custom range can be
        // adjusted rather than re-entered from scratch.
        start = root()?.dataset.windowStart || null;
        end = root()?.dataset.windowEnd || null;
        hover = null;
        const anchor = parse(end || today());
        month = addMonths({ y: anchor.y, m: anchor.m }, -1);
        box.hidden = false;
        toggle?.setAttribute("aria-expanded", "true");
        render();
    }

    function close() {
        const box = panel();
        if (box) box.hidden = true;
        document.getElementById("range-custom-toggle")?.setAttribute("aria-expanded", "false");
    }

    document.addEventListener("click", (e) => {
        if (e.target.closest("#range-custom-toggle")) {
            if (panel() && !panel().hidden) close();
            else open();
            return;
        }

        const nav = e.target.closest("[data-cal-nav]");
        if (nav) {
            month = addMonths(month, Number(nav.dataset.calNav));
            render();
            return;
        }

        const day = e.target.closest("[data-cal-day]");
        if (day) {
            // A click always either starts a new range or closes the open
            // one -- never edits an endpoint of a finished range. Two clicks
            // is the whole grammar, and the alternative (nearest-endpoint
            // editing) makes the second click of a NEW range ambiguous.
            if (!start || end) {
                start = day.dataset.calDay;
                end = null;
            } else {
                const picked = day.dataset.calDay;
                [start, end] = picked < start ? [picked, start] : [start, picked];
            }
            hover = null;
            render();
            return;
        }

        if (e.target.closest('[data-cal-action="cancel"]')) {
            close();
            return;
        }

        if (e.target.closest('[data-cal-action="apply"]')) {
            if (!(start && end)) return;
            close();
            window.htmx?.ajax("GET", `/stats/daily-activity?from=${start}&to=${end}`,
                              { target: "#daily-activity", swap: "outerHTML" });
            return;
        }

        // Anywhere else closes it, the way a popover is expected to behave.
        if (panel() && !panel().hidden && !e.target.closest("#range-picker")) close();
    });

    document.addEventListener("mouseover", (e) => {
        if (!start || end) return;
        const day = e.target.closest("[data-cal-day]");
        if (!day || day.dataset.calDay === hover) return;
        hover = day.dataset.calDay;
        render();
    });

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && panel() && !panel().hidden) close();
    });
})();
