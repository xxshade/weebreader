/**
 * Comic/manga reader -- direct page fetch, outside the HTMX cycle.
 *
 * Modes: single page, double page (with configurable western/manga
 * direction), and virtualized continuous scroll (IntersectionObserver
 * mounts/unmounts <img> outside the viewport -- essential for long webtoon
 * series on weak hardware).
 *
 * In every mode: a sliding window of pages preloaded as Blob/ObjectURL,
 * revoking URLs outside the window to keep memory in check.
 */
(async () => {
    const root = document.getElementById("reader-root");
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

    // The chapter single/double page mode is currently showing. Mutable:
    // reaching the end of a chapter swaps the next one in place rather
    // than navigating (see advanceToNextChapter), so these three follow
    // whatever is on screen instead of being pinned to the item the page
    // was originally opened with.
    let itemId = root.dataset.itemId;
    let pageCount = parseInt(root.dataset.pageCount || "0", 10);
    let nextItemId = root.dataset.nextItemId || null; // next item in the series, if any

    const elSingle = document.getElementById("page-single");
    const elDouble = document.getElementById("page-double");
    const elScroll = document.getElementById("page-scroll");
    const imgSingle = document.getElementById("page-image");
    const imgRight = document.getElementById("page-image-right");
    const imgLeft = document.getElementById("page-image-left");
    const indicator = document.getElementById("page-indicator");
    const controls = document.getElementById("controls");

    // Times each page visit and posts the spans (see reading-time.js).
    // Created here rather than per-mode: the reader hands over between
    // chapters without reloading the document, so one tracker spans the
    // whole session.
    const readingTime = createReadingTimeTracker();

    const PRELOAD_RADIUS = 2;
    const dimensions = new Map();  // pageIndex -> {width, height}

    // itemId -> the hash of the file behind it, which every page URL
    // carries as `?v=`. Without it the server cannot answer `immutable`
    // honestly: /reader/7/page/3 goes on naming page 3 of item 7 after the
    // file is replaced, so a browser told "never revalidate" would serve
    // the old scan for a year. Per item, not one global value, because
    // scroll mode holds several chapters at once.
    const versions = new Map();
    if (root.dataset.version) versions.set(String(itemId), root.dataset.version);

    function pageUrlFor(chapterItemId, pageIndex) {
        const version = versions.get(String(chapterItemId));
        const query = version ? `?v=${encodeURIComponent(version)}` : "";
        return `/reader/${chapterItemId}/page/${pageIndex}${query}`;
    }

    // ---------- Every page image, in every mode ----------
    //
    // One store, keyed by (item, page). There used to be two -- the paged
    // modes' by page index, scroll mode's by item and page -- and three
    // things went wrong between them:
    //
    //  - A page still being fetched was only marked as such, so asking for
    //    it again returned at once with nothing: a turn onto a page that
    //    was preloading kept the OLD image on screen while the indicator
    //    and the saved progress moved on (both browsers). A request in
    //    flight is now its promise, and asking again waits for it.
    //  - Nothing was released when the reader was left by a boosted link
    //    (the document never unloads) or when the mode or the fit changed:
    //    measured, 21, then 42, then 63 live blob URLs after three visits.
    //  - A page fetched for one chapter could land in the other's cache
    //    after a handover, under a bare page index.
    //
    // What is kept is decided by the modes (evictOutsideWindow,
    // evictScrollOutside); everything goes on the way out (leaveOnBoost).
    const pageBlobs = new Map();     // "itemId:page" -> objectURL
    const pageRequests = new Map();  // "itemId:page" -> Promise<objectURL | null>
    let storeEpoch = 0;              // bumped by releaseAllPages: a late answer is dropped

    function pageKey(chapterItemId, pageIndex) {
        return `${chapterItemId}:${pageIndex}`;
    }

    function pageBlob(chapterItemId, pageIndex) {
        return pageBlobs.get(pageKey(chapterItemId, pageIndex));
    }

    function loadPage(chapterItemId, pageIndex, chapterPageCount) {
        if (pageIndex < 0 || pageIndex >= chapterPageCount) return Promise.resolve(null);
        const key = pageKey(chapterItemId, pageIndex);
        if (pageBlobs.has(key)) return Promise.resolve(pageBlobs.get(key));
        if (pageRequests.has(key)) return pageRequests.get(key);
        const epoch = storeEpoch;
        // cache: "force-cache" stops the browser from sending a
        // conditional request (If-None-Match) even when the resource is
        // already marked immutable -- without this, in some situations
        // (hard refresh, cache pressure) the browser still revalidates,
        // the server responds 304, and `Response.ok` is FALSE for a 304
        // (it's not 2xx) -- so the page was never cached while
        // currentPage and the text indicator moved on regardless: the
        // number at the top advanced and the image stayed stuck.
        // force-cache guarantees an existing cache entry is used directly
        // (as a 200, with a body) without ever risking a 304.
        const request = fetch(pageUrlFor(chapterItemId, pageIndex), { cache: "force-cache" })
            .then((res) => (res.ok ? res.blob() : null))
            .then((blob) => {
                if (!blob || epoch !== storeEpoch) return null;
                const url = URL.createObjectURL(blob);
                pageBlobs.set(key, url);
                return url;
            })
            .catch(() => null)
            .finally(() => {
                if (pageRequests.get(key) === request) pageRequests.delete(key);
            });
        pageRequests.set(key, request);
        return request;
    }

    // Revokes every stored page `keep` does not want.
    function releasePages(keep) {
        for (const [key, url] of pageBlobs) {
            if (keep(key)) continue;
            URL.revokeObjectURL(url);
            pageBlobs.delete(key);
        }
    }

    function releaseAllPages() {
        storeEpoch++;
        releasePages(() => false);
        pageRequests.clear();
    }

    const PREFS_KEY = "comic-reader-prefs"; // device-level display
    // preference (reading mode, direction) -- not reading progress (that
    // already syncs via the server), so localStorage is appropriate here.
    function loadPrefs() {
        try {
            return JSON.parse(localStorage.getItem(PREFS_KEY)) || {};
        } catch {
            return {};
        }
    }
    function savePrefs(prefs) {
        try {
            localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
        } catch {
            // localStorage unavailable (private mode etc.) -- proceed without persisting
        }
    }
    const readerPrefs = Object.assign({ mode: "single", rtl: false, fit: "width" }, loadPrefs());

    let mode = readerPrefs.mode; // single | double | scroll
    let rtl = readerPrefs.rtl;   // manga reading direction (right-to-left)
    let currentPage = parseInt(root.dataset.startPage || "0", 10) || 0;
    // Asked once, on a back/forward-cache restore, so a book read on
    // another device while this tab was frozen is not shown at the page
    // this tab left off at. See reader-resume.js.
    window.readerResume?.watch(`/reader/${itemId}/progress`, root.dataset.startPercent);

    // ---------- Next chapter (single/double page mode) ----------
    // Reaching the last page/pair continues into the next item of the
    // series (computed server-side, see open_reader) WITHOUT reloading the
    // page: the next chapter's metadata, dimensions and first pages are
    // fetched while the reader is still a few pages from the end, and the
    // swap itself is then just a state change.
    //
    // This used to be `window.location.href = ...`, a full navigation:
    // a blank flash, the whole document/JS re-parsed, and every preloaded
    // page in this reader's cache thrown away right at the moment the
    // reader needed the NEXT ones. Scroll mode already avoided all of that
    // by appending (see maybeLoadNextChapter); this brings the paginated
    // modes in line with it.
    //
    // The difference from scroll mode is what "append" has to mean. There,
    // several chapters are mounted at once and `currentPage` is a global
    // index across them, because the user can physically see two chapters
    // meeting mid-scroll. Here only one page/pair is ever on screen, so
    // there is nothing to show side by side: the previous chapter's pages
    // would be unreachable markup. So the paginated reader carries one
    // chapter at a time and hands over at the boundary -- the preloading
    // is what makes it seamless, not simultaneous mounting.

    const PREFETCH_WITHIN = 3;   // start pulling the next chapter this many pages from the end
    const PREFETCH_PAGES = 2;    // how many of its opening pages to pull ahead of the swap

    let nextChapterPrefetch = null;  // { itemId, title, pageCount, nextItemId, dims }
    let prefetchingNextChapter = false;
    let advancingChapter = false;

    // Pulls the next chapter's metadata, page dimensions and first pages
    // into memory so the swap below has nothing left to wait on. Safe to
    // call repeatedly -- it keeps at most one chapter staged, and gives up
    // silently on any failure (the swap then just fetches on demand).
    async function prefetchNextChapter() {
        if (!nextItemId || prefetchingNextChapter) return;
        if (nextChapterPrefetch && nextChapterPrefetch.itemId === nextItemId) return;
        prefetchingNextChapter = true;
        try {
            const info = await fetchChapterInfo(nextItemId);
            if (!info || !info.page_count) return;
            const dims = await fetchChapterDimensions(info.id);
            // Into the shared store, under the next chapter's own id -- kept
            // there by evictOutsideWindow while it is the staged one.
            nextChapterPrefetch = {
                itemId: String(info.id), title: info.title, pageCount: info.page_count,
                nextItemId: info.next_item_id ? String(info.next_item_id) : null,
                dims,
            };
            for (let i = 0; i < Math.min(PREFETCH_PAGES, info.page_count); i++) {
                await loadPage(info.id, i, info.page_count);
            }
        } catch {
            nextChapterPrefetch = null;
        } finally {
            prefetchingNextChapter = false;
        }
    }

    // Repoints the paginated reader at a different chapter. Everything
    // keyed by page index -- the dimensions map, the double-page spread
    // boundaries -- belongs to the chapter being left, so it is dropped
    // here; `seedDims`, when the caller already has them, refill it
    // without a round trip. The page images are keyed by item, so the
    // next evictOutsideWindow releases the old chapter's.
    //
    // #reader-root is updated too. It is what the reader initialises from,
    // and htmx's history cache restores a COPY of it: left as the chapter
    // this page was opened with, Back into the reader after a handover
    // re-opened that one (chapter 1 at "20 / 20" instead of chapter 2,
    // page 3), and reader-resume asked about the wrong chapter.
    //
    // replaceState, not pushState: moving through a long series shouldn't
    // bury the page the reader was opened from under a stack of history
    // entries -- same reasoning as scroll mode's
    // updateScrollIndicatorAndUrl.
    function becomeChapter(chapter, localIndex, seedDims) {
        dimensions.clear();
        spreadStartsPromise = null;
        nextChapterPrefetch = null;

        itemId = String(chapter.itemId);
        pageCount = chapter.pageCount;
        nextItemId = chapter.nextItemId ? String(chapter.nextItemId) : null;
        currentPage = localIndex;

        rememberPosition();
        window.readerResume?.watch(`/reader/${itemId}/progress`, root.dataset.startPercent);

        if (seedDims) {
            seedDims.forEach((d, i) => {
                if (d && d.width && d.height) dimensions.set(i, { width: d.width, height: d.height });
            });
        }

        history.replaceState(null, "", `/reader/${itemId}`);
        if (chapter.title) document.title = chapter.title;
    }

    // "There is no next one", which the reader used to answer with
    // silence: advanceToNextChapter() returned at its first line and the
    // page simply did not move. The element is rendered by the template
    // (see #series-end in comic.html); this decides where it goes.
    //
    // Over the page in the paged modes, because there the page TURN is
    // the question being asked and the answer belongs where the turn
    // happened -- dismissed by a tap, so nobody is trapped behind it. At
    // the foot of the strip in Continuous scroll, where there is no turn
    // to answer and below the last page is the only place it means
    // anything.
    const seriesEnd = document.getElementById("series-end");
    // Opaque black rather than a wash: the page behind it is letterboxed
    // against the same black, and a translucent utility has no rule in the
    // frozen app.css (tests/test_css_classes.py).
    const OVER_THE_PAGE = ["absolute", "inset-0", "z-20", "bg-black", "flex"];

    function showSeriesEndOverPage() {
        if (!seriesEnd || !seriesEnd.classList.contains("hidden")) return;
        seriesEnd.classList.remove("hidden");
        seriesEnd.classList.add(...OVER_THE_PAGE);
    }

    function hideSeriesEnd() {
        if (!seriesEnd) return;
        seriesEnd.classList.add("hidden");
        seriesEnd.classList.remove(...OVER_THE_PAGE);
    }

    function placeSeriesEndBelow(container) {
        if (!seriesEnd || seriesEnd.parentElement === container) return;
        seriesEnd.classList.remove("hidden", ...OVER_THE_PAGE);
        seriesEnd.classList.add("flex");
        container.appendChild(seriesEnd);
    }

    // Scroll mode empties its container to rebuild, and the notice may be
    // living inside it by then -- without this, switching modes would
    // delete it from the document and the paged modes would have nothing
    // left to show.
    function reclaimSeriesEnd() {
        if (!seriesEnd) return;
        if (seriesEnd.parentElement !== root) root.appendChild(seriesEnd);
        hideSeriesEnd();
    }

    if (seriesEnd) {
        seriesEnd.addEventListener("click", (event) => {
            // The link goes where it says. In the strip this is not a
            // curtain over anything, so there is nothing to dismiss.
            if (event.target.closest("a")) return;
            if (seriesEnd.parentElement !== root) return;
            event.stopPropagation();
            hideSeriesEnd();
        });
    }

    // Hands over to the staged next chapter at its first page.
    async function advanceToNextChapter() {
        if (!nextItemId) {
            // The last volume is finished too. In double mode its final
            // spread usually starts before the last page, so without this
            // the volume stayed "reading" at 90% however far the reader
            // got -- the case asFinished exists for (both browsers).
            saveProgress(false, true);
            showSeriesEndOverPage();
            return;
        }
        if (advancingChapter) return;
        advancingChapter = true;
        try {
            // Record the finished chapter as read before its id is gone.
            saveProgress(false, true);

            if (!nextChapterPrefetch || nextChapterPrefetch.itemId !== nextItemId) {
                await prefetchNextChapter();
            }
            const staged = nextChapterPrefetch;
            if (!staged || staged.itemId !== nextItemId) {
                // Couldn't stage it (offline, server error) -- fall back to
                // the old behaviour rather than silently refusing to
                // advance, so the reader still gets to the next chapter.
                window.location.href = `/reader/${nextItemId}`;
                return;
            }

            becomeChapter(staged, 0, staged.dims);
            await showPage(0);
        } finally {
            advancingChapter = false;
        }
    }

    // Called after every page turn in single/double mode: once the reader
    // is within PREFETCH_WITHIN pages of the end, start staging what comes
    // next so the boundary itself is instant.
    function maybePrefetchNextChapter() {
        if (mode === "scroll" || !nextItemId) return;
        if (currentPage >= pageCount - PREFETCH_WITHIN) prefetchNextChapter();
    }

    function pageUrl(pageIndex) {
        return pageUrlFor(itemId, pageIndex);
    }

    function fetchPage(pageIndex) {
        return loadPage(itemId, pageIndex, pageCount);
    }

    // The paged modes keep their own chapter's pages around the one on
    // screen (double mode preloads one further, so one further is kept)
    // and the staged next chapter's; anything else -- the previous
    // chapter, scroll mode's other chapters -- is released.
    function evictOutsideWindow(centerIndex) {
        const here = String(itemId);
        const staged = nextChapterPrefetch ? nextChapterPrefetch.itemId : null;
        releasePages((key) => {
            const [item, page] = key.split(":");
            if (item === staged) return true;
            return item === here && Math.abs(Number(page) - centerIndex) <= PRELOAD_RADIUS + 1;
        });
        // `dimensions` is deliberately NOT evicted with the blobs: it is
        // two numbers per page, it came from one request for the whole
        // chapter, and dropping it would mean asking for it again.
    }

    // Fills `dimensions` for this chapter from /reader/{id}/dimensions --
    // the sizes the server measured once and cached in page_dimensions.
    //
    // They used to arrive one at a time, on an X-Page-Dimensions header
    // the page route computed by opening each image in Pillow as it served
    // it. That was the same numbers, measured again, per page turn, on the
    // request that is already the hottest one in the app -- while the
    // endpoint that has all of them is the one double-page mode reads its
    // spread boundaries from anyway.
    //
    // Resolves to nothing useful if the request fails, and isLandscape
    // then answers false for every page: the same fallback as a header
    // that did not arrive, which is pairing pages two at a time.
    async function ensureDimensions() {
        const dims = await fetchChapterDimensions(itemId);
        if (!dims || !dims.length) return;
        dims.forEach((d, i) => {
            if (d && d.width && d.height) dimensions.set(i, { width: d.width, height: d.height });
        });
    }

    function isLandscape(pageIndex) {
        const d = dimensions.get(pageIndex);
        return d ? d.width > d.height : false;
    }

    // ---------- Size fit (single/double page) ----------
    // Continuous scroll also honors this preference (see
    // verticalFitWidthCSS, further below) -- it uses a separate function
    // because there the size is computed per page, at the moment each
    // placeholder is created (part of what guarantees scroll never needs to
    // readjust itself afterward), instead of being applied to a fixed <img>
    // on screen like here.
    //
    // slotMaxWidthClass is the horizontal "slot" the image occupies:
    // 'max-w-full' (single page, or a landscape spread occupying both slots
    // in double mode) or 'max-w-[49%]' (half of a paired double mode). In
    // fit="height", width still respects this ceiling -- without that, a
    // page much wider than tall in paired double mode would spill into the
    // neighboring page's space.
    function fitImageClasses(slotMaxWidthClass) {
        const classes = ["select-none"];
        if (readerPrefs.fit === "height") {
            classes.push("h-full", "max-h-full", "w-auto", slotMaxWidthClass, "object-contain");
        } else if (readerPrefs.fit === "original") {
            // actual size, no scaling -- the container (overflow: auto) is
            // scrollable on both axes to reach the edges
            classes.push("w-auto", "h-auto", "max-w-none", "max-h-none");
        } else {
            // "width" (default)
            classes.push(slotMaxWidthClass === "max-w-full" ? "w-full" : slotMaxWidthClass, "h-auto", "max-h-none");
        }
        return classes;
    }

    function applyFit() {
        imgSingle.className = fitImageClasses("max-w-full").join(" ");
        imgRight.className = fitImageClasses(imgRight.dataset.landscape === "1" ? "max-w-full" : "max-w-[49%]").join(" ");
        imgLeft.className = fitImageClasses("max-w-[49%]").join(" ");

        document.querySelectorAll(".fit-btn").forEach((btn) => {
            btn.classList.toggle("bg-indigo-600", btn.dataset.fit === readerPrefs.fit);
            btn.classList.toggle("bg-neutral-800", btn.dataset.fit !== readerPrefs.fit);
        });
    }

    // ---------- Single page mode ----------

    // recordProgress=false for the initial display case (resuming the saved
    // page on open) -- opening the book isn't "advancing reading", and
    // firing a progress save there was redundant at best and a
    // plausible source of a race/spurious save at worst.
    async function showSingle(pageIndex, recordProgress = true) {
        currentPage = Math.max(0, Math.min(pageCount - 1, pageIndex));
        const page = currentPage;
        const item = itemId;
        const url = await fetchPage(page);
        // Overtaken by a later turn, a handover or a mode switch while the
        // page was arriving: that one owns the screen now.
        if (currentPage !== page || itemId !== item || mode !== "single") return;
        if (url) imgSingle.src = url;
        applyFit();
        indicator.textContent = `${currentPage + 1} / ${pageCount}`;

        for (let offset = 1; offset <= PRELOAD_RADIUS; offset++) {
            fetchPage(currentPage + offset);
            fetchPage(currentPage - offset);
        }
        evictOutsideWindow(currentPage);
        maybePrefetchNextChapter();
        trackVisiblePage();
        if (recordProgress) saveProgress();
    }

    // ---------- Double page mode ----------
    // A landscape page (a spread already merged in the source file) fills
    // both slots by itself; portrait pages are paired up two at a time.

    async function showDouble(pageIndex, recordProgress = true) {
        currentPage = Math.max(0, Math.min(pageCount - 1, pageIndex));
        const page = currentPage;
        const item = itemId;
        const overtaken = () => currentPage !== page || itemId !== item || mode !== "double";
        // Before asking which way this page is turned: the sizes now come
        // from one request for the whole chapter rather than riding along
        // on each page (see ensureDimensions). Cached after the first
        // call, so every later page turn is already answered.
        const [urlA] = await Promise.all([fetchPage(page), ensureDimensions()]);
        if (overtaken()) return;

        const landscape = isLandscape(page);
        imgRight.dataset.landscape = landscape ? "1" : "0";

        if (landscape) {
            imgRight.src = urlA || "";
            imgLeft.src = "";
        } else {
            // A landscape page is never anyone's partner: it is shown alone
            // (see getSpreadStarts, which walks the same rule). Paired, it
            // was squeezed into half the screen beside its neighbour and
            // never shown on its own.
            const partnerIndex = page + 1 < pageCount && !isLandscape(page + 1) ? page + 1 : null;
            const urlB = partnerIndex !== null ? await fetchPage(partnerIndex) : null;
            if (overtaken()) return;

            // rtl: the "older" page (currentPage) sits on the right
            const [urlLeftSlot, urlRightSlot] = rtl ? [urlB, urlA] : [urlA, urlB];
            imgLeft.src = urlLeftSlot || "";
            imgRight.src = urlRightSlot || "";
        }
        applyFit();

        indicator.textContent = `${currentPage + 1} / ${pageCount}`;
        for (let offset = 1; offset <= PRELOAD_RADIUS + 1; offset++) {
            fetchPage(currentPage + offset);
            fetchPage(currentPage - offset);
        }
        evictOutsideWindow(currentPage);
        maybePrefetchNextChapter();
        trackVisiblePage();
        if (recordProgress) saveProgress();
    }

    // ---------- Continuous scroll mode (virtualized, vertical, infinite
    // across chapters) ----------
    // Every placeholder is born already at its FINAL size -- via CSS
    // `aspect-ratio`, from the real dimensions fetched from
    // /reader/{itemId}/dimensions BEFORE anything is drawn -- instead of
    // starting at an estimated height (40vh) that only gets corrected once
    // the real image loads later.
    //
    // That was the root of every scroll bug in earlier rounds: with no
    // layout growth/shrinkage after the initial mount, there's NOTHING LEFT
    // to compensate for -- the IntersectionObserver still decides only when
    // to mount/unmount the real <img> (memory management), but that never
    // moves anyone's scroll, because the space was already reserved at the
    // right size from the start. The problem was never the compensation
    // formula -- it was needing a compensation at all.
    //
    // Only when a specific page's dimensions couldn't be read (e.g. a
    // corrupted file) do we fall back to the old estimated placeholder for
    // THAT page -- and only in that rare case does the compensation logic
    // below still kick in.
    //
    // INFINITE SCROLL ACROSS CHAPTERS: reaching the last page used to
    // navigate to the next item (goToNextChapter, a full page reload).
    // Here, instead, the next chapter's pages are fetched and appended
    // into this SAME container -- same virtualization, same placeholder
    // technique, just spanning more than one item's worth of pages.
    // `chapters` tracks every chapter currently mounted; `currentPage`
    // (already used elsewhere in this file as "the current page") becomes
    // a GLOBAL index spanning all of them, and resolveGlobalIndex() maps
    // a global index back to (chapter, local index within that chapter)
    // whenever a page actually needs to be fetched, saved, etc. Single
    // and double page mode are untouched by any of this -- they only ever
    // know about the original item, exactly as before.

    let scrollObserver = null;
    let activeScrollContainer = null;
    let scrollHandlerCleanup = null;
    const chapterDimensionsPromise = new Map(); // itemId -> Promise<[{width,height}, ...]>

    // [{ itemId, title, pageCount, startIndex, nextItemId }], in scroll
    // order. startIndex is that chapter's first page's GLOBAL index.
    // nextItemId is carried per-chapter (not just once globally) because
    // each newly appended chapter needs to know what comes after IT, to
    // keep the infinite scroll going.
    let chapters = [];
    let activeChapterIndex = 0;   // index into `chapters` currently in view -- drives the URL/title
    let loadingNextChapter = false;


    function fetchChapterDimensions(chapterItemId) {
        if (!chapterDimensionsPromise.has(chapterItemId)) {
            chapterDimensionsPromise.set(chapterItemId,
                fetch(`/reader/${chapterItemId}/dimensions`)
                    .then((res) => (res.ok ? res.json() : { pages: [] }))
                    .then((data) => data.pages || [])
                    .catch(() => [])
            );
        }
        return chapterDimensionsPromise.get(chapterItemId);
    }

    async function fetchChapterInfo(chapterItemId) {
        try {
            const res = await fetch(`/reader/${chapterItemId}/chapter-info`);
            if (!res.ok) return null;
            const info = await res.json();
            // Every chapter this reader appends or hands over to needs its
            // own file version before any of its pages are fetched -- see
            // `versions` above.
            if (info && info.version) versions.set(String(info.id), info.version);
            return info;
        } catch {
            return null;
        }
    }

    // Scroll mode keeps the pages around the one in view, and any page
    // still mounted; the rest of every chapter is released as the reader
    // moves on. It used to be released a whole chapter at a time, and only
    // once a fourth chapter was appended: 66 blobs alive with 20-34 images
    // mounted after 64 pages.
    function evictScrollOutside(centerGlobal) {
        const container = activeScrollContainer;
        if (!container) return;
        releasePages((key) => {
            const [item, local] = key.split(":");
            const chapter = chapters.find((c) => String(c.itemId) === item);
            if (!chapter) return false;
            const globalIndex = chapter.startIndex + Number(local);
            if (Math.abs(globalIndex - centerGlobal) <= PRELOAD_RADIUS + 2) return true;
            const placeholder = container.children[globalIndex];
            return !!(placeholder && Number(placeholder.dataset.globalIndex) === globalIndex
                      && placeholder.querySelector("img"));
        });
    }

    // Maps a GLOBAL index (spanning every currently-mounted chapter) to
    // which chapter it falls in and the local page index within it.
    function resolveGlobalIndex(globalIndex) {
        for (let i = chapters.length - 1; i >= 0; i--) {
            if (chapters[i].startIndex <= globalIndex) {
                return { chapter: chapters[i], chapterIdx: i, localIndex: globalIndex - chapters[i].startIndex };
            }
        }
        return { chapter: chapters[0], chapterIdx: 0, localIndex: globalIndex };
    }

    // How close to the very bottom counts as "the reader saw the end".
    // Not 0: browsers report fractional scroll geometry (zoom, device pixel
    // ratio), so an exact comparison can be off by a pixel or two and never
    // match.
    const BOTTOM_SLACK_PX = 8;

    // Chapters already reported finished, so scrolling around the boundary
    // doesn't re-post the same thing on every frame.
    const finishedChapters = new Set();

    function markChapterFinished(chapter) {
        if (!chapter || !chapter.pageCount) return;
        if (finishedChapters.has(chapter.itemId)) return;
        finishedChapters.add(chapter.itemId);
        postProgress(chapter.itemId, chapter.pageCount - 1, chapter.pageCount);
    }

    function totalMountedPageCount() {
        const last = chapters[chapters.length - 1];
        return last.startIndex + last.pageCount;
    }

    // Only used by the fallback path (unknown dimension) -- see comment
    // above. Counts pages BEFORE the current one whose fallback image has
    // started mounting but hasn't finished yet (i.e. whose compensation
    // hasn't been applied yet); a measurement taken in that window can only
    // UNDERESTIMATE the current page, never overestimate it.
    let pendingPrecedingMounts = 0;

    // Single source of truth for "current page": whichever page has its
    // start/end touching the TOP of the screen -- the same reference used
    // by scrollIntoView on open, so opening and measuring always agree, and
    // the user sees the start of page X. Returns a GLOBAL index.
    function measureCurrentScrollPage() {
        const container = activeScrollContainer;
        if (!container || !container.children.length) return currentPage;

        // The top-of-viewport rule cannot name a final page SHORTER than
        // the screen. Scrolled as far as it goes, that page sits at the
        // BOTTOM and the top of the screen is still inside the page (or
        // two) before it -- so the last page is never current, the counter
        // stops one short, and the volume is never finished. Measured on a
        // 390x844 viewport with pages of 585px and a 341px credits page
        // (58% of a normal one, exactly the shape reported): at
        // fromBottom = 0 the indicator read "4 / 5" and the row saved was
        // `reading`, 80%.
        //
        // At the end of the scroll range there is nothing below left to
        // reach, so what the reader has got to is simply the last page --
        // the same reasoning as the paginated modes' asFinished, and as
        // the markChapterFinished call in onScroll, which was the earlier
        // half of this fix: it marked the chapter read but left the
        // measurement wrong, so the next save (or the pagehide beacon)
        // posted `reading` over it from the page the top rule still named.
        //
        // A tall final page is unaffected: at the bottom of one the top of
        // the screen is already inside it, so both rules agree.
        if (container.scrollHeight - container.scrollTop - container.clientHeight < BOTTOM_SLACK_PX) {
            const last = container.children[container.children.length - 1];
            const lastIndex = parseInt(last.dataset.globalIndex, 10);
            if (Number.isFinite(lastIndex)) return lastIndex;
        }

        const referencePoint = container.getBoundingClientRect().top;

        let best = currentPage;
        let bestDistance = Infinity;
        for (const child of container.children) {
            const rect = child.getBoundingClientRect();
            if (referencePoint >= rect.top && referencePoint < rect.bottom) {
                return parseInt(child.dataset.globalIndex, 10);
            }
            const distance = referencePoint < rect.top ? rect.top - referencePoint : referencePoint - rect.bottom;
            if (distance < bestDistance) {
                bestDistance = distance;
                best = parseInt(child.dataset.globalIndex, 10);
            }
        }
        return best;
    }

    // Safe version for deciding what to SAVE -- only matters while there's
    // still a pending fallback placeholder (see pendingPrecedingMounts);
    // with known dimensions this is always 0 and this function is just a
    // passthrough.
    function measureCurrentScrollPageSafe() {
        const measured = measureCurrentScrollPage();
        if (pendingPrecedingMounts > 0 && measured < currentPage) return currentPage;
        return measured;
    }

    // Placeholder width according to the chosen fit (same preference used
    // in single/double page -- see fitImageClasses). Still 100% CSS
    // (calc/min), with NO resize JS at all: the browser recalculates on its
    // own on resize/screen rotation, exactly like plain aspect-ratio already
    // did for fit="width" -- it doesn't reintroduce any kind of calculation
    // that would need to be redone manually afterward.
    function verticalFitWidthCSS(width, height) {
        if (readerPrefs.fit === "original") {
            // actual size; never wider than the container (otherwise it
            // would need horizontal scrolling, which this container doesn't have)
            return `min(${width}px, 100%)`;
        }
        if (readerPrefs.fit === "height") {
            // width that results in height = full screen, but never wider
            // than the container
            return `min(calc(100dvh * (${width} / ${height})), 100%)`;
        }
        return "100%"; // "width" (default)
    }

    // Builds and appends the placeholders for one chapter's pages into the
    // scroll container -- used both for the first chapter (setupScrollMode)
    // and for every chapter appended afterward (maybeLoadNextChapter).
    // Returns the newly created elements so the caller can register them
    // with the IntersectionObserver.
    function appendChapterPlaceholders(container, chapterEntry, dims) {
        const created = [];
        for (let i = 0; i < chapterEntry.pageCount; i++) {
            const placeholder = document.createElement("div");
            const d = dims[i];
            if (d && d.width > 0 && d.height > 0) {
                // Final size right from the first layout: width comes from
                // the chosen fit, height is derived via aspect-ratio -- the
                // SAME size the real <img> will occupy once mounted, so
                // mounting/unmounting the image never resizes the
                // placeholder.
                //
                // "mx-auto": when the fit makes the page narrower than the
                // container (height/original), the placeholder (a plain
                // block, not flex) doesn't center itself -- margin auto on
                // both sides does that; has no effect when width is already
                // 100%.
                placeholder.className = "mx-auto flex items-center justify-center";
                placeholder.style.width = verticalFitWidthCSS(d.width, d.height);
                placeholder.style.aspectRatio = `${d.width} / ${d.height}`;
            } else {
                // fallback: this page's dimensions couldn't be read --
                // without knowing the size in advance there's no way to
                // apply the chosen fit here; falls back to the old
                // behavior (estimate + compensation) until this page mounts.
                placeholder.className = "min-h-[40vh] flex items-center justify-center";
            }
            placeholder.dataset.globalIndex = chapterEntry.startIndex + i;
            placeholder.dataset.itemId = chapterEntry.itemId;
            placeholder.dataset.localIndex = i;
            container.appendChild(placeholder);
            created.push(placeholder);
        }
        return created;
    }

    // Where a fresh call to setupScrollMode() should (re)start from: the
    // very first time, that's the item/page this reader opened with
    // (chapters is still empty). On later calls -- currently only a fit
    // change -- it's whichever chapter is currently active, so changing
    // display settings mid-infinite-scroll doesn't jump the reader back to
    // chapter 1.
    function currentSetupStart() {
        if (chapters.length > 0) {
            const chapter = chapters[activeChapterIndex];
            const localIndex = Math.max(0, currentPage - chapter.startIndex);
            return {
                itemId: chapter.itemId, title: chapter.title, pageCount: chapter.pageCount,
                localIndex, nextItemId: chapter.nextItemId,
            };
        }
        return { itemId, title: document.title, pageCount, localIndex: currentPage, nextItemId };
    }

    // Updates the page-count indicator (always relative to the chapter
    // currently in view, e.g. "5 / 20" -- not some odd cross-chapter
    // total) and, when the active chapter has changed, the URL/title.
    // history.replaceState (not pushState) so scrolling through many
    // chapters doesn't pile up browser-history entries -- the URL just
    // silently tracks where the reader actually is.
    function updateScrollIndicatorAndUrl(forceUrlUpdate) {
        const { chapter, chapterIdx, localIndex } = resolveGlobalIndex(currentPage);
        indicator.textContent = `${localIndex + 1} / ${chapter.pageCount}`;
        if (forceUrlUpdate || chapterIdx !== activeChapterIndex) {
            activeChapterIndex = chapterIdx;
            const url = `/reader/${chapter.itemId}`;
            if (window.location.pathname !== url) {
                history.replaceState(null, "", url);
                if (chapter.title) document.title = chapter.title;
            }
        }
    }

    async function setupScrollMode() {
        const container = elScroll;
        const start = currentSetupStart();
        const dims = await fetchChapterDimensions(start.itemId);

        // the user may have switched modes again while dimensions were
        // still being fetched -- if this isn't the active mode anymore,
        // there's no point continuing to mount
        if (mode !== "scroll") return;

        reclaimSeriesEnd();
        container.innerHTML = "";
        chapters = [{
            itemId: start.itemId, title: start.title, pageCount: start.pageCount,
            startIndex: 0, nextItemId: start.nextItemId,
        }];
        activeChapterIndex = 0;
        currentPage = start.localIndex;

        appendChapterPlaceholders(container, chapters[0], dims);

        activeScrollContainer = container;
        pendingPrecedingMounts = 0;

        // Avoids registering the scroll listener twice if the mode is
        // switched back and forth (the container is reused, not recreated).
        if (scrollHandlerCleanup) scrollHandlerCleanup();

        // Only decides whether to mount/unmount the image (lazy loading,
        // memory management) -- no longer decides which page is "current"
        // and doesn't affect layout (dimensions are already reserved).
        // The generous rootMargin here is just a preload buffer.
        if (scrollObserver) scrollObserver.disconnect();
        scrollObserver = new IntersectionObserver(
            (entries) => {
                for (const entry of entries) {
                    const globalIndex = parseInt(entry.target.dataset.globalIndex, 10);
                    if (entry.isIntersecting) {
                        mountScrollPage(entry.target, globalIndex);
                    } else if (Math.abs(globalIndex - currentPage) > PRELOAD_RADIUS) {
                        unmountScrollPage(entry.target);
                    }
                }
            },
            { root: container, rootMargin: "50% 0px", threshold: 0 }
        );
        for (const child of container.children) scrollObserver.observe(child);

        // IntersectionObserver fires an initial callback for every observed
        // element as soon as observe() is called -- this isn't the user
        // actually scrolling, it's just the mode's setup.
        let scrollInitializing = true;

        const target = container.children[start.localIndex];
        // Aligns the START of the saved page with the TOP of the screen.
        // Since every placeholder is already born at its final size, this
        // position is exact and final -- it never changes afterward, unlike
        // the previous approach (based on 40vh placeholders that only
        // gradually took on their real size).
        if (target) {
            target.scrollIntoView({ block: "start", inline: "nearest" });
        }

        currentPage = measureCurrentScrollPageSafe();
        updateScrollIndicatorAndUrl(true);
        trackVisiblePage();

        function onScroll() {
            const globalIndex = measureCurrentScrollPageSafe();
            if (globalIndex !== currentPage) {
                currentPage = globalIndex;
                updateScrollIndicatorAndUrl(false);
                const { chapter, localIndex } = resolveGlobalIndex(globalIndex);
                for (let offset = 1; offset <= PRELOAD_RADIUS; offset++) {
                    loadPage(chapter.itemId, localIndex + offset, chapter.pageCount);
                    loadPage(chapter.itemId, localIndex - offset, chapter.pageCount);
                }
                evictScrollOutside(globalIndex);
                trackVisiblePage();
                if (!scrollInitializing) saveProgress();
            }

            // Approaching the end of everything currently mounted -- append
            // the next chapter, so scrolling further just keeps going.
            //
            // This deliberately does NOT ask whether the last page is the
            // "current" one, which is what it used to require (and is why
            // it could silently never fire). `measureCurrentScrollPage`
            // defines the current page as the one crossing the TOP of the
            // viewport, so a final page SHORTER than the viewport -- normal
            // with fit="original", and for the trailing credits/afterword
            // page of many volumes -- is never current: scrolled all the
            // way down, the top of the screen is still inside the page
            // before it. Whether more content is needed is a question about
            // scroll geometry alone, so that is all this looks at now.
            //
            // One viewport of slack, rather than the old 4px, so the next
            // chapter is fetched while the reader is still approaching the
            // end instead of exactly at it.
            const fromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
            if (fromBottom < container.clientHeight) maybeLoadNextChapter();

            if (!scrollInitializing) {
                // Genuinely at the bottom: everything mounted has been
                // seen, so the last chapter counts as read. Same reasoning
                // as the paginated modes' asFinished -- reaching the end of
                // the content is what "finished" means, regardless of which
                // page the top-of-viewport rule happens to name.
                if (fromBottom < BOTTOM_SLACK_PX) {
                    markChapterFinished(chapters[chapters.length - 1]);
                }
                // Scrolled on into a later chapter -- everything before it
                // has been read through.
                const { chapterIdx } = resolveGlobalIndex(globalIndex);
                for (let i = 0; i < chapterIdx; i++) markChapterFinished(chapters[i]);
            }
        }

        let scrollRafPending = false;
        function onScrollThrottled() {
            if (scrollRafPending) return;
            scrollRafPending = true;
            requestAnimationFrame(() => {
                scrollRafPending = false;
                onScroll();
            });
        }
        container.addEventListener("scroll", onScrollThrottled, { passive: true });
        scrollHandlerCleanup = () => container.removeEventListener("scroll", onScrollThrottled);

        // Only the fallback (unknown dimension) can still grow without
        // firing a native "scroll" event -- we keep this safety net for
        // that rare case.
        onImageMounted = onScrollThrottled;

        requestAnimationFrame(() => { scrollInitializing = false; });
    }

    // Fetches the next chapter's metadata + page dimensions and appends its
    // placeholders right after whatever's currently mounted -- this is the
    // actual "infinite scroll" step, called once the user has scrolled to
    // the bottom of the last mounted chapter. Never navigates; on any
    // failure (network hiccup, or genuinely no next chapter) it just does
    // nothing, and the next time the user nudges the scroll and re-hits the
    // same boundary, it tries again.
    async function maybeLoadNextChapter() {
        if (loadingNextChapter) return;
        const lastChapter = chapters[chapters.length - 1];
        if (!lastChapter.nextItemId) {
            // End of the series. Not a failure to load anything, so it is
            // said rather than passed over in silence.
            placeSeriesEndBelow(activeScrollContainer);
            return;
        }

        loadingNextChapter = true;
        try {
            const info = await fetchChapterInfo(lastChapter.nextItemId);
            if (!info || mode !== "scroll") return;

            const dims = await fetchChapterDimensions(info.id);
            if (mode !== "scroll") return; // mode may have changed while awaiting

            const newChapter = {
                itemId: info.id,
                title: info.title,
                pageCount: info.page_count || 0,
                startIndex: totalMountedPageCount(),
                nextItemId: info.next_item_id,
            };
            if (newChapter.pageCount === 0) return; // nothing to append

            chapters.push(newChapter);
            const created = appendChapterPlaceholders(activeScrollContainer, newChapter, dims);
            for (const el of created) scrollObserver.observe(el);
            // What the earlier chapters' pages hold is released page by page
            // as the reader moves on (evictScrollOutside).
        } finally {
            loadingNextChapter = false;
        }
    }

    let onImageMounted = null;

    async function mountScrollPage(placeholder, globalIndex) {
        if (placeholder.querySelector("img")) return; // already mounted

        const { chapter, localIndex } = resolveGlobalIndex(globalIndex);
        const hasKnownDimension = !!placeholder.style.aspectRatio;
        const affectsCompensation = !hasKnownDimension && globalIndex < currentPage;
        if (affectsCompensation) pendingPrecedingMounts++;

        const url = await loadPage(chapter.itemId, localIndex, chapter.pageCount);
        // Unmounted, or the strip rebuilt (a fit change), while it arrived.
        if (!placeholder.isConnected || placeholder.querySelector("img")) {
            if (affectsCompensation) pendingPrecedingMounts = Math.max(0, pendingPrecedingMounts - 1);
            return;
        }
        if (!url) {
            if (affectsCompensation) pendingPrecedingMounts = Math.max(0, pendingPrecedingMounts - 1);
            return;
        }

        const img = document.createElement("img");
        img.src = url;
        img.draggable = false;

        if (hasKnownDimension) {
            // The space was already reserved at the right size (aspect-ratio
            // on the placeholder) -- the image just fills that space.
            // object-contain is just a safety net in case the ratio
            // reported by the server drifts a pixel off the real one due
            // to rounding.
            img.className = "w-full h-full object-contain";
            placeholder.appendChild(img);
            if (onImageMounted) onImageMounted();
            return;
        }

        // ---- Fallback: unknown dimension for this page ----
        // Same as the old approach, but now a rare exception instead of the
        // rule: the placeholder can grow/shrink as the real image
        // mounts/unmounts, requiring manual scroll compensation for pages
        // before the current one. Needs `overflow-anchor: none` on the
        // container (see comic.html) so the browser's NATIVE scroll
        // anchoring doesn't apply the SAME compensation on its own and the
        // two cancel out or stack.
        img.className = "max-w-full";
        const sizeBefore = placeholder.offsetHeight;
        const finishPending = () => {
            if (affectsCompensation) pendingPrecedingMounts = Math.max(0, pendingPrecedingMounts - 1);
        };
        img.addEventListener("load", () => {
            const container = activeScrollContainer;
            if (container) {
                const sizeAfter = placeholder.offsetHeight;
                const delta = sizeAfter - sizeBefore;
                if (delta !== 0 && affectsCompensation) {
                    container.scrollTop += delta;
                }
            }
            finishPending();
            if (onImageMounted) onImageMounted();
        }, { once: true });
        img.addEventListener("error", finishPending, { once: true });
        placeholder.appendChild(img);
    }


    function unmountScrollPage(placeholder) {
        const img = placeholder.querySelector("img");
        if (!img) return;

        // With a known dimension, the placeholder has a fixed size
        // (aspect-ratio) -- removing the image doesn't change the layout at
        // all, so there's nothing to compensate for.
        if (placeholder.style.aspectRatio) {
            img.remove();
            return;
        }

        // Fallback: same symmetric reasoning as always -- shrinking also
        // needs compensation (negative, here) for pages before the current one.
        const globalIndex = parseInt(placeholder.dataset.globalIndex, 10);
        const container = activeScrollContainer;
        const sizeBefore = placeholder.offsetHeight;
        img.remove();
        if (container && globalIndex < currentPage) {
            const sizeAfter = placeholder.offsetHeight;
            const delta = sizeAfter - sizeBefore; // negative -- it shrank
            if (delta !== 0) {
                container.scrollTop += delta;
            }
        }
    }

    // ---------- Mode switching ----------

    function setMode(newMode) {
        // Leaving an infinite scroll that has moved past the chapter this
        // page was opened with: the paginated modes now carry a current
        // chapter of their own (see becomeChapter), so they simply adopt
        // whichever one is in view, at the page the reader is actually
        // looking at. This used to be a full navigation to that chapter --
        // the only reload left in this reader once chapter handover stopped
        // being one.
        // Asked whenever scroll mode is being LEFT, not only when it holds
        // more than one chapter: a fit change rebuilds it from the chapter
        // in view, leaving one chapter that is not the opened one -- and
        // Single then showed chapter 1's page 6 under chapter 2's URL.
        if ((newMode === "single" || newMode === "double") && mode === "scroll" && chapters.length > 0) {
            const { chapter, localIndex } = resolveGlobalIndex(currentPage);
            if (String(chapter.itemId) !== String(itemId)) {
                becomeChapter(chapter, localIndex);
            } else {
                // Same chapter, but currentPage is a GLOBAL index in scroll
                // mode -- for the first mounted chapter startIndex is 0, so
                // it already equals the local index; being explicit keeps
                // that from silently depending on it.
                currentPage = localIndex;
            }
        }

        // Coming INTO scroll mode, it starts from where the paged modes are
        // (currentSetupStart): `chapters` is left over from an earlier
        // visit to scroll mode, and trusting it re-opened the chapter the
        // reader had already handed over from.
        if (newMode === "scroll" && mode !== "scroll") chapters = [];

        mode = newMode;
        readerPrefs.mode = newMode;
        savePrefs(readerPrefs);
        elSingle.classList.toggle("hidden", mode !== "single");
        elDouble.classList.toggle("hidden", mode !== "double");
        elDouble.classList.toggle("flex", mode === "double");
        elScroll.classList.toggle("hidden", mode !== "scroll");

        document.querySelectorAll(".mode-btn").forEach((btn) => {
            btn.classList.toggle("bg-indigo-600", btn.dataset.mode === mode);
            btn.classList.toggle("bg-neutral-800", btn.dataset.mode !== mode);
        });

        // Controls always start hidden, in ANY mode -- a click/tap on the
        // screen is what reveals them (see toggleControls()).
        controls.classList.add("opacity-0", "pointer-events-none");

        if (mode === "single") showSingle(currentPage, false);
        else if (mode === "double") showDouble(currentPage, false);
        else if (mode === "scroll") setupScrollMode();
    }

    // Returns the render promise so callers that need the page to actually
    // be on screen before continuing (advanceToNextChapter) can await it.
    function showPage(pageIndex) {
        if (mode === "single") return showSingle(pageIndex);
        if (mode === "double") return showDouble(pageIndex);
        // in scroll mode, navigation happens via the user's own scroll gesture
        return Promise.resolve();
    }

    // ---------- Double-page spread boundaries ----------
    // In double mode the step is NOT always 2: a landscape page (a spread
    // already merged in the source file) is displayed ALONE, filling both
    // slots by itself -- see showDouble. Stepping a fixed 2 past one of
    // those skipped the page right after it, making it permanently
    // unreachable: with pages [P,P,L,P,P,P] the reader showed 0, 1, 2, 4, 5
    // and page 3 was never displayed at all.
    //
    // A local "is the neighbouring page landscape?" rule is NOT enough to
    // fix this, which the first attempt here got wrong: every spread shifts
    // the odd/even alignment of every pair after it, so where a spread
    // STARTS depends on the whole walk from page 0, not on its immediate
    // neighbours. Backward navigation in particular can't be reconstructed
    // locally -- it would silently re-align onto a different set of pairs
    // than the forward walk produced.
    //
    // So the boundaries are computed once, by replaying exactly the walk
    // showDouble performs, and navigation is then just an index step into
    // that list. The page dimensions come from /reader/{id}/dimensions --
    // the same endpoint continuous scroll already uses, already cached
    // server-side in page_dimensions, so this costs no extra work.
    let spreadStartsPromise = null;
    function getSpreadStarts() {
        if (!spreadStartsPromise) {
            spreadStartsPromise = fetchChapterDimensions(itemId)
                .then((dims) => {
                    if (!dims || !dims.length) return null; // unknown -> caller falls back to a fixed step
                    const wide = (k) => {
                        const d = dims[k];
                        return !!(d && d.width > 0 && d.height > 0 && d.width > d.height);
                    };
                    const starts = [];
                    for (let i = 0; i < pageCount; ) {
                        starts.push(i);
                        // Alone when it is landscape -- and when the page
                        // after it is, since that one is never a partner
                        // (the same rule showDouble pairs by).
                        i += (wide(i) || wide(i + 1)) ? 1 : 2;
                    }
                    return starts;
                })
                .catch(() => null);
        }
        return spreadStartsPromise;
    }

    // Index of the spread currently on screen: the last boundary at or
    // before currentPage. Tolerates currentPage not being a boundary at all
    // (e.g. switching into double mode from single mode mid-chapter) --
    // the next navigation then re-aligns onto the canonical boundaries.
    function spreadIndexFor(starts, page) {
        for (let i = starts.length - 1; i >= 0; i--) {
            if (starts[i] <= page) return i;
        }
        return 0;
    }

    async function nextPage() {
        if (mode === "double") {
            const starts = await getSpreadStarts();
            if (starts) {
                const idx = spreadIndexFor(starts, currentPage);
                if (idx + 1 < starts.length) {
                    showPage(starts[idx + 1]);
                } else {
                    // last spread -- time for the next chapter (if any)
                    advanceToNextChapter();
                }
                return;
            }
        }
        const step = mode === "double" ? 2 : 1;
        const target = Math.min(pageCount - 1, currentPage + step);
        if (target === currentPage) {
            // already at the last displayable page/pair -- don't advance
            // further within the item, it's time for the next chapter (if any)
            advanceToNextChapter();
            return;
        }
        showPage(target);
    }

    async function prevPage() {
        if (mode === "double") {
            const starts = await getSpreadStarts();
            if (starts) {
                const idx = spreadIndexFor(starts, currentPage);
                showPage(starts[Math.max(0, idx - 1)]);
                return;
            }
        }
        const step = mode === "double" ? 2 : 1;
        showPage(Math.max(0, currentPage - step));
    }

    // ---------- Progress ----------
    // Saves on every page turn, no debounce -- for a single-user self-hosted
    // app, the cost of one extra request per page is negligible, and this
    // permanently eliminates the "up to 5s without saving" window (and the
    // complexity of reasoning about the timer interacting with fast
    // navigation) that existed before. (A scheduleProgressSave() wrapper
    // used to sit here holding that timer; once the debounce went away it
    // was a bare passthrough, so callers just call saveProgress directly.)

    // asFinished: the reader left this chapter by reading PAST its end, so
    // it counts as finished whatever page the display happens to be on.
    // That matters in double mode, where the final spread usually starts
    // before the last page (pages [P,P,P,P] pair as 0-1 and 2-3, so the
    // last spread starts at 2 of 4) -- the "did we reach the end" test
    // below would score that 75% and "reading", and the chapter would
    // never be marked read no matter how far the reader got.
    // Which chapter and which page of it is on screen right now.
    //
    // In scroll mode, currentPage is a GLOBAL index across every mounted
    // chapter, so "where am I" is not simply (itemId, currentPage) --
    // progress belongs to whichever chapter is actually in view, not
    // always the item the page was opened with. Single/double mode never
    // touch `chapters` and fall straight through unchanged.
    //
    // Shared by saveProgress and by the reading-time tracker below, which
    // need the exact same answer: one asks "how far have I got", the other
    // "which page am I timing".
    function visiblePosition() {
        if (mode === "scroll" && chapters.length > 0) {
            const resolved = resolveGlobalIndex(currentPage);
            return {
                itemId: resolved.chapter.itemId,
                page: resolved.localIndex,
                pageCount: resolved.chapter.pageCount,
            };
        }
        return { itemId, page: currentPage, pageCount };
    }

    function saveProgress(useBeacon = false, asFinished = false) {
        if (root.dataset.resumePending === "true") return;
        const at = visiblePosition();
        // Finished stays finished for the rest of the session, as scroll
        // mode's markChapterFinished already did: the last volume read to
        // its end posted "completed", and leaving the page a moment later
        // posted the final spread's first page over it -- 90%, "reading".
        if (asFinished) finishedChapters.add(at.itemId);
        const targetPage = asFinished && at.pageCount ? at.pageCount - 1 : at.page;
        postProgress(at.itemId, targetPage, at.pageCount, useBeacon);
    }

    // Reported on every change of what is DISPLAYED, which is not the same
    // set of moments as saveProgress: switching display mode or reopening
    // the book puts a page on screen without that counting as advancing
    // through it (see the comment on showSingle's recordProgress), but the
    // page is on screen either way and the clock should be running on it.
    function trackVisiblePage() {
        const at = visiblePosition();
        readingTime.at({ item_id: Number(at.itemId), page_index: at.page,
            source_version:versions.get(String(at.itemId)) });
        rememberPosition();
    }

    // Keeps #reader-root saying which chapter and page are on screen. It is
    // what the reader initialises from, and htmx's history cache restores
    // a copy of it -- see becomeChapter. In scroll mode the chapter is the
    // one in view, not the one this page was opened with.
    function rememberPosition() {
        let at;
        if (mode === "scroll" && chapters.length > 0) {
            const { chapter, localIndex } = resolveGlobalIndex(currentPage);
            at = { itemId: String(chapter.itemId), page: localIndex,
                   pageCount: chapter.pageCount, nextItemId: chapter.nextItemId };
        } else {
            at = { itemId: String(itemId), page: currentPage, pageCount, nextItemId };
        }
        root.dataset.itemId = at.itemId;
        root.dataset.pageCount = String(at.pageCount);
        root.dataset.nextItemId = at.nextItemId ? String(at.nextItemId) : "";
        if (versions.has(at.itemId)) root.dataset.version = versions.get(at.itemId);
        root.dataset.startPage = String(at.page);
        root.dataset.startPercent = String(
            at.pageCount ? Math.round(((at.page + 1) / at.pageCount) * 100) : 0);
    }

    // The actual request. Separate from saveProgress because scroll mode
    // also needs to report a SPECIFIC chapter (one the reader has scrolled
    // past, which is by definition no longer the one `currentPage` points
    // at) -- see markChapterFinished.
    function postProgress(targetItemId, targetPage, targetPageCount, useBeacon = false) {
        // Having reached the end wins over whichever page the display is
        // showing now -- the same rule the paginated modes state as
        // asFinished. It matters on a short final page, where scrolling
        // back up by a pixel puts the top of the screen in the page before
        // it: without this, one such move (or the pagehide beacon) posts
        // `reading` over a volume that was read to the end. `finished` is
        // per session and in memory, so reopening the book and starting
        // again lowers it normally.
        const finished = finishedChapters.has(targetItemId);
        const reachedEnd = finished || targetPage >= targetPageCount - 1;
        const progressPercent = reachedEnd
            ? 100
            : (targetPageCount ? Math.round(((targetPage + 1) / targetPageCount) * 100) : 0);
        const payload = JSON.stringify({
            current_page: targetPage,
            progress_percent: progressPercent,
            status: reachedEnd ? "completed" : "reading",
        });
        // The baseline a restored page measures the server against -- see
        // reader-resume.js. Only for the item this PAGE is: scroll mode
        // posts a chapter that has been scrolled past under its own id,
        // and how far through THAT one is says nothing about where this
        // page stands.
        if (String(targetItemId) === String(itemId)) {
            window.readerResume?.sent(progressPercent);
        }
        const url = `/reader/${targetItemId}/progress`;
        if (useBeacon && navigator.sendBeacon) {
            navigator.sendBeacon(url, new Blob([payload], { type: "application/json" }));
        } else {
            fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: payload, keepalive: true });
        }
    }

    // ---------- Input ----------

    // Shared by every mode: toggles the controls bar.
    function toggleControls() {
        const hidden = controls.classList.toggle("opacity-0");
        controls.classList.toggle("pointer-events-none", hidden);
    }

    // Left third = previous page, right third = next, middle third =
    // toggles the controls -- in single/double page. Detected directly on
    // a click on the container ITSELF (elSingle/elDouble), not via
    // overlapping zone divs: overlay zones are SIBLINGS of the container
    // (not descendants of it), so a zone covering the whole screen blocks
    // mouse-wheel scrolling and native scrollbar dragging inside it -- the
    // browser only finds a scrollable ancestor starting from the event's
    // actual target, and the zone doesn't have one. This didn't matter
    // before (the image always fit entirely on screen), but started to
    // matter with the size-fit options that can require scrolling within
    // the page. By using the container itself as the click target, it
    // remains the same scrollable element -- nothing blocks anything.
    function handleTapClick(e, containerEl) {
        readerFullscreen.armFromGesture();
        const rect = containerEl.getBoundingClientRect();
        const relativeX = (e.clientX - rect.left) / rect.width;
        if (relativeX < 1 / 3) {
            rtl ? nextPage() : prevPage();
        } else if (relativeX > 2 / 3) {
            rtl ? prevPage() : nextPage();
        } else {
            toggleControls();
        }
    }
    elSingle.addEventListener("click", (e) => handleTapClick(e, elSingle));
    elDouble.addEventListener("click", (e) => handleTapClick(e, elDouble));

    // In scroll mode page navigation happens via the scroll gesture itself,
    // so any click on the container just toggles the controls. A real tap
    // (without dragging) still fires "click" normally; a scroll gesture
    // doesn't -- the browser already tells the two apart on its own.
    elScroll.addEventListener("click", () => {
        readerFullscreen.armFromGesture();
        toggleControls();
    });

    document.querySelectorAll(".mode-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            setMode(btn.dataset.mode);
            // setMode hides the toolbar. Do not leave keyboard focus on
            // its now-invisible button instead of the reading surface.
            if (mode === "scroll") elScroll.focus({ preventScroll: true });
        });
    });

    document.querySelectorAll(".fit-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            readerPrefs.fit = btn.dataset.fit;
            savePrefs(readerPrefs);
            applyFit();
            // vertical scroll computes each placeholder's size at mount
            // time (see verticalFitWidthCSS) -- it needs to be rebuilt to
            // reflect the new fit; since alignment via scrollIntoView uses
            // the current page, the reading position isn't lost, only the
            // page sizes change.
            if (mode === "scroll") setupScrollMode();
        });
    });

    const btnRtl = document.getElementById("btn-rtl");
    btnRtl.addEventListener("click", (e) => {
        rtl = !rtl;
        readerPrefs.rtl = rtl;
        savePrefs(readerPrefs);
        e.target.textContent = rtl ? "Manga (R→L)" : "Western";
        if (mode === "double") showDouble(currentPage, false);
    });

    bindOutside(document, "keydown", (e) => {
        readerFullscreen.armFromGesture();
        // A key meant for a control is not a page turn.
        if (e.target instanceof Element
                && e.target.closest("input, select, textarea, button, [contenteditable]")) return;
        if (mode === "scroll") {
            // The body cannot scroll, and native arrows may target it
            // before the comic has focus (notably in Firefox). Move only
            // the scroll surface; never route to nextPage/prevPage: their
            // page-turn semantics can finish a chapter prematurely here.
            if (e.defaultPrevented || e.isComposing || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                elScroll.scrollBy({ top: e.key === "ArrowDown" ? 40 : -40, behavior: "instant" });
            }
            return;
        }
        if (e.key === "ArrowRight" || e.key === " ") (rtl ? prevPage() : nextPage());
        if (e.key === "ArrowLeft" || e.key === "Backspace") (rtl ? nextPage() : prevPage());
    });

    let touchStartX = null;
    bindOutside(document, "touchstart", (e) => { touchStartX = e.touches[0].clientX; });
    bindOutside(document, "touchend", (e) => {
        if (touchStartX === null || mode === "scroll") return;
        const deltaX = e.changedTouches[0].clientX - touchStartX;
        if (Math.abs(deltaX) > 50) {
            const forward = deltaX < 0;
            (forward !== rtl) ? nextPage() : prevPage();
        }
        touchStartX = null;
    });

    // visibilitychange and pagehide usually both fire for the same
    // navigation (that's what the logs showed: two identical saves, from
    // different ports/connections, in the same millisecond). saveOnExit()
    // dedupes this -- repeated calls within a short window collapse into one.
    let lastExitSaveAt = 0;
    function saveOnExit() {
        // Re-measures synchronously right at the moment of leaving -- never
        // trusts the last value the (async) scroll listener may not have
        // processed yet. measureCurrentScrollPageSafe() avoids capturing a
        // transient state (previous pages still mid-mount) as if it were
        // the final position.
        if (mode === "scroll") currentPage = measureCurrentScrollPageSafe();
        const now = performance.now();
        if (now - lastExitSaveAt < 500) return;
        lastExitSaveAt = now;
        saveProgress(true);
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
        // The document outlives the reader under hx-boost, and so did every
        // page image it held (see the page store above).
        nextChapterPrefetch = null;
        releaseAllPages();
    }
    bindOutside(document, "htmx:before:history:update", leaveOnBoost);
    bindOutside(document, "htmx:history:cache:hit", leaveOnBoost);

    readerFullscreen.setupButton(document.getElementById("btn-fullscreen"));

    if (rtl) btnRtl.textContent = "Manga (R→L)";
    applyFit(); // ensures the visual state of the fit buttons even if the initial mode is scroll
    setMode(mode); // restores the saved mode (or "single" on first visit)
})();
