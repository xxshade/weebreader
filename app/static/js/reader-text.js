/**
 * Shared reflowable-text reader engine -- powers both the EPUB reader and
 * the web-novel reader. Originally two separate implementations (the EPUB
 * reader was epub.js-based, the novel reader fetched server-rendered
 * fragments via htmx-style appending); unified here because they'd
 * converged on the exact same design independently -- paragraph-indexed
 * position tracking, chapter-by-chapter iframe rendering -- and because
 * this app's own EPUB engine (custom-built, no third-party library) turned
 * out more capable than the novel reader's. Unifying also closes a real
 * gap: the old novel reader injected scraped, third-party HTML directly
 * into the page's own DOM (`{{ content_html | safe }}`); every chapter now
 * renders inside a sandboxed <iframe>, same isolation EPUB content already
 * got, instead of trusting a scraped page's markup to behave in the app's
 * own origin.
 *
 * Both "Paginated" and "Continuous scroll" modes render chapters the same
 * way: fetch raw HTML for one chapter, inject a <base> tag + this app's
 * stylesheet, and render it inside a sandboxed <iframe>. Position is
 * tracked identically in both modes as (spine index, paragraph index) --
 * "elidx:N".
 *
 * WHY NOT EPUB.JS (kept from the original EPUB-only version of this file,
 * since the same reasoning is exactly why Paginated mode is worth having
 * for novels too): a library that computes page boundaries as CSS-column
 * blocks sized to whatever fits the CURRENT device's viewport, always
 * starting fresh from the beginning of a chapter, can't guarantee a saved
 * position lands at the TOP of a page on a different screen -- only that
 * it's somewhere on the page that happens to contain it. Readers like
 * KOReader don't have this problem because their layout engine can start
 * a fresh page at an arbitrary position -- that's what the custom
 * paginator below does, for both content types now.
 *
 * PARAGRAPH-INDEXED POSITION ("elidx:N"): the Nth block (paragraph,
 * heading, list item, table, etc.) in the chapter's HTML, counted in
 * document order. That order never changes between devices -- only how
 * much SPACE each element takes up does -- which is why it's a reliable
 * cross-device anchor. What counts as a block is reader-blocks.js, for
 * both content types and both modes.
 *
 * SOURCE ADAPTER: the only thing that differs between the two content
 * types -- how to list chapters, fetch one chapter's HTML, resolve
 * relative references inside it, and save progress -- is isolated in the
 * `adapters` object below, picked via data-source on #reader-root. Every
 * page/scroll/pagination mechanism past that point is 100% shared.
 */
(async () => {
    const root = document.getElementById("reader-root");
    // A boosted visit retains the old head. Upgrade its shared recorder
    // before using the new protocol, without reloading the document.
    const trackerUrl = root?.closest('main')?.dataset.readingTrackerUrl;
    if (trackerUrl && (window.createReadingTimeTracker?.protocolVersion !== 2
            || window.createReadingTimeTracker.assetUrl !== new URL(trackerUrl,location.href).href)) {
        await new Promise(resolve => {
            const script = document.createElement('script');
            script.src = trackerUrl;
            script.onload = script.onerror = () => { script.remove(); resolve(); };
            document.head.append(script);
        });
        if (!root.isConnected) return;
    }

    // A tab opened before reader-blocks.js was loaded from <head> keeps
    // that old <head> through every boosted visit, and this engine cannot
    // lay out a page without it -- measured, the reader never rendered and
    // said nothing. One real load brings the current <head>. Only when the
    // tag is missing: one that is there but failed to load would reload
    // forever.
    if ((!window.readerBlocks && !document.querySelector('script[src*="/reader-blocks.js"]')) ||
            (!window.readerPagination && !document.querySelector('script[src*="/reader-pagination.js"]'))) {
        window.location.reload();
        return;
    }

    // ---- Listeners that end with this reader ----
    //
    // Under hx-boost the document is never unloaded: a boosted link
    // replaces <body>'s CHILDREN and leaves `document` and `window`
    // exactly as they were -- with every listener this file put on them,
    // closed over elements that are no longer in the page. Measured
    // before this, both browsers: leave the
    // comic reader by its own link, then press the arrow keys on the
    // library page -- five presses, four `POST /reader/3/progress`, and
    // the book nobody is reading goes from page 6 to page 9 and is
    // recorded as finished. Three visits also left 15 of these registered
    // where 5 belong.
    //
    // So nothing below binds to a target that outlives the page directly.
    // Two rules do it instead, and both are needed, because the two
    // boosted ways out are not the same event:
    //
    //   - every handler is wrapped in the `root.isConnected` check the
    //     boosted-exit save below used to do by hand. The first event
    //     after this reader has left the document unbinds the listener
    //     and does nothing else. That is what covers **Back**, which
    //     htmx's history cache answers without raising
    //     `before:history:update` at all.
    //   - and an exit this file can SEE drops them all at once, rather
    //     than leaving each one to wait for an event that may never come
    //     (`pagehide` in a document that is never unloaded). Measured
    //     over three visits, tallied on the library page each time:
    //     **none** left behind, against 5, 10 and 15 -- with 7 live
    //     while a reader is actually on screen.
    const outside = [];
    function bindOutside(target, name, handler, options) {
        const drop = () => target.removeEventListener(name, wrapped, options);
        const wrapped = (event) => {
            if (!root.isConnected) {
                drop();
                return;
            }
            handler(event);
        };
        outside.push(drop);
        target.addEventListener(name, wrapped, options);
    }
    function releaseOutside() {
        for (const drop of outside) drop();
        outside.length = 0;
    }

    const source = root.dataset.source || "epub";
    const itemId = root.dataset.itemId;
    const startLocationRaw = root.dataset.startCfi || undefined;
    const startSpineIndexRaw = root.dataset.startSpineIndex;
    const startSpineIndex = startSpineIndexRaw !== "" && startSpineIndexRaw !== undefined
        ? parseInt(startSpineIndexRaw, 10)
        : undefined;
    const stylesheetUrl = `${window.location.origin}${root.dataset.stylesheetUrl}`;

    // ---------- Display-only find-and-replace ----------
    // Rules come from /settings, per series (EPUB) or per novel, and are
    // applied to a chapter's HTML on the way in -- so the EPUB on disk and
    // the scraped `chapters.content_html` are never touched, and deleting
    // a rule puts the original text back with no re-import.
    //
    // Applied HERE, to the fetched string, rather than to the rendered
    // DOM: both modes measure what they render (continuous scroll takes
    // each section's height, the paginator counts blocks per page), and a
    // substitution that changed the text AFTER the measurement would
    // leave the layout describing text that is no longer on screen.
    //
    // Only the text BETWEEN tags is substituted. A blind replace over the
    // whole string would happily rewrite an attribute, a class name or a
    // URL, which is how a "purely visual" setting turns into a broken
    // chapter. <script> and <style> bodies are skipped for the same
    // reason -- they sit between tags but are not text anyone reads.
    // Consequences of working on the raw HTML, both accepted: a rule
    // cannot match across a tag boundary ("the <em>end</em>"), and it
    // matches the source spelling, so a rule containing & or < has to be
    // written the way the file writes it.
    const replacementRules = (() => {
        try {
            const parsed = JSON.parse(root.dataset.replacements || "[]");
            return Array.isArray(parsed) ? parsed.filter((r) => r && r.find) : [];
        } catch {
            return []; // malformed attribute: read the book unmodified
        }
    })();

    const SKIP_ELEMENT_BODIES = /<(script|style)\b[\s\S]*?<\/\1\s*>/gi;

    function applyReplacements(html) {
        if (!replacementRules.length) return html;
        return html.replace(
            new RegExp(`${SKIP_ELEMENT_BODIES.source}|>([^<]+)<`, "gi"),
            (match, _tag, text) => {
                if (text === undefined) return match; // a <script>/<style> body
                let out = text;
                // In order, each rule seeing the previous one's output --
                // which is why the settings list keeps them oldest first.
                for (const rule of replacementRules) {
                    out = out.split(rule.find).join(rule.replace || "");
                }
                return `>${out}<`;
            },
        );
    }

    // New positions are "elidx:N:OFFSET"; "elidx:N" still means block start. A
    // raw EPUB CFI ("epubcfi(...)") can still show up here from progress
    // saved before this app had its own paginator -- it can't be resolved
    // without epub.js, so it's treated the same as "no saved position"
    // (falls back to the start of the saved chapter). One-time precision
    // loss for whoever has old progress, never a crash.
    const pagination = window.readerPagination;
    const startPosition = pagination.parse(startLocationRaw);

    // ---------- Source adapters ----------
    // Each adapter resolves: where to list chapters, where to fetch one
    // chapter's raw HTML, what base href relative references inside it
    // should resolve against, where to save progress (and in what shape --
    // the two backends have genuinely different progress schemas, not
    // just different URLs), and an optional hook for a source-specific
    // side effect when the reader moves past a chapter.
    const markedChapters = new Set(); // novel adapter only -- avoids re-POSTing the same chapter id repeatedly

    // The hash of the file this item is, read off the manifest and put on
    // every chapter URL as `?v=`. It is what lets those be answered
    // `immutable`: /reader/7/resource/ch1.xhtml goes on naming the same
    // slot inside item 7 after the file behind it is replaced, so without
    // a version the browser would keep serving the old book. Empty until
    // the manifest has been read, which is before any chapter is fetched.
    //
    // Images and stylesheets the chapter itself references do NOT get it:
    // they resolve against a `<base href>`, and a relative URL drops the
    // query. Those revalidate against their ETag instead, which is the
    // honest answer for a URL with no version in it -- see
    // reader/router.py's _content_cache_headers.
    let contentVersion = "";
    const timingSourceVersions = new Map();
    const versionQuery = () => (contentVersion ? `?v=${encodeURIComponent(contentVersion)}` : "");

    const adapters = {
        epub: {
            manifestUrl: `/reader/${itemId}/manifest`,
            resourceUrl: (entry) => `/reader/${itemId}/resource/${entry.href}${versionQuery()}`,
            baseHrefFor: (entry) => {
                const href = entry.href;
                const baseDir = href.includes("/") ? href.slice(0, href.lastIndexOf("/") + 1) : "";
                return `${window.location.origin}/reader/${itemId}/resource/${baseDir}`;
            },
            normalizeSpine: (data) => (data.spine || []).map((e) => ({ href: e.href, title: e.title, size: e.size })),
            progressUrl: `/reader/${itemId}/progress`,
            // An EPUB item's percentage is of the whole item, so the
            // chapter fraction the novel adapter needs is ignored here.
            buildProgressPayload: (spineIndex, entry, elidx, fraction, chapterFraction, offset = 0) => ({
                current_location: `elidx:${elidx}:${offset}`,
                current_page: spineIndex,
                progress_percent: Math.round(fraction * 100),
                status: (customMode ? fraction >= 0.98 : pgFinished) ? "completed" : "reading",
            }),
            // An EPUB has no chapter rows of its own, so a position is the
            // item plus where in its spine -- section_index is what keeps
            // paragraph 3 of chapter 1 distinct from paragraph 3 of
            // chapter 2 (see page_reads in schema.sql).
            buildTimingPayload: (spineIndex, entry, elidx) => ({
                item_id: Number(itemId),
                section_index: spineIndex,
                page_index: elidx,
            }),
            onChapterAdvance: null,
        },
        novel: {
            manifestUrl: `/novels/${itemId}/manifest`,
            resourceUrl: (entry) => `/novels/${itemId}/resource/${entry.chapterId}`,
            baseHrefFor: (entry) => entry.sourceUrl || window.location.origin,
            normalizeSpine: (data) => (data.spine || []).map((e) => ({
                href: String(e.chapter_id), chapterId: e.chapter_id, sourceUrl: e.source_url, title: e.title, size: e.size,
            })),
            progressUrl: `/novels/${itemId}/progress`,
            // Both fractions: `fraction` is of the whole novel, which is
            // what the reading-rate learning measures a novel with;
            // `chapterFraction` is of this chapter alone, which is what
            // the cards show. See save_novel_progress.
            buildProgressPayload: (spineIndex, entry, elidx, fraction, chapterFraction, offset = 0) => ({
                chapter_id: entry.chapterId,
                scroll_percent: Math.round(fraction * 10000) / 100,
                chapter_percent: Math.round(chapterFraction * 10000) / 100,
                paragraph_index: elidx,
                character_offset: offset,
            }),
            // A web novel chapter is its own row, so it names itself (the
            // server reads its novel) and needs no section index.
            buildTimingPayload: (spineIndex, entry, elidx) => ({
                chapter_id: entry.chapterId,
                page_index: elidx,
            }),
            // Marks a chapter read as soon as the reader moves past it
            // (crosses into the next chapter) in either mode -- simpler
            // and more consistent across Paginated/Continuous scroll than
            // the old novel reader's "scrolled past 90% of this one
            // chapter's own height" heuristic, which had no equivalent
            // concept in Paginated mode at all.
            //
            // `readToEnd` is the chapter the reader just crossed OUT of by
            // reading on: its saved position is where its last PAGE began,
            // short of the end by a page, and recorded as how far it was
            // read it understated the reading rate by (P-1)/P. Chapters
            // jumped over are left to the server's own default.
            onChapterAdvance: (completedEntry, readToEnd) => {
                const chapterId = completedEntry.chapterId;
                if (!chapterId || markedChapters.has(chapterId)) return;
                markedChapters.add(chapterId);
                fetch(`/novels/chapters/${chapterId}/read-status`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(readToEnd ? { read: true, read_from_percent: 100 } : { read: true }),
                    keepalive: true,
                }).catch(() => {});
            },
        },
    };
    const adapter = adapters[source] || adapters.epub;
    // Asked once, on a back/forward-cache restore -- the same endpoint
    // progress is posted to, read rather than written. See
    // reader-resume.js; a novel is sent somewhere else than where it
    // asked from, because its reader is opened per chapter.
    window.readerResume?.watch(adapter.progressUrl, root.dataset.startPercent, [startSpineIndex || 0, startPosition.index, startPosition.offset]);

    // Times each page visit and posts the spans (see reading-time.js).
    const readingTime = Object.assign({activate() {},interrupt() {},finish() {}}, createReadingTimeTracker());
    let lastAdvanceCheckSpineIndex = startSpineIndex;

    function notifyChapterAdvance(newSpineIndex) {
        if (!adapter.onChapterAdvance) return;
        if (lastAdvanceCheckSpineIndex !== undefined && newSpineIndex > lastAdvanceCheckSpineIndex && spineList) {
            for (let i = lastAdvanceCheckSpineIndex; i < newSpineIndex; i++) {
                if (spineList[i]) adapter.onChapterAdvance(spineList[i], i === newSpineIndex - 1);
            }
        }
        lastAdvanceCheckSpineIndex = newSpineIndex;
    }

    const PREFS_KEY = "text-reader-prefs"; // shared by both content types
    // on purpose -- one set of typography/theme choices for anything read
    // in this app, not two separate ones to keep in sync. Device-level
    // display preference, not reading progress (that syncs via the
    // server) -- localStorage is appropriate here.
    const PREF_DEFAULTS = { theme: "amoled", font: "original", fontSize: 100, lineHeight: 1.6, width: 720, flow: "paginated", bottomGap: 0 };

    function loadPrefs() {
        try {
            return Object.assign({}, PREF_DEFAULTS, JSON.parse(localStorage.getItem(PREFS_KEY)) || {}, { theme: "amoled" });
        } catch {
            return Object.assign({}, PREF_DEFAULTS);
        }
    }
    function savePrefs(p) {
        try {
            localStorage.setItem(PREFS_KEY, JSON.stringify(p));
        } catch {
            // localStorage unavailable (private mode etc.) -- proceed without persisting
        }
    }
    const prefs = loadPrefs();

    // These are work-specific, unlike the device-wide font/theme controls.
    // Never add discretionary hyphen characters: text offsets and verified
    // reading passages must continue to name the original DOM text.
    const workPrefsKey = `text-work-prefs:${root.dataset.readerUserId || 'local'}:${source}:${itemId}`;
    function canonicalLanguage(value) {
        if (!value || typeof value !== 'string') return '';
        try {
            const tag = Intl.getCanonicalLocales(value.trim().replace(/_/g, '-'))[0] || '';
            return /^(und|mul|zxx)(-|$)/i.test(tag) ? '' : tag;
        } catch { return ''; }
    }
    let workPrefs = {language: ''};
    let legacyHyphenation = false;
    try {
        const saved = JSON.parse(localStorage.getItem(workPrefsKey)) || {};
        workPrefs = {language: canonicalLanguage(saved.language)};
        legacyHyphenation = saved.hyphenation === true;
    } catch { /* unavailable storage or an older malformed preference */ }
    // Seed the shared toggle once, from the first work opened after upgrade.
    if (typeof prefs.hyphenation !== 'boolean') prefs.hyphenation = legacyHyphenation;
    savePrefs(prefs);
    let metadataLanguage = '';
    let currentChapterLanguage = '';
    function saveWorkPrefs() {
        try { localStorage.setItem(workPrefsKey, JSON.stringify(workPrefs)); } catch { /* device-local only */ }
    }
    function applyChapterLanguage(doc) {
        const html = doc.documentElement;
        if (!html.hasAttribute('data-reader-source-language')) {
            html.dataset.readerSourceLanguage = canonicalLanguage(html.getAttribute('lang') || html.getAttribute('xml:lang'))
                || canonicalLanguage(doc.body.getAttribute('lang') || doc.body.getAttribute('xml:lang'));
            html.dataset.readerSourceBodyLanguage = doc.body.getAttribute('lang') || '';
        }
        const language = workPrefs.language || html.dataset.readerSourceLanguage || metadataLanguage;
        if (language) html.setAttribute('lang', language); else html.removeAttribute('lang');
        // A body-level language must not defeat an explicit work override.
        if (workPrefs.language) doc.body.setAttribute('lang', workPrefs.language);
        else if (html.dataset.readerSourceBodyLanguage) doc.body.setAttribute('lang', html.dataset.readerSourceBodyLanguage);
        else doc.body.removeAttribute('lang');
        if (prefs.hyphenation) html.dataset.readerHyphenation = language ? 'auto' : 'manual';
        else delete html.dataset.readerHyphenation;
        currentChapterLanguage = language;
        renderWorkPrefs();
    }
    function renderWorkPrefs() {
        if (!root.isConnected) return;
        const enabled = document.getElementById("hyphenation-enabled");
        const language = document.getElementById("hyphenation-language");
        const status = document.getElementById("hyphenation-status");
        if (!enabled || !language || !status) return;
        enabled.checked = prefs.hyphenation;
        if (document.activeElement !== language) language.value = workPrefs.language;
        const effective = workPrefs.language || currentChapterLanguage || metadataLanguage;
        status.textContent = !prefs.hyphenation ? 'Using the book’s original hyphenation rules.'
            : effective ? `Language: ${effective}. Hyphenation depends on this browser’s dictionary.`
                : 'Set a language above to enable automatic hyphenation.';
    }

    // `shell` is the reader's own chrome (top bar, settings panel, page-nav
    // and whatever the content iframe doesn't cover) -- separate from
    // `background` because those elements are transparent over <body> and
    // carry FIXED light text (text-neutral-100/400), so the shell has to
    // stay dark whatever the content theme is. Light and dark therefore
    // keep exactly the neutral-950 the templates already set; only AMOLED
    // changes, and it changes for the reason AMOLED exists: at #0a0a0a the
    // top bar is still lighting pixels on an OLED panel, so a theme sold as
    // "true black" was showing a visibly non-black band across the top of
    // every page (measured: 4.4% of the viewport, 17.2% with the settings
    // panel open).
    const THEMES = {
        // true black -- real savings on OLED screens
        amoled: { background: "#000000", color: "#d0d0d0", shell: "#000000" },
    };

    // Everything outside the content iframe that has to follow the theme.
    // Called on load AND on every theme change -- customScrollEl used to be
    // painted once at startup only, so switching light -> AMOLED mid-read
    // left a white container leaking around the mounted chapters until a
    // reload.
    function applyShellTheme() {
        const t = THEMES[prefs.theme];
        document.body.style.backgroundColor = t.shell;
        if (customScrollEl) {
            customScrollEl.style.background = t.background;
            // Inherited by the end-of-book footer, which is shell markup
            // rather than book content -- so it follows a theme change
            // like everything else instead of staying the old colour.
            customScrollEl.style.color = t.color;
        }
    }

    function themeCssText() {
        const t = THEMES[prefs.theme];
        const fontFamily = prefs.font === "original" ? "inherit" : `'${prefs.font}', serif`;
        // Gecko Android accepts auto/none here, not the 100% value in the
        // shared stylesheet. Its inflation can enlarge long flow roots while
        // leaving short paragraphs small. CSS font sizes and zoom still apply.
        return `html { -moz-text-size-adjust: none; } ` +
            `html, body { overflow-y: auto !important; overflow-x: hidden !important; } ` +
            `body { background: ${t.background} !important; color: ${t.color} !important; ` +
            `font-family: ${fontFamily} !important; font-size: ${prefs.fontSize}% !important; ` +
            `line-height: ${prefs.lineHeight} !important; max-width: ${prefs.width}px !important; ` +
            `margin-left: auto !important; margin-right: auto !important; box-sizing: border-box; } ` +
            // Baseline paragraph/image spacing -- deliberately NOT
            // !important: scraped novel content has no styling of its own
            // (Tailwind's preflight, loaded via the stylesheet link below,
            // zeroes out default <p> margins), so this is what gives it
            // any spacing at all. An EPUB's own stylesheet (loaded from
            // the book itself, injected further below in <head> so it
            // wins the cascade) is free to override this with the
            // author's actual intended typography.
            //
            // img max-width/height: keeps a wide image from overflowing
            // horizontally (scaled down proportionally instead). A TALL
            // image -- e.g. an EPUB's cover, on desktop where it can
            // easily exceed the viewport height -- still can't shrink
            // further without becoming illegibly small, which is exactly
            // why html/body above allow vertical scrolling: a page too
            // tall to fit is shown as its own page (see renderPage in the
            // JS), and this is what actually lets the rest of it be
            // reached by scrolling instead of being silently clipped.
            `p { margin: 0 0 1.1em 0; } img { max-width: 100%; height: auto; border-radius: 4px; margin: 1em 0; }` +
            `@layer reader-work-preferences { html[data-reader-hyphenation="auto"] body, html[data-reader-hyphenation="auto"] body * { hyphens: auto !important; -webkit-hyphens: auto !important; } ` +
            `html[data-reader-hyphenation="manual"] body, html[data-reader-hyphenation="manual"] body * { hyphens: manual !important; -webkit-hyphens: manual !important; } ` +
            `html[data-reader-hyphenation] pre, html[data-reader-hyphenation] pre *, html[data-reader-hyphenation] code, html[data-reader-hyphenation] code * { hyphens: manual !important; -webkit-hyphens: manual !important; } } ` +
            `[data-reader-continuation-hyphen]::after { content: var(--reader-continuation-character, "\\2010"); }`;
    }

    // ---------- Shared: spine list + chapter document building ----------

    let spineList = null;
    async function fetchSpine() {
        if (!spineList) {
            const res = await fetch(adapter.manifestUrl);
            const data = await res.json();
            // Before normalizeSpine, so the first chapter URL built from
            // this spine already carries it.
            contentVersion = data.version || "";
            metadataLanguage = canonicalLanguage(data.language);
            renderWorkPrefs();
            spineList = adapter.normalizeSpine(data);
            buildSpineWeights();
        }
        return spineList;
    }

    // How much of the whole book each chapter accounts for. Progress used
    // to be (chapterIndex + fractionWithinChapter) / chapterCount, i.e.
    // every spine entry weighing the same however little it holds -- so on
    // a normal book, paging through the cover, title page, copyright and
    // TOC (one line each) reported 29% read before the first word of
    // chapter one. Measured, not assumed: that number came from driving
    // the real reader through a 4-entry front matter + 10 chapters.
    //
    // `size` is bytes of markup, so it only approximates how much PROSE an
    // entry holds -- fine for a progress bar, and vastly closer than
    // treating a one-line cover as a whole chapter. An entry with no size
    // (an orphan EPUB href; a novel chapter whose text hasn't been fetched
    // yet) gets the average of the ones that do have one, so it counts as
    // a typical chapter rather than as nothing; if the backend reports no
    // sizes at all, every weight is 1 and this degrades exactly to the old
    // equal-weight formula.
    let spineOffsets = null;   // weight accumulated BEFORE each chapter
    let spineWeights = null;
    let spineTotalWeight = 0;
    function buildSpineWeights() {
        const known = spineList.map((e) => (Number.isFinite(e.size) && e.size > 0 ? e.size : 0));
        const positives = known.filter((n) => n > 0);
        const fallback = positives.length ? positives.reduce((a, b) => a + b, 0) / positives.length : 1;
        spineWeights = known.map((n) => (n > 0 ? n : fallback));
        spineOffsets = [];
        let acc = 0;
        for (const w of spineWeights) {
            spineOffsets.push(acc);
            acc += w;
        }
        spineTotalWeight = acc;
    }

    /** Fraction of the whole book at `fraction` through chapter `spineIndex`. */
    function bookFraction(spineIndex, fraction) {
        if (!spineTotalWeight || !spineWeights || spineIndex == null || !spineWeights[spineIndex]) {
            return spineList && spineList.length ? (spineIndex + fraction) / spineList.length : 0;
        }
        return (spineOffsets[spineIndex] + fraction * spineWeights[spineIndex]) / spineTotalWeight;
    }

    function buildSectionDoc(rawHtml, baseHref) {
        // <base> fix: without it, any relative reference the browser
        // resolves on its own inside the iframe (including from a
        // stylesheet) would use THIS page's URL as its base instead of
        // the resource's actual location, producing 404s at the wrong
        // path (EPUB) or pointing at this app's own domain instead of the
        // source site (novels).
        const injected =
            `<base href="${baseHref}">` +
            `<link rel="stylesheet" href="${stylesheetUrl}">` +
            `<style id="reader-theme-style">${themeCssText()}</style>`;
        const doc = new DOMParser().parseFromString(rawHtml, 'text/html');
        applyChapterLanguage(doc);
        doc.head.insertAdjacentHTML('afterbegin', injected);
        // Relative CSS in srcdoc can trigger an extra request despite <base>.
        // Resolve EPUB stylesheets before attaching the document to avoid it;
        // imports and images still resolve against the CSS URL.
        if (source === "epub") {
            for (const link of doc.querySelectorAll('link[rel~="stylesheet"][href]')) {
                try {
                    link.setAttribute('href', new URL(link.getAttribute('href'), baseHref).href);
                } catch {
                    // An invalid source URL must not prevent opening the chapter.
                }
            }
        }
        return doc.documentElement.outerHTML;
    }

    let userHasInteracted = false;
    function markInteraction() { userHasInteracted = true; readingTime.activate(); }

    // Registered on every iframe this file creates, in either mode -- the
    // rendered content lives inside an isolated <iframe>, so touch/wheel/
    // keydown don't bubble out to the parent document on their own.
    function wireIframeInteractions(iframe) {
        const doc = iframe.contentDocument;
        if (!doc) return;
        doc.addEventListener("wheel", markInteraction, { passive: true });
        doc.addEventListener("touchstart", markInteraction, { passive: true });
        doc.addEventListener("touchstart", handleTouchStart);
        doc.addEventListener("touchend", handleTouchEnd);
        doc.addEventListener("keydown", handleKeydown);
        // A click covers the mouse (a touch arrives as touchend above, and
        // handleTouchEnd arms it there). Registered on the iframe as well
        // as on the top document because a tap while reading lands on the
        // chapter, which is a separate document that doesn't bubble out.
        doc.addEventListener("click", readerFullscreen.armFromGesture);
    }

    const loadingIndicator = document.getElementById("loading-indicator");
    const loadWarning = document.getElementById("load-warning");
    const loadWarningText = document.getElementById("load-warning-text");
    document.getElementById("load-warning-dismiss").addEventListener("click", () => {
        loadWarning.classList.add("hidden");
    });

    function showLoadWarning(message) {
        console.warn("[reader]", message);
        loadWarningText.textContent = message;
        loadWarning.classList.remove("hidden");
    }

    // A chapter that did not arrive. A network failure says so and that
    // it is tried again; only a refusal from the server is worth the hint
    // about malformed files -- the hint on a dropped connection sent a
    // reader off to re-save a perfectly good EPUB in Sigil.
    function warnChapterNotLoaded(err) {
        const offline = err && (err.name === "TypeError" || err.name === "AbortError");
        showLoadWarning(offline
            ? "Couldn't load a chapter -- check the connection; it is tried again when you go on reading."
            : "Couldn't load a chapter. " + MALFORMED_HINT);
    }

    const MALFORMED_HINT = source === "epub"
        ? "This usually happens with EPUBs that have malformed HTML (common in conversions/scrapes). " +
          "Opening the file in Sigil and saving it (it restructures things automatically) usually fixes it."
        : "This can happen if the chapter hasn't finished downloading yet -- try syncing this novel again from Settings.";

    function clearLoadingIndicator() {
        clearTimeout(loadingTimeout);
        clearTimeout(stuckTimeout);
        if (loadingIndicator) loadingIndicator.remove();
    }
    const loadingTimeout = setTimeout(() => {
        if (loadingIndicator) loadingIndicator.textContent = "Still loading… large books can take longer, especially on mobile.";
    }, 8000);
    const stuckTimeout = setTimeout(() => {
        if (loadingIndicator && document.body.contains(loadingIndicator)) {
            loadingIndicator.textContent = "Couldn't load the content. " + MALFORMED_HINT;
        }
    }, 25000);

    const indicator = document.getElementById("page-indicator");
    const clock = document.getElementById("reader-clock");

    // HH:MM beside the page counter, refreshed only when the page changes
    // (updateIndicator is the one place that runs, in both modes). Turning
    // a page is exactly when the time is worth a glance, and a per-minute
    // timer would be a wake-up every 60s for a value nobody is looking at.
    //
    // Built from the Date rather than toLocaleTimeString: `hour12: false`
    // still renders 24:07 under an h24 locale, and what was asked for is
    // HH:MM.
    function updateClock() {
        if (!clock) return;
        const now = new Date();
        clock.textContent = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
    }
    updateClock();

    // ---------- CUSTOM continuous scroll ----------
    // Each chapter becomes its own <iframe>, stacked inside #custom-scroll.
    //
    // Every placeholder is measured BEFORE it enters the flow (mounted
    // absolutely positioned + invisible at its final width, then measured,
    // then revealed at its real height) -- this is what makes the scroll
    // position never jump: there's nothing left to compensate for, since
    // the space was already reserved at the right size from the start.
    // Only mounting/retreating across a chapter boundary that inserts
    // content ABOVE the current position still needs a scrollTop
    // compensation (see retreatMount) -- symmetric to the same technique
    // used in the comic reader for pages that grow above the current one.
    let customMode = false;
    let customScrollEl = document.getElementById("custom-scroll");
    let sectionWrappers = []; // one <div> per spine index, in order
    let mountedFrom = -1; // smallest index mounted so far (with real height)
    let mountedUpTo = -1; // largest index mounted so far (with real height)
    let bottomSentinelEl = null;
    let topSentinelEl = null;
    let bottomSentinelObserver = null;
    let topSentinelObserver = null;
    let customHasNavigated = false;
    let customReflowing = false;
    let customReflowGeneration = 0;

    function reapplyCustomTheme() {
        const css = themeCssText();
        for (const wrapper of sectionWrappers) {
            const iframe = wrapper && wrapper.querySelector("iframe");
            const styleEl = iframe && iframe.contentDocument && iframe.contentDocument.getElementById("reader-theme-style");
            if (styleEl) {
                applyChapterLanguage(iframe.contentDocument);
                styleEl.textContent = css;
            }
        }
    }

    async function remeasureCustomSections(position) {
        const generation = ++customReflowGeneration;
        customReflowing = true;
        cancelScrollSave();
        for (const wrapper of sectionWrappers) {
            const iframe = wrapper?.querySelector('iframe');
            if (!iframe || wrapper.dataset.mounted !== '1') continue;
            await waitForFonts(iframe.contentDocument);
            if (generation !== customReflowGeneration || !customMode || !root.isConnected) {
                if (generation === customReflowGeneration) customReflowing = false;
                return;
            }
            // A fixed old frame height otherwise becomes the document's
            // minimum height, hiding shrinkage after a typography change.
            iframe.style.height = '1px';
            const height = Math.ceil(iframe.contentDocument.documentElement.scrollHeight);
            iframe.style.height = `${height}px`;
            wrapper.style.height = `${height}px`;
        }
        if (generation !== customReflowGeneration || !customMode || !root.isConnected) {
            if (generation === customReflowGeneration) customReflowing = false;
            return;
        }
        scrollToCustomPosition(position.spineIndex, {index: position.elementIndex, offset: position.offset || 0});
        requestAnimationFrame(() => requestAnimationFrame(() => {
            if (generation !== customReflowGeneration) return;
            customReflowing = false;
            reportContinuousTiming(false);
        }));
    }

    function ensureWrappers() {
        for (let i = 0; i < spineList.length; i++) {
            if (!sectionWrappers[i]) {
                const wrapper = document.createElement("div");
                wrapper.dataset.index = String(i);
                wrapper.style.position = "relative";
                wrapper.style.overflow = "hidden";
                wrapper.style.height = "0px";
                customScrollEl.appendChild(wrapper);
                sectionWrappers[i] = wrapper;
            }
        }
    }

    async function mountSection(index) {
        const wrapper = sectionWrappers[index];
        if (!wrapper || wrapper.dataset.mounted === "1") return wrapper;
        wrapper.dataset.mounted = "pending";

        const entry = spineList[index];
        let html;
        try {
            const res = await fetch(adapter.resourceUrl(entry));
            if (!res.ok) throw new Error("HTTP " + res.status);
            timingSourceVersions.set(index,res.headers.get('X-Reading-Source') || res.headers.get('ETag'));
            html = applyReplacements(await res.text());
        } catch (err) {
            // NOT mounted, so it is fetched again -- see advanceMount.
            warnChapterNotLoaded(err);
            delete wrapper.dataset.mounted;
            return null;
        }

        const iframe = createChapterIframe();
        iframe.setAttribute("scrolling", "no");
        iframe.style.cssText = "position:absolute; top:0; left:0; width:100%; border:none; visibility:hidden; display:block;";
        wrapper.appendChild(iframe);
        iframe.srcdoc = buildSectionDoc(html, adapter.baseHrefFor(entry));

        if (!await waitForIframeLoad(iframe)) {
            iframe.remove();
            delete wrapper.dataset.mounted;
            if (customMode && root.isConnected) {
                showLoadWarning("Couldn't prepare a chapter. Please try opening the reader again.");
                clearLoadingIndicator();
            }
            return null;
        }
        // A chapter's height is read ONCE and becomes the wrapper's height
        // for the rest of the session, so anything that arrives after the
        // measurement is clipped by the iframe for good -- the same
        // late-font and late-image problem the paginator has, except here
        // there is no page turn to repaginate it away.
        try {
            await waitForImages(iframe.contentDocument);
            await waitForFonts(iframe.contentDocument);
        } catch (err) {
            // the iframe was torn down mid-measure (a fast mode switch)
        }

        let height = 0;
        try {
            height = Math.ceil(iframe.contentDocument.documentElement.scrollHeight);
        } catch (err) {
            // cross-origin content, or the iframe got destroyed midway (fast mode switch)
        }
        if (!height) height = Math.round(window.innerHeight * 0.4);

        wrapper.style.height = height + "px";
        wrapper.style.overflow = "visible";
        iframe.style.position = "static";
        iframe.style.visibility = "visible";
        iframe.style.height = height + "px";
        wrapper.dataset.mounted = "1";

        wireIframeInteractions(iframe);
        return wrapper;
    }

    function placeBottomSentinel() {
        if (!bottomSentinelEl) {
            bottomSentinelEl = document.createElement("div");
            bottomSentinelEl.style.height = "1px";
        }
        customScrollEl.appendChild(bottomSentinelEl);
    }

    function placeTopSentinel() {
        if (!topSentinelEl) {
            topSentinelEl = document.createElement("div");
            topSentinelEl.style.height = "1px";
        }
        const anchor = sectionWrappers[mountedFrom];
        if (anchor) customScrollEl.insertBefore(topSentinelEl, anchor);
    }

    function setupSentinelObservers() {
        if (bottomSentinelObserver) bottomSentinelObserver.disconnect();
        bottomSentinelObserver = new IntersectionObserver(
            (entries) => { for (const e of entries) if (e.isIntersecting) advanceMount(); },
            { root: customScrollEl, rootMargin: "150% 0px" }
        );
        bottomSentinelObserver.observe(bottomSentinelEl);

        if (topSentinelObserver) topSentinelObserver.disconnect();
        topSentinelObserver = new IntersectionObserver(
            (entries) => { for (const e of entries) if (e.isIntersecting) retreatMount(); },
            { root: customScrollEl, rootMargin: "150% 0px" }
        );
        topSentinelObserver.observe(topSentinelEl);
    }

    // In Continuous scroll there is no "next page" gesture to read as a
    // request for the next volume -- the end is just where scrolling
    // stops. So the offer is made explicitly, once the last section is
    // mounted, and one tap takes it. Auto-advancing on reaching the
    // bottom would fire on an overscroll and swap the book out from
    // under someone who was only looking at the last paragraph.
    let endOfBookEl = null;
    function maybePlaceEndOfBook() {
        if (endOfBookEl || !spineList) return;
        if (mountedUpTo < spineList.length - 1) return;

        endOfBookEl = document.createElement("div");
        endOfBookEl.style.cssText =
            `max-width: ${prefs.width}px; margin: 0 auto; padding: 3rem 1rem 4rem; text-align: center;`;
        const label = document.createElement("div");
        // Which end it is, because they are not the same thing to a
        // reader: one more volume is waiting, or the series stops here.
        label.textContent = nextItemId ? "End of book" : "End of series";
        label.style.cssText = "font-size: 0.75rem; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.55; margin-bottom: 0.75rem;";
        endOfBookEl.append(label, nextItemId ? nextVolumeButton() : backToSeriesLink());
        customScrollEl.appendChild(endOfBookEl);
        placeBottomSentinel(); // keep the sentinel the last child
    }

    // Both are styled from `currentColor` rather than from the app's
    // palette, because this sits INSIDE the reader's own theme -- a
    // sepia page and a black one have to get a border that can be seen.
    const END_CONTROL = "color: inherit; background: transparent; border: 1px solid currentColor;"
        + "border-radius: 6px; padding: 0.6rem 1.1rem; font-size: 0.95rem;"
        + "cursor: pointer; opacity: 0.85; display: inline-block; text-decoration: none;";

    function nextVolumeButton() {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = nextItemTitle ? `Next: ${nextItemTitle} →` : "Next in series →";
        button.style.cssText = END_CONTROL;
        button.addEventListener("click", advanceToNextItem);
        return button;
    }

    // The end of the LAST volume, which used to be answered with nothing
    // at all: the offer was only made when there was something to offer,
    // so finishing a series looked exactly like the reader failing to
    // load the next one.
    function backToSeriesLink() {
        const link = document.createElement("a");
        link.href = root.dataset.seriesUrl || "/";
        link.textContent = "Back to the series";
        link.style.cssText = END_CONTROL;
        return link;
    }

    // The same answer for Paginated, where there is no bottom to put it
    // at: the page TURN is the question, so the answer covers the page
    // the turn was asked from, and a tap anywhere takes it away again.
    // Built here rather than in the template because it has to inherit
    // the reader's own colours, which are set on this element at runtime.
    let endOfSeriesOverlay = null;
    function showEndOfSeriesOverPage() {
        if (endOfSeriesOverlay) return;
        endOfSeriesOverlay = document.createElement("div");
        endOfSeriesOverlay.style.cssText =
            "position: absolute; inset: 0; z-index: 20; display: flex; flex-direction: column;"
            + "align-items: center; justify-content: center; gap: 0.75rem; text-align: center;"
            + "padding: 1.5rem; background: inherit; color: inherit;";
        const label = document.createElement("div");
        label.textContent = "End of series";
        label.style.cssText = "font-size: 0.75rem; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.55;";
        endOfSeriesOverlay.append(label, backToSeriesLink());
        endOfSeriesOverlay.addEventListener("click", (event) => {
            if (event.target.closest("a")) return;
            event.stopPropagation();
            endOfSeriesOverlay.remove();
            endOfSeriesOverlay = null;
            const chapter = pgChapterCache.get(pgSpineIndex);
            if (chapter) reportTextTiming(pgSpineIndex,chapter.blocks,pgPageStart,pgPageEnd,false);
        });
        root.appendChild(endOfSeriesOverlay);
    }

    let advancingMount = false;
    async function advanceMount() {
        if (advancingMount) return;
        if (mountedUpTo + 1 >= spineList.length) {
            maybePlaceEndOfBook();
            return;
        }
        advancingMount = true;
        let mounted = null;
        try {
            mounted = await mountSection(mountedUpTo + 1);
            if (mounted) {
                mountRetries = 0;
                mountedUpTo++;
                placeBottomSentinel();
            }
        } finally {
            advancingMount = false;
        }
        // A chapter that failed was skipped for good: counted as mounted,
        // left at height 0, and the reader scrolled straight from the
        // chapter before it into the one after (measured: 120 paragraphs
        // gone, in both browsers). It is fetched again instead, a few
        // times, while the reader is still there.
        if (!mounted) scheduleMountRetry(advanceMount);
    }

    const MOUNT_RETRY_MS = 3000;
    const MOUNT_RETRY_LIMIT = 10;
    let mountRetries = 0;
    function scheduleMountRetry(again) {
        if (mountRetries >= MOUNT_RETRY_LIMIT) return;
        mountRetries++;
        setTimeout(() => { if (customMode && root.isConnected) again(); }, MOUNT_RETRY_MS);
    }

    let retreatingMount = false;
    async function retreatMount() {
        if (retreatingMount) return;
        if (mountedFrom - 1 < 0) return;
        retreatingMount = true;
        try {
            const targetIndex = mountedFrom - 1;
            const heightBefore = customScrollEl.scrollHeight;
            const mounted = await mountSection(targetIndex);
            if (!mounted) {
                scheduleMountRetry(retreatMount);
                return;
            }
            const delta = customScrollEl.scrollHeight - heightBefore;
            if (delta > 0) customScrollEl.scrollTop += delta;
            mountedFrom = targetIndex;
            placeTopSentinel();
        } finally {
            retreatingMount = false;
        }
    }

    // Read on every scroll frame, and a loaded chapter never changes, so
    // its blocks are found once per document. Keyed by the document rather
    // than the wrapper: before the srcdoc has loaded, the iframe holds a
    // different, empty one -- and one still being parsed is not kept.
    const blocksByDocument = new WeakMap();
    function getBlocks(wrapper) {
        const iframe = wrapper && wrapper.querySelector("iframe");
        try {
            const doc = iframe && iframe.contentDocument;
            if (!doc || !doc.body) return [];
            if (blocksByDocument.has(doc)) return blocksByDocument.get(doc);
            const blocks = window.readerBlocks.collect(doc.body);
            if (doc.readyState === "complete") blocksByDocument.set(doc, blocks);
            return blocks;
        } catch (err) {
            return [];
        }
    }

    function measureCustomPosition() {
        const top = customScrollEl.scrollTop;
        // Scrolled to the very bottom of the last chapter is the end of the
        // book. Read at the TOP of the viewport, the position could never
        // get there: the last screenful's first paragraph is short of the
        // end by a screen, and a book read to its last line stayed
        // "reading" at 97% with "End of series" on the page (both browsers).
        const lastIndex = spineList.length - 1;
        if (mountedUpTo === lastIndex && lastIndex >= 0
                && top + customScrollEl.clientHeight >= customScrollEl.scrollHeight - END_SLACK_PX) {
            const blocks = getBlocks(sectionWrappers[lastIndex]);
            if (blocks.length) {
                return { spineIndex: lastIndex, elementIndex: blocks.length - 1, elementFraction: 1 };
            }
        }
        for (let i = Math.max(0, mountedFrom); i <= mountedUpTo; i++) {
            const wrapper = sectionWrappers[i];
            if (!wrapper) continue;
            const wrapperTop = wrapper.offsetTop;
            const wrapperHeight = wrapper.offsetHeight;
            if (top < wrapperTop + wrapperHeight || i === mountedUpTo) {
                const targetY = top - wrapperTop;
                const blocks = getBlocks(wrapper);
                let elementIndex = 0;
                for (let b = 0; b < blocks.length; b++) {
                    if (blocks[b].getBoundingClientRect().top <= targetY + 1) {
                        elementIndex = b;
                    } else {
                        break;
                    }
                }
                const offset = blocks[elementIndex] ? pagination.offsetAt(blocks[elementIndex], targetY) : 0;
                const elementFraction = blocks.length ? (elementIndex + offset / Math.max(1, pagination.length(blocks[elementIndex]))) / blocks.length : 0;
                return { spineIndex: i, elementIndex, offset, elementFraction };
            }
        }
        return { spineIndex: Math.max(0, mountedFrom), elementIndex: 0, elementFraction: 0 };
    }

    // How close to the bottom counts as there: a fractional scrollTop and
    // a browser's own rounding leave a pixel or two whatever the reader does.
    const END_SLACK_PX = 4;

    function scrollToCustomPosition(spineIndex, elementIndex) {
        const wrapper = sectionWrappers[spineIndex];
        if (!wrapper) return;
        if (elementIndex !== undefined && elementIndex !== null) {
            const blocks = getBlocks(wrapper);
            // Past the end of the chapter (a shorter edition, a stale
            // location): its last block, as in Paginated.
            const anchor = pagination.position(elementIndex);
            const el = blocks[Math.min(anchor.index, blocks.length - 1)];
            if (el) {
                customScrollEl.scrollTop = wrapper.offsetTop + (anchor.offset ? pagination.topAt(el, anchor.offset) : el.getBoundingClientRect().top);
                return;
            }
        }
        customScrollEl.scrollTop = wrapper.offsetTop;
    }

    async function initCustomScroll(targetIndexOverride, targetElementIndexOverride) {
        await fetchSpine();
        ensureWrappers();

        let targetIndex = targetIndexOverride !== undefined ? targetIndexOverride : (startSpineIndex || 0);
        let targetElementIndex = targetIndexOverride !== undefined ? (targetElementIndexOverride || 0) : startPosition;
        targetIndex = Math.max(0, Math.min(spineList.length - 1, targetIndex));

        const precedingIndex = targetIndex - 1;
        const followingIndex = targetIndex + 1;
        // A neighbour that failed to load is left out of the mounted range,
        // so the sentinels fetch it again rather than scroll past it.
        mountedFrom = targetIndex;
        if (precedingIndex >= 0 && await mountSection(precedingIndex)) mountedFrom = precedingIndex;
        await mountSection(targetIndex);
        mountedUpTo = targetIndex;
        if (followingIndex < spineList.length && await mountSection(followingIndex)) {
            mountedUpTo = followingIndex;
        }
        scrollToCustomPosition(targetIndex, targetElementIndex);
        placeBottomSentinel();
        placeTopSentinel();
        setupSentinelObservers();
        maybePlaceEndOfBook(); // a book already mounted to its end has one now

        clearLoadingIndicator();
        ensureCustomScrollListeners();
        lastAdvanceCheckSpineIndex = targetIndex;
        reportContinuousTiming(false);
    }

    let cancelScrollSave = () => {};
    let customScrollListenersReady = false;
    let customScrollInitializing = true;
    function ensureCustomScrollListeners() {
        if (customScrollListenersReady) return;
        customScrollListenersReady = true;

        let scrollRafPending = false;
        function onCustomScroll() {
            if (customReflowing) return;
            if (scrollRafPending) return;
            scrollRafPending = true;
            requestAnimationFrame(() => {
                scrollRafPending = false;
                if (!userHasInteracted) return; // never save from an automatic adjustment
                customHasNavigated = true;
                const { spineIndex, elementIndex, offset = 0, elementFraction } = measureCustomPosition();
                const percentage = bookFraction(spineIndex, elementFraction);
                reportContinuousTiming(!customScrollInitializing);
                updateIndicator(elementIndex, getBlocks(sectionWrappers[spineIndex]).length, spineIndex);
                notifyChapterAdvance(spineIndex);
                if (!customScrollInitializing) {
                    scheduleScrollSave(spineIndex, {index: elementIndex, offset}, percentage, elementFraction);
                }
            });
        }

        // One POST per paragraph reached, at most one per SCROLL_SAVE_MS --
        // not one per animation frame: measured, 60 wheel steps posted 60
        // times (a fling on a phone is 60 a second), each a write
        // transaction and an event to every open tab, most of them saying
        // what the one before had said. The latest position is the one
        // sent, and every way out (pagehide, boosted leave) saves at once.
        const SCROLL_SAVE_MS = 1000;
        let lastScrollSaveKey = null;
        let pendingScrollSave = null;
        let scrollSaveTimer = null;
        cancelScrollSave = () => { clearTimeout(scrollSaveTimer); scrollSaveTimer = null; pendingScrollSave = null; };
        function scheduleScrollSave(spineIndex, elementIndex, percentage, fraction) {
            pendingScrollSave = { spineIndex, elementIndex, percentage, fraction };
            if (scrollSaveTimer) return;
            scrollSaveTimer = setTimeout(() => {
                scrollSaveTimer = null;
                const save = pendingScrollSave;
                pendingScrollSave = null;
                const key = `${save.spineIndex}:${save.elementIndex.index}:${save.elementIndex.offset}:${save.fraction === 1}`;
                if (key === lastScrollSaveKey) return;
                lastScrollSaveKey = key;
                saveProgress(save.spineIndex, spineList[save.spineIndex], save.elementIndex,
                             save.percentage, save.fraction);
            }, SCROLL_SAVE_MS);
        }
        customScrollEl.addEventListener("scroll", onCustomScroll, { passive: true });
        customScrollEl.addEventListener("wheel", markInteraction, { passive: true });
        customScrollEl.addEventListener("touchstart", markInteraction, { passive: true });
        requestAnimationFrame(() => { customScrollInitializing = false; });
    }

    // ---------- CUSTOM paginated mode ----------
    // A "page" here is a contiguous slice of blocks (paragraph, heading,
    // table, etc. -- reader-blocks.js, the same blocks continuous scroll
    // counts) chosen so their combined rendered height fits within one
    // viewport. Resuming always computes a FRESH page starting exactly at
    // the saved element index -- see the module comment at the top for why
    // that's the whole point of not using epub.js/a CSS-column approach.
    //
    // A single block that alone is taller than the viewport (a large
    // image, a long table, an unusually long paragraph) is still shown
    // alone as its own page rather than causing an infinite loop or a
    // blank page -- it may require its own internal scroll, which is an
    // acceptable rare exception, not a reason to split it further. That
    // includes an EPUB's cover or title page, which is very often JUST an
    // image with no text at all: it is one block, and gets its own page.
    const paginatedViewerEl = document.getElementById("paginated-viewer");
    // spineIndex -> { blocks, shell }: the chapter's blocks, and the empty
    // page they are placed into (its own stylesheets and body attributes).
    const pgChapterCache = new Map();
    let pgSpineIndex = null;
    let pgPageStart = pagination.position(0);
    let pgPageEnd = pagination.position(0);
    let pgHasNavigated = false;
    let pgFinished = false;

    // spineIndex -> Promise of the chapter's text, fetched ahead of the
    // page turn that needs it (see prefetchChapter).
    const pgPrefetched = new Map();

    async function fetchChapterText(spineIndex) {
        const res = await fetch(adapter.resourceUrl(spineList[spineIndex]));
        if (!res.ok) throw new Error("HTTP " + res.status);
        timingSourceVersions.set(spineIndex,res.headers.get('X-Reading-Source') || res.headers.get('ETag'));
        return res.text();
    }

    // The chapter after the one on screen, fetched as soon as that one is
    // shown. Without it the first turn into a new chapter waited for the
    // network: measured in both browsers, a chapter was requested only
    // when the turn past the last page of the one before asked for it.
    // Only the TEXT is fetched here -- parsing and measuring stay with the
    // turn, which knows the page size in force by then. A failure is
    // dropped silently: the turn fetches again and is the one that warns,
    // so a blip while reading page 1 never shows an error for a chapter
    // nobody has asked to see. Safe for a web novel too: its chapter comes
    // out of the database, and reading it early scrapes or marks nothing.
    function prefetchChapter(spineIndex) {
        if (spineIndex < 0 || spineIndex >= spineList.length) return;
        if (pgChapterCache.has(spineIndex) || pgPrefetched.has(spineIndex)) return;
        const pending = fetchChapterText(spineIndex);
        pending.catch(() => pgPrefetched.delete(spineIndex));
        pgPrefetched.set(spineIndex, pending);
    }

    async function loadChapterBlocks(spineIndex) {
        if (pgChapterCache.has(spineIndex)) return pgChapterCache.get(spineIndex);
        const pending = pgPrefetched.get(spineIndex);
        pgPrefetched.delete(spineIndex);
        let html;
        try {
            const text = pending
                ? await pending.catch(() => fetchChapterText(spineIndex))
                : await fetchChapterText(spineIndex);
            html = applyReplacements(text);
        } catch (err) {
            // Thrown, and NOT cached: a failure cached as an empty chapter
            // stayed one for the session -- a blank page, then a turn
            // that skipped the whole chapter (measured in both browsers).
            // The caller warns, and the next turn fetches it again.
            throw err;
        }
        const doc = new DOMParser().parseFromString(html, "text/html");
        const result = { blocks: window.readerBlocks.collect(doc.body), shell: window.readerBlocks.pageShell(doc) };
        pgChapterCache.set(spineIndex, result);
        return result;
    }

    // Every chapter iframe is created here, sandboxed. A srcdoc iframe with
    // no `sandbox` attribute is the app's own origin: an EPUB's <script>
    // ran with the reader's session -- measured, a credentialed fetch of
    // /library/job-status answered 200 from inside a chapter -- and an
    // inline handler (`<img onerror>`) ran even in Paginated, which drops
    // <script> elements but not attributes. `allow-same-origin` WITHOUT
    // `allow-scripts` keeps what the reader needs (it reads and wires the
    // iframe's document from out here, see wireIframeInteractions) and
    // runs nothing the book brought. Popups stay allowed, unsandboxed, so
    // a book's external link opened in a new tab still works.
    function createChapterIframe() {
        const iframe = document.createElement("iframe");
        iframe.setAttribute("sandbox", "allow-same-origin allow-popups allow-popups-to-escape-sandbox");
        return iframe;
    }

    async function waitForIframeLoad(iframe) {
        // A load for the initial about:blank can arrive before srcdoc commits.
        // Filling that old document makes a visible page which the later
        // navigation erases, including its tap handlers (GeckoView).
        const chapterDocument = () => {
            const doc = iframe.contentDocument;
            return doc?.URL === "about:srcdoc" && doc.body ? doc : null;
        };
        const loaded = await new Promise((resolve) => {
            let timer;
            const finish = (ready) => {
                clearTimeout(timer);
                iframe.removeEventListener("load", onLoad);
                resolve(ready);
            };
            const onLoad = () => {
                if (chapterDocument()?.readyState === "complete") finish(true);
            };
            iframe.addEventListener("load", onLoad);
            // A committed document may have a slow stylesheet. Keep the
            // bounded wait, but never substitute the initial blank document.
            timer = setTimeout(() => finish(!!chapterDocument()), 5000);
            onLoad(); // Also handle a srcdoc that completed before binding.
        });
        if (!loaded) return false;
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        return true;
    }

    // A web font is fetched only once the document actually has text to
    // draw with it, and `font-display: swap` (see the @font-face rules in
    // app.css) means the browser draws that text in a FALLBACK face in the
    // meantime and re-lays it out when the real face arrives. So a page
    // measured the instant after its blocks were inserted is measured in
    // the wrong font: Literata is taller per line than the fallback, and
    // the page rendered right afterwards -- by which time the face HAS
    // arrived -- grows past the bottom of the screen. Measured on a 390px
    // phone viewport, cold cache: the first page fitted 7 blocks at
    // measure time and rendered 899px tall in an 807px viewport, 92px of
    // text below the fold; every later page, with the font warm, fitted
    // exactly. That is the bug whose workaround was switching fonts back
    // and forth to force a repagination.
    //
    // `fonts.ready` resolves once nothing is left loading FOR WHAT IS
    // CURRENTLY RENDERED, so it has to be awaited after the text is in the
    // document, not at load time when the body is still empty. Bounded,
    // like waitForImages: an unreachable font must not hang pagination.
    function waitForFonts(doc, timeoutMs = 3000) {
        // Trigger layout first: otherwise fonts.ready can still be the resolved
        // promise from the empty page, before newly inserted text requests a face.
        doc?.body?.getBoundingClientRect();
        if (!doc || !doc.fonts || !doc.fonts.ready) return Promise.resolve();
        return Promise.race([
            Promise.resolve(doc.fonts.ready).catch(() => {}),
            new Promise((resolve) => setTimeout(resolve, timeoutMs)),
        ]);
    }

    // A newly-inserted <img> doesn't have its real height available
    // immediately -- the browser only knows it once the image data itself
    // has actually loaded, which is NOT part of insertAdjacentHTML(); it
    // happens asynchronously afterward. Measuring scrollHeight right after
    // inserting a block that contains one reads its height as ~0 (or
    // whatever a CSS placeholder happens to be), not its real, eventual
    // size -- which is exactly why two images that shouldn't fit on the
    // same page could still both get measured as "fitting": the first
    // image hadn't finished loading yet when the second was measured
    // against it. Waiting for every image in the block just inserted
    // (bounded by a timeout, so one broken/unreachable image link can't
    // hang pagination forever) fixes the measurement at the source,
    // instead of trying to patch around it after the fact.
    function waitForImages(doc, timeoutMs = 4000) {
        const imgs = Array.from(doc.images || doc.querySelectorAll("img"));
        const pending = imgs.filter((img) => !img.complete);
        if (pending.length === 0) return Promise.resolve();
        return Promise.race([
            Promise.all(pending.map((img) => new Promise((resolve) => {
                img.addEventListener("load", resolve, { once: true });
                img.addEventListener("error", resolve, { once: true }); // a broken image shouldn't hang pagination
            }))),
            new Promise((resolve) => setTimeout(resolve, timeoutMs)),
        ]);
    }

    // Text nodes use the same UTF-16 offsets as saved reading positions.
    // Keep complete block text with each range, so the server can verify it
    // against the source and count word starts without splitting words twice.
    function timingText(block) {
        const skipped = new Set(['script','style','noscript','template','svg','math','video','audio','canvas','iframe','object','embed']);
        const runs = [];
        let domOffset = 0, text = '';
        function visit(node) {
            if (node.nodeType === Node.TEXT_NODE) {
                runs.push({start: domOffset, end: domOffset + node.length, readable: text.length});
                domOffset += node.length;
                text += node.data;
            } else if (node.nodeType === Node.ELEMENT_NODE) {
                // Saved anchors count all DOM text, including labels inside
                // media. Reading pace excludes that text. Preserve the gap
                // and map the anchor into the filtered passage explicitly.
                if (skipped.has(node.localName)) domOffset += node.textContent.length;
                else for (const child of node.childNodes) visit(child);
            }
        }
        for (const node of block.nodes) visit(node);
        function offsetAt(offset) {
            for (const run of runs) {
                if (offset < run.start) return run.readable;
                if (offset <= run.end) return run.readable + Math.max(0, offset - run.start);
            }
            return text.length;
        }
        return {text, length: domOffset, offsetAt};
    }
    function timingPassage(blocks, start, end) {
        const result = [];
        for (let i = start.index; i < blocks.length && i <= end.index; i++) {
            const readable = timingText(blocks[i]);
            const from = readable.offsetAt(i === start.index ? start.offset || 0 : 0);
            const to = readable.offsetAt(i === end.index ? end.offset || 0 : readable.length);
            if (to > from) result.push({text: readable.text, start:from, end:to});
        }
        return result;
    }
    let previousTiming = null;
    let timingReflowUntil = 0;
    let lastTimingScroll = null;
    let lastTimingUpdate = 0;
    let timingScrollTimer = null;
    outside.push(() => clearTimeout(timingScrollTimer));
    function reportContinuousTiming(normal) {
        if (!root.isConnected) return;
        // Smooth scrolling changes character offsets every animation frame.
        // Accumulate that time before advancing the measured anchor instead
        // of throwing away dozens of sub-second visits per second.
        if (normal && performance.now() - lastTimingUpdate < 1000) {
            if (timingScrollTimer === null) timingScrollTimer = setTimeout(() => {
                timingScrollTimer = null;
                reportContinuousTiming(true);
            },1000 - (performance.now() - lastTimingUpdate));
            return;
        }
        clearTimeout(timingScrollTimer);
        timingScrollTimer = null;
        lastTimingUpdate = performance.now();
        const pos = measureCustomPosition();
        const wrapper = sectionWrappers[pos.spineIndex];
        if (!wrapper) return;
        const blocks = getBlocks(wrapper);
        const start = {index:pos.elementIndex, offset:pos.offset || 0};
        const bottom = customScrollEl.scrollTop - wrapper.offsetTop + customScrollEl.clientHeight;
        let end = {index:blocks.length, offset:0};
        for (let i = start.index; i < blocks.length; i++) {
            if (blocks[i].getBoundingClientRect().bottom >= bottom) {
                end = {index:i, offset:pagination.offsetAt(blocks[i], bottom)};
                break;
            }
        }
        const scroll = customScrollEl.scrollTop;
        normal = normal && lastTimingScroll !== null
            && Math.abs(scroll - lastTimingScroll) <= customScrollEl.clientHeight * 1.5;
        lastTimingScroll = scroll;
        reportTextTiming(pos.spineIndex, blocks, start, end, normal, true);
    }
    function reportTextTiming(spine, blocks, start, end, normal = false, continuous = false) {
        const payload = {...adapter.buildTimingPayload(spine, spineList[spine], start.index),
            source_version:source === 'epub' ? contentVersion : timingSourceVersions.get(spine),
            character_offset:start.offset || 0, passage:timingPassage(blocks, start, end)};
        normal = normal && performance.now() >= timingReflowUntil;
        let completed = normal, traversedEnd = null;
        if (continuous && previousTiming) {
            completed = normal && spine === previousTiming.spine
                && pagination.compare(start, previousTiming.start) > 0
                && pagination.compare(start, previousTiming.end) <= 0;
            if (completed) {
                const prefix = timingPassage(blocks, previousTiming.start, start);
                traversedEnd = {part:prefix.length - 1, offset:prefix.at(-1)?.end || 0};
                if (!prefix.length) completed = false;
            }
        }
        readingTime.at(payload, {completed, traversedEnd});
        previousTiming = {spine, start, end};
    }

    function updateIndicator(elementIndex, chapterBlockCount, spineIndex) {
        updateClock();
        // Both modes already funnel every page change through here -- it
        // is why the clock lives here too -- so it is also where the
        // reading-time tracker is told what is on screen. The adapter
        // decides how a page is identified, because an EPUB position is
        // (item, spine index, paragraph) while a web novel's is (chapter,
        // paragraph): see the two buildTimingPayload implementations.
        if (spineIndex != null && spineList[spineIndex]) {
            // Timing is reported with character ranges by each layout below.
        }
        if (!indicator) return;
        if (chapterBlockCount) {
            indicator.textContent = `${elementIndex + 1} / ${chapterBlockCount}`;
        } else {
            indicator.textContent = "";
        }
    }

    // ---------- One page ----------
    //
    // A page is laid out IN the iframe that shows it: blocks go in one at a
    // time until the next would push past the bottom, and that one comes
    // back off. It used to be measured in a hidden iframe first and then
    // built again in a second one, checked once more against the real
    // viewport -- two documents, two loads and two layouts per turn (20
    // iframes for 10 turns; ~85 ms a turn in Chromium, ~100 in Firefox).
    // Measuring the document that is then shown cannot disagree with it,
    // which is what the second check existed to catch: a font that arrived
    // late, an EPUB's own embedded face, a stylesheet the book loads.
    //
    // Which edge is anchored is not a detail: paging FORWARD the page
    // starts where the last one ended, so blocks are appended and the one
    // that overflowed comes off the end; paging BACKWARD the page ends
    // where the current one starts, so they are prepended and it comes off
    // the front. A lone block too tall for any page keeps a page of its own
    // (the iframe scrolls; see overflow-y in the theme CSS) rather than
    // being split.
    //
    // The new page is built hidden ON TOP of the one being read and swaps
    // in only when it is done. Every call takes a generation, and one that
    // has been overtaken -- a slider dragged across a dozen values, an
    // arrow key held down -- drops its iframe at the next checkpoint and
    // touches nothing else. Without that, two layouts ran at once and each
    // cleared the viewer under the other: measured in both browsers, a
    // page left with one paragraph and 615px of blank below it, and the
    // indicator rewritten by the loser five seconds later.
    let pageViewportSize = "";
    const pageResize = new ResizeObserver(() => {
        const size = `${paginatedViewerEl.clientWidth}:${paginatedViewerEl.clientHeight}`;
        if (!customMode && pgSpineIndex !== null && paginatedViewerEl.clientHeight > 0 && size !== pageViewportSize) {
            reapplyPrefsToCurrentView();
        }
    });
    pageResize.observe(paginatedViewerEl);
    outside.push(() => pageResize.disconnect());
    let pgGeneration = 0;

    async function renderPage(spineIndex, edge, direction = "forward", normal = false) {
        pageViewportSize = `${paginatedViewerEl.clientWidth}:${paginatedViewerEl.clientHeight}`;
        const gen = ++pgGeneration;
        const stale = () => gen !== pgGeneration || customMode || !root.isConnected;
        const entry = spineList[spineIndex];
        let chapter;
        try {
            chapter = await loadChapterBlocks(spineIndex);
        } catch (err) {
            if (!stale()) warnChapterNotLoaded(err);
            return false;
        }
        if (stale()) return false;
        const { blocks } = chapter;

        const iframe = createChapterIframe();
        iframe.style.cssText = "position:absolute; top:0; left:0; width:100%; height:100%; border:none; visibility:hidden;";
        iframe.srcdoc = buildSectionDoc(chapter.shell, adapter.baseHrefFor(entry));
        paginatedViewerEl.appendChild(iframe);
        if (!await waitForIframeLoad(iframe)) {
            iframe.remove();
            if (!stale()) {
                showLoadWarning("Couldn't prepare a chapter. Please try opening the reader again.");
                clearLoadingIndicator();
            }
            return false;
        }
        if (stale()) { iframe.remove(); return false; }

        const doc = iframe.contentDocument;
        let start, end;
        try {
            ({start, end} = await pagination.fill(doc.body, blocks, edge, direction, iframe.clientHeight || 1, async () => {
                await waitForImages(doc);
                await waitForFonts(doc);
                if (stale()) throw new Error("Superseded page");
            }));
        } catch (error) {
            iframe.remove();
            if (stale()) return false;
            throw error;
        }
        if (stale()) { iframe.remove(); return false; }

        for (const child of Array.from(paginatedViewerEl.children)) {
            if (child !== iframe) child.remove();
        }
        iframe.style.visibility = "visible";
        wireIframeInteractions(iframe);
        // Left/right thirds of the page turn back/forward, middle third
        // asks for the (hidden-by-default) Previous/Next bar, or puts the
        // settings panel away when that is what is on screen -- a
        // horizontal swipe-to-turn-page can get intercepted by the mobile
        // browser's own edge-swipe-back gesture before our touchend
        // handler ever sees it (swipe is left in place too, for anyone it
        // works fine for -- this is an addition, not a replacement). A
        // plain tap doesn't move far enough to cross SWIPE_THRESHOLD, so
        // it never double-fires with the swipe handler.
        if (doc) doc.addEventListener("click", (e) => handlePaginatedTap(e, doc));

        notifyChapterAdvance(spineIndex);
        pgSpineIndex = spineIndex;
        pgPageStart = start;
        pgPageEnd = end;
        reportTextTiming(spineIndex, blocks, start, end, normal);
        updateIndicator(start.index, blocks.length, spineIndex);
        clearLoadingIndicator();
        prefetchChapter(spineIndex + 1);
        return true;
    }

    function handlePaginatedTap(e, doc) {
        const width = (doc.documentElement && doc.documentElement.clientWidth) || 1;
        const relativeX = e.clientX / width;
        if (relativeX < 1 / 3) {
            paginatedPrev();
        } else if (relativeX > 2 / 3) {
            paginatedNext();
        } else {
            toggleControls();
        }
    }

    // Both answer whether THIS call is the page now on screen: false when
    // a later one overtook it (see renderPage), which then owns the save.
    function gotoPaginatedForward(spineIndex, start, normal = false) {
        return renderPage(spineIndex, start, "forward", normal);
    }

    function gotoPaginatedBackwardEnd(spineIndex, endExclusive, normal = false) {
        return renderPage(spineIndex, endExclusive, "backward", normal);
    }

    async function initPaginated(targetSpineIndex, targetElementIndex) {
        await fetchSpine();
        const spineIndex = Math.max(0, Math.min(spineList.length - 1, targetSpineIndex));
        lastAdvanceCheckSpineIndex = spineIndex;
        await gotoPaginatedForward(spineIndex, targetElementIndex || 0);
    }

    let pgNavigating = false;
    async function paginatedNext() {
        if (pgNavigating || pgSpineIndex === null) return;
        pgNavigating = true;
        try {
            markInteraction();
            const { blocks } = await loadChapterBlocks(pgSpineIndex);
            let shown;
            if (pgPageEnd.index < blocks.length) {
                shown = await gotoPaginatedForward(pgSpineIndex, pgPageEnd, true);
            } else if (pgSpineIndex + 1 < spineList.length) {
                shown = await gotoPaginatedForward(pgSpineIndex + 1, 0, true);
            } else {
                // End of the book. A page turn here is a request for what
                // comes next, so give it the next volume -- or, when this
                // was the last one, say so instead of refusing to move
                // for a reason the page never states.
                notifyChapterAdvance(spineList.length);
                pgFinished = true;
                readingTime.finish();
                pgHasNavigated = true;
                savePaginatedProgress();
                if (nextItemId) advanceToNextItem();
                else showEndOfSeriesOverPage();
                return;
            }
            if (!shown) return;
            pgFinished = false;
            pgHasNavigated = true;
            savePaginatedProgress();
        } finally {
            pgNavigating = false;
        }
    }

    async function paginatedPrev() {
        if (pgNavigating || pgSpineIndex === null) return;
        pgNavigating = true;
        try {
            markInteraction();
            let shown;
            if (pagination.compare(pgPageStart, pagination.position(0)) > 0) {
                shown = await gotoPaginatedBackwardEnd(pgSpineIndex, pgPageStart, true);
            } else if (pgSpineIndex > 0) {
                let blocks;
                try {
                    ({ blocks } = await loadChapterBlocks(pgSpineIndex - 1));
                } catch (err) {
                    warnChapterNotLoaded(err);
                    return;
                }
                shown = await gotoPaginatedBackwardEnd(pgSpineIndex - 1, blocks.length, true);
            } else {
                return; // start of the book
            }
            if (!shown) return;
            pgFinished = false;
            pgHasNavigated = true;
            savePaginatedProgress();
        } finally {
            pgNavigating = false;
        }
    }

    function savePaginatedProgress(useBeacon = false) {
        if (!pgHasNavigated || pgSpineIndex === null) return;
        const { blocks } = pgChapterCache.get(pgSpineIndex) || { blocks: [] };
        const fraction = pgFinished ? 1 : (blocks.length ? (pgPageStart.index + pgPageStart.offset / Math.max(1, pagination.length(blocks[pgPageStart.index]))) / blocks.length : 0);
        const percentage = pgFinished ? 1 : bookFraction(pgSpineIndex, fraction);
        saveProgress(pgSpineIndex, spineList[pgSpineIndex], pgPageStart, percentage, fraction, useBeacon);
    }

    // ---------- Controls visibility ----------
    // Two independent things, where there used to be one flag for both.
    // The page-nav is about WHERE YOU ARE in the book and belongs to the
    // tap that asks for it (the middle third of the page, or the "Aa"
    // button's old job); the settings panel is a thing you go and open.
    // Tying them together meant reaching Previous/Next opened nine
    // controls under the text, and opening the settings put a pair of
    // navigation buttons on screen for no reason.
    //
    // Previous/Next only mean anything in Paginated mode, so they stay
    // hidden in Continuous scroll whatever navVisible says.
    let navVisible = false;
    let panelOpen = false;
    const settingsPanel = document.getElementById("settings-panel");
    const settingsButton = document.getElementById("btn-settings");

    function updateControlsVisibility() {
        settingsPanel.classList.toggle("hidden", !panelOpen);
        settingsButton.setAttribute("aria-expanded", panelOpen ? "true" : "false");
        document.getElementById("page-nav").classList.toggle("hidden", customMode || !navVisible);
    }
    function setPanelOpen(open) {
        panelOpen = open;
        updateControlsVisibility();
        // The panel scrolls and is taller than the screen on a phone, so
        // it opens at its own top however far the last visit scrolled it.
        if (open) settingsPanel.scrollTop = 0;
    }
    function toggleControls() {
        // A tap on the page with the panel open is asking to put it away,
        // not to turn the page-nav on underneath it.
        if (panelOpen) { setPanelOpen(false); return; }
        navVisible = !navVisible;
        updateControlsVisibility();
        // The nav IS a sibling of the reading area (unlike the settings
        // panel, which floats over it), so showing it makes #viewer
        // shorter under a page that was laid out for the taller one.
        // Measured: 21px of the last paragraph below the fold for as long
        // as the bar was up. Recomputing the page from the same starting
        // block is what keeps "everything on screen is readable" true at
        // both heights; the bar is deliberate and transient, so the
        // reflow is something the reader asked for and can undo.
        if (!customMode) reapplyPrefsToCurrentView();
    }

    // ---------- Mode switching ----------

    async function applyFlow(flow) {
        readingTime.interrupt();
        timingReflowUntil = performance.now() + 500;
        cancelScrollSave();
        prefs.flow = flow;
        savePrefs(prefs);

        if (flow === "scrolled-doc") {
            const targetSpine = pgSpineIndex !== null ? pgSpineIndex : startSpineIndex;
            const targetElidx = pgSpineIndex !== null ? pgPageStart : startPosition;
            customMode = true;
            updateControlsVisibility();
            paginatedViewerEl.classList.add("hidden");
            document.getElementById("custom-scroll").classList.remove("hidden");
            await initCustomScroll(targetSpine, targetElidx);
        } else {
            // Measured while continuous scroll is still on screen. Hidden,
            // it has no layout: scrollTop and every offset read 0, and the
            // position came back as the LAST block of the last chapter
            // mounted -- measured, every switch, both browsers.
            let targetSpine = startSpineIndex || 0;
            let targetElidx = startPosition;
            if (customHasNavigated || mountedFrom >= 0) {
                const { spineIndex, elementIndex, offset = 0 } = measureCustomPosition();
                targetSpine = spineIndex;
                targetElidx = {index: elementIndex, offset};
            }
            customMode = false;
            updateControlsVisibility();
            document.getElementById("custom-scroll").classList.add("hidden");
            paginatedViewerEl.classList.remove("hidden");
            await initPaginated(targetSpine, targetElidx);
        }
    }

    // ---------- Carrying on into the next volume ----------
    //
    // Only an EPUB has one: a web novel's spine already IS the whole
    // novel, so its chapters are crossed by the ordinary paginator and
    // there is no boundary to cross here. The comic reader does the same
    // thing for a series of CBZs (advanceToNextChapter), which is why
    // finishing an EPUB used to be the one place in the app where reading
    // simply stopped.
    //
    // This one navigates rather than swapping the book in place, and that
    // is a deliberate difference from the comic reader: everything the
    // text engine holds -- the manifest, the spine weights, the resource
    // base href, the progress and timing endpoints -- is keyed to the item
    // it was built for, and a book boundary is crossed once per volume
    // rather than once per chapter, so a page load is a fair price for not
    // making all of that mutable.
    const nextItemId = root.dataset.nextItemId || null;
    const nextItemTitle = root.dataset.nextItemTitle || "";

    // Set once the hand-over has started, because leaving the page fires
    // pagehide, and saveOnExit would otherwise post the ORDINARY position
    // (the last page is ~92% of the book, so "reading") on top of the
    // "finished" one written just above. Which of the two lands last is
    // not something either end guarantees -- in Chromium the keepalive
    // fetch won every time and removing this guard changed nothing
    // observable -- so this makes the answer not depend on it.
    let leavingForNextItem = false;

    function advanceToNextItem() {
        if (!nextItemId || leavingForNextItem) return;
        leavingForNextItem = true;
        // Record it as finished before the id is gone -- same order as the
        // comic reader, and what keeps "read the whole series" from
        // leaving every volume but the last at 97%.
        if (customMode) {
            const { spineIndex, elementIndex } = measureCustomPosition();
            saveProgress(spineIndex, spineList[spineIndex], elementIndex, 1, 1);
        } else if (pgSpineIndex !== null) {
            pgFinished = true;
            saveProgress(pgSpineIndex, spineList[pgSpineIndex], pgPageStart, 1, 1);
        }
        window.location.href = `/reader/${nextItemId}`;
    }

    // ---------- Progress persistence (shared by both modes) ----------

    // Request arrival order is not reading action order. Keep a counter for
    // this opening, including exit beacons; a later backward turn is still new.
    const progressSaveSession = crypto.randomUUID ? crypto.randomUUID() :
        Array.from(crypto.getRandomValues(new Uint8Array(16)), value =>
            value.toString(16).padStart(2, "0")).join("").replace(
                /(.{8})(.{4})(.{4})(.{4})(.{12})/, "$1-$2-$3-$4-$5");
    let progressSaveSequence = 0;

    function saveProgress(spineIndex, entry, location, percentage, chapterFraction, useBeacon = false) {
        const {index: elidx, offset} = pagination.position(location);
        if (!entry || root.dataset.resumePending === "true" || !root.isConnected) return;
        const payload = JSON.stringify(
            { ...adapter.buildProgressPayload(spineIndex, entry, elidx, percentage || 0, chapterFraction || 0, offset),
              on_exit: useBeacon,
              ...(source === "epub" ? {save_session: progressSaveSession, save_sequence: ++progressSaveSequence} : {}) });
        // The baseline a restored page measures the server against -- see
        // reader-resume.js. The WHOLE-book fraction, not the chapter's:
        // 90% of chapter 2 is behind 10% of chapter 40, and the two ends
        // have to be on one scale for "ahead" to mean anything.
        window.readerResume?.sent((percentage || 0) * 100, [spineIndex, elidx, offset]);
        const url = adapter.progressUrl;
        if (useBeacon && navigator.sendBeacon) {
            navigator.sendBeacon(url, new Blob([payload], { type: "application/json" }));
        } else {
            // A save that fails is superseded by the next one -- every page
            // change and every way out saves the position again -- so it
            // is not an error to show. Uncaught, it reached the page's
            // unhandledrejection handler and told the reader their EPUB
            // had malformed HTML.
            fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: payload, keepalive: true })
                .catch(() => {});
        }
    }

    function saveOnExit() {
        if (leavingForNextItem) return; // already saved, as finished
        if (customMode) {
            if (!customHasNavigated) return;
            const { spineIndex, elementIndex, offset = 0, elementFraction } = measureCustomPosition();
            const percentage = bookFraction(spineIndex, elementFraction);
            saveProgress(spineIndex, spineList[spineIndex], {index: elementIndex, offset}, percentage, elementFraction, true);
        } else {
            savePaginatedProgress(true);
        }
    }
    bindOutside(document, "visibilitychange", () => {
        if (document.visibilityState === "hidden") saveOnExit();
    });
    bindOutside(window, "pagehide", saveOnExit);

    // ---- Leaving under hx-boost ----
    //
    // A boosted navigation does not unload the document: it fetches the
    // next page and swaps <body>. So `pagehide` NEVER FIRES, and neither
    // does `visibilitychange` -- the document stays visible throughout.
    // Both handlers above would simply not run, and the position would be
    // lost on the one exit a reader takes most.
    //
    // `htmx:before:history:update` is the hook because of WHERE it is
    // raised. In the vendored bundle it is the first thing `swap()` does:
    //
    //     async swap(e) { this.#$e(e); let {fragment, title} = ...
    //
    // -- before the response is even parsed, and long before the DOM is
    // touched, so the reader can still measure itself. It is also narrow:
    // it fires only for a navigation that pushes or replaces the URL, and
    // these readers move their own URL with `history.replaceState`
    // directly (see above), which htmx knows nothing about -- so a page
    // turn cannot reach it.
    //
    // AND BACK IS A SECOND WAY OUT, which raises none of it: htmx's
    // history cache answers a traversal from a copy of the DOM and never
    // pushes or replaces the URL, so `before:history:update` does not
    // fire. `htmx:history:cache:hit` does, on `document`, before the swap
    // -- the same window, for the same reason. Measured, boosted Back
    // out of the comic reader, both browsers: **0** progress POSTs
    // before this, 1 after.
    //
    // Both are bound through `bindOutside`, so the guard that used to be
    // written out here is the general one now, and each of them also
    // drops every listener this reader left on `document` and `window`.
    //
    // Measured, both browsers: leaving by the reader's own link saved
    // page 1 -> 5 (20% -> 60%) with one POST, and `pagehide` and
    // `visibilitychange` never fired at all. Control, the same build
    // without this: page 1, 20%, zero POSTs -- exactly the loss the
    // readers used to be excluded from hx-boost to prevent.
    function leaveOnBoost() {
        saveOnExit();
        readingTime.stop();
        releaseOutside();
    }
    bindOutside(document, "htmx:before:history:update", leaveOnBoost);
    bindOutside(document, "htmx:history:cache:hit", leaveOnBoost);

    // ---------- Input ----------

    function handleKeydown(e) {
        readerFullscreen.armFromGesture();
        if (customMode) return; // navigation there is the scroll gesture itself
        // A key meant for a control is not a page turn: the arrows on a
        // focused slider (line spacing, width) changed the setting AND
        // turned the page, and Space on a focused button pressed it and
        // turned the page.
        if (e.target instanceof Element
                && e.target.closest("input, select, textarea, button, [contenteditable]")) return;
        if (e.key === "ArrowRight" || e.key === " ") paginatedNext();
        if (e.key === "ArrowLeft" || e.key === "Backspace") paginatedPrev();
    }
    bindOutside(document, "keydown", handleKeydown);

    let touchStartX = null;
    const SWIPE_THRESHOLD = 50;
    function handleTouchStart(e) {
        touchStartX = e.touches[0].clientX;
        markInteraction();
    }
    function handleTouchEnd(e) {
        readerFullscreen.armFromGesture();
        if (touchStartX === null) return;
        const deltaX = e.changedTouches[0].clientX - touchStartX;
        if (!customMode && Math.abs(deltaX) > SWIPE_THRESHOLD) {
            deltaX < 0 ? paginatedNext() : paginatedPrev();
        }
        touchStartX = null;
    }
    bindOutside(document, "touchstart", handleTouchStart);
    bindOutside(document, "touchend", handleTouchEnd);
    bindOutside(document, "click", readerFullscreen.armFromGesture);
    bindOutside(document, "wheel", markInteraction, { passive: true });

    document.getElementById("btn-next").addEventListener("click", paginatedNext);
    document.getElementById("btn-prev").addEventListener("click", paginatedPrev);

    // ---------- The strip a mobile browser draws over the page ----------
    //
    // Reported from a Zenfone running Vivaldi as an installed PWA: a few
    // pixels at the bottom of the screen are covered by the browser's own
    // bar, so the last line of every paginated page sits underneath it and
    // can only be read by changing the font size until that paragraph moves
    // to another page -- once per page, which is not reading.
    //
    // The fix is one number, because BOTH the measurement and the render go
    // through #paginated-viewer: every page is laid out in an iframe
    // appended to it at height:100% (see renderPage), so shrinking that
    // element by N pixels means the page is laid out to fit N pixels less
    // AND drawn N pixels short. There is no way for the two to disagree.
    //
    // What can actually be detected:
    //
    //  - `visualViewport`: what the layout believes it has
    //    (window.innerHeight) minus what is really visible
    //    (visualViewport.height + offsetTop). A browser bar that overlays
    //    the page WITHOUT shrinking the layout viewport shows up here.
    //  - `env(safe-area-inset-bottom)`: the gesture area, which the
    //    viewport-fit=cover in these templates deliberately lets the page
    //    extend under. Read through a probe element, since CSS env() is not
    //    otherwise reachable from JS.
    //
    // A browser whose bar is invisible to both is why the manual slider
    // exists; it is a floor, not an addition, so dialling it in never
    // double-counts a band that WAS detected.
    const BOTTOM_GAP_MAX = 96; // beyond this it is not chrome (an on-screen keyboard, say)
    let safeAreaProbe = null;
    let appliedBottomGap = null;
    let lastDetectedGap = 0;

    function readSafeAreaInsetBottom() {
        if (!safeAreaProbe) {
            safeAreaProbe = document.createElement("div");
            safeAreaProbe.style.cssText =
                "position:fixed; left:0; bottom:0; width:0; visibility:hidden; " +
                "pointer-events:none; height:env(safe-area-inset-bottom, 0px);";
            document.body.appendChild(safeAreaProbe);
        }
        return Math.round(safeAreaProbe.getBoundingClientRect().height);
    }

    function detectDeadBand() {
        let detected = readSafeAreaInsetBottom();
        const vv = window.visualViewport;
        if (vv) {
            const hidden = Math.round(window.innerHeight - (vv.height + vv.offsetTop));
            // A band larger than any browser bar means something else took
            // the space (a keyboard, a pinch-zoom). Keep the last good value
            // rather than reflowing the book around a transient state.
            if (hidden > BOTTOM_GAP_MAX) return lastDetectedGap;
            detected = Math.max(detected, hidden);
        }
        lastDetectedGap = Math.max(0, Math.min(detected, BOTTOM_GAP_MAX));
        return lastDetectedGap;
    }

    function applyBottomGap({ repaginate = false } = {}) {
        const gap = Math.max(detectDeadBand(), Number(prefs.bottomGap) || 0);
        // Only a real change is worth acting on: on Android the visual
        // viewport moves constantly as the browser's bar slides, and
        // repaginating on every one of those would reflow mid-sentence.
        if (appliedBottomGap !== null && Math.abs(gap - appliedBottomGap) < 2) return;
        appliedBottomGap = gap;
        if (paginatedViewerEl) paginatedViewerEl.style.bottom = gap + "px";
        if (customScrollEl) customScrollEl.style.bottom = gap + "px";
        if (repaginate) reapplyPrefsToCurrentView();
    }

    let gapCheckTimer = null;
    function scheduleGapCheck() {
        clearTimeout(gapCheckTimer);
        gapCheckTimer = setTimeout(() => applyBottomGap({ repaginate: true }), 250);
    }
    bindOutside(window, "resize", scheduleGapCheck);
    bindOutside(window, "orientationchange", scheduleGapCheck);
    if (window.visualViewport) {
        bindOutside(window.visualViewport, "resize", scheduleGapCheck);
        bindOutside(window.visualViewport, "scroll", scheduleGapCheck);
    }

    // ---------- Settings panel ----------

    settingsButton.addEventListener("click", (e) => {
        e.stopPropagation(); // or the document listener below closes it again in the same click
        setPanelOpen(!panelOpen);
    });
    document.getElementById("settings-close").addEventListener("click", () => {
        setPanelOpen(false);
        settingsButton.focus(); // the panel is gone; focus must not go with it
    });
    // Anywhere outside closes it -- including a tap on the book, which
    // arrives through handlePaginatedTap since the chapter is its own
    // document and does not bubble out here.
    bindOutside(document, "click", (e) => {
        if (panelOpen && !settingsPanel.contains(e.target)) setPanelOpen(false);
    });
    bindOutside(document, "keydown", (e) => {
        if (e.key === "Escape" && panelOpen) {
            setPanelOpen(false);
            settingsButton.focus();
        }
    });
    readerFullscreen.setupButton(document.getElementById("btn-fullscreen"));

    const fontSelect = document.getElementById("font-select");
    const lineHeightInput = document.getElementById("line-height");
    const widthInput = document.getElementById("reader-width");
    const flowSelect = document.getElementById("flow-select");
    const bottomGapInput = document.getElementById("bottom-gap");
    const bottomGapValue = document.getElementById("bottom-gap-value");
    const fontSizeValue = document.getElementById("font-size-value");
    const fontSizeDec = document.getElementById("font-size-dec");
    const fontSizeInc = document.getElementById("font-size-inc");

    // Text size is stepped rather than dragged: it is the setting reached
    // most often, a range's handle is the smallest target on the panel,
    // and "one notch bigger" is the whole question being asked. The ends
    // are the ones the slider had, and a button at its end is DISABLED
    // rather than silently doing nothing.
    const FONT_SIZE_MIN = 80;
    const FONT_SIZE_MAX = 200;
    const FONT_SIZE_STEP = 10;

    function renderFontSize() {
        if (fontSizeValue) fontSizeValue.textContent = `${prefs.fontSize}%`;
        if (fontSizeDec) fontSizeDec.disabled = Number(prefs.fontSize) <= FONT_SIZE_MIN;
        if (fontSizeInc) fontSizeInc.disabled = Number(prefs.fontSize) >= FONT_SIZE_MAX;
    }

    function stepFontSize(delta) {
        const next = Math.max(FONT_SIZE_MIN, Math.min(FONT_SIZE_MAX, Number(prefs.fontSize) + delta));
        if (next === Number(prefs.fontSize)) return;
        prefs.fontSize = next;
        savePrefs(prefs);
        renderFontSize();
        reapplyPrefsToCurrentView();
    }
    if (fontSizeDec) fontSizeDec.addEventListener("click", () => stepFontSize(-FONT_SIZE_STEP));
    if (fontSizeInc) fontSizeInc.addEventListener("click", () => stepFontSize(FONT_SIZE_STEP));

    // A display-setting change affects how much text fits per page, so
    // Paginated mode needs to recompute the current page from scratch --
    // same "restart from current position" principle already used for the
    // comic reader's fit-change. Continuous scroll doesn't need a rebuild:
    // its layout adjusts on its own (no fixed page boundaries to recompute).
    function reapplyPrefsToCurrentView() {
        const position = customMode && mountedFrom >= 0 ? measureCustomPosition() : null;
        timingReflowUntil = performance.now() + 500;
        readingTime.interrupt();
        applyShellTheme();
        reapplyCustomTheme(); // harmless no-op if no scroll chapters are mounted yet
        if (!customMode && pgSpineIndex !== null) gotoPaginatedForward(pgSpineIndex, pgPageStart);
        if (position) remeasureCustomSections(position).catch(error => {
            customReflowing = false;
            warnChapterNotLoaded(error);
        });
    }

    fontSelect.addEventListener("change", (e) => { prefs.font = e.target.value; savePrefs(prefs); reapplyPrefsToCurrentView(); });
    lineHeightInput.addEventListener("input", (e) => { prefs.lineHeight = e.target.value; savePrefs(prefs); reapplyPrefsToCurrentView(); });
    widthInput.addEventListener("input", (e) => { prefs.width = e.target.value; savePrefs(prefs); reapplyPrefsToCurrentView(); });
    document.getElementById("hyphenation-enabled").addEventListener('change', (event) => {
        prefs.hyphenation = event.target.checked;
        savePrefs(prefs);
        renderWorkPrefs();
        reapplyPrefsToCurrentView();
    });
    document.getElementById("hyphenation-language").addEventListener('change', (event) => {
        const language = canonicalLanguage(event.target.value);
        event.target.setCustomValidity(event.target.value.trim() && !language ? 'Use a language code such as en or pt-BR.' : '');
        if (!event.target.checkValidity()) { event.target.reportValidity(); return; }
        workPrefs.language = language;
        saveWorkPrefs();
        renderWorkPrefs();
        reapplyPrefsToCurrentView();
    });
    flowSelect.addEventListener("change", (e) => { applyFlow(e.target.value); });
    if (bottomGapInput) {
        bottomGapInput.addEventListener("input", (e) => {
            prefs.bottomGap = Number(e.target.value) || 0;
            savePrefs(prefs);
            renderBottomGapValue();
            applyBottomGap({ repaginate: true });
        });
    }

    fontSelect.value = prefs.font;
    renderFontSize();
    lineHeightInput.value = prefs.lineHeight;
    widthInput.value = prefs.width;
    flowSelect.value = prefs.flow;
    renderWorkPrefs();
    // The reported total, not just the manual part -- what the reader wants
    // to know is how much room is being kept clear, however it was arrived
    // at, so a phone that needs nothing shows 0 and stays out of the way.
    function renderBottomGapValue() {
        if (!bottomGapValue) return;
        const total = Math.max(detectDeadBand(), Number(prefs.bottomGap) || 0);
        bottomGapValue.textContent = `${total}px`;
    }
    if (bottomGapInput) bottomGapInput.value = prefs.bottomGap;
    renderBottomGapValue();
    // Before init(), so the very first page is measured against the real
    // usable height instead of being laid out and then reflowed.
    applyBottomGap();
    applyShellTheme();
    updateControlsVisibility();

    // ---------- Init ----------

    console.log("[reader]", source, "starting, item", itemId, "saved location:", startLocationRaw || "(none)", "spine index:", startSpineIndex);

    async function init() {
        await fetchSpine();
        if (prefs.flow === "scrolled-doc") {
            customMode = true;
            document.getElementById("custom-scroll").classList.remove("hidden");
            paginatedViewerEl.classList.add("hidden");
            await initCustomScroll();
        } else {
            customMode = false;
            paginatedViewerEl.classList.remove("hidden");
            await initPaginated(startSpineIndex || 0, startPosition);
        }
    }

    init().catch((err) => {
        console.error("[reader] failed to load:", err);
        if (loadingIndicator) {
            loadingIndicator.textContent =
                "Couldn't open this. Detail: " + (err && err.message ? err.message : String(err)) +
                " " + MALFORMED_HINT;
        }
    });

    let isNavigatingAway = false;
    bindOutside(window, "pagehide", () => { isNavigatingAway = true; }, { capture: true });
    bindOutside(document, "visibilitychange", () => {
        if (document.visibilityState === "hidden") isNavigatingAway = true;
    });
    bindOutside(window, "unhandledrejection", (event) => {
        if (isNavigatingAway) return;
        const name = event.reason && event.reason.name;
        if (name === "AbortError") return;
        showLoadWarning("Part of the content didn't load correctly. " + MALFORMED_HINT);
    });
})();
