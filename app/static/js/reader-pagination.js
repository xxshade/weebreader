/** Text positions survive reflow; page boundaries do not. Offsets count UTF-16
 * code units in a block's DOM text (the same convention as DOM Range). */
(() => {
    if (window.readerPagination) return;
    const position = (value) => typeof value === 'number' ? {index: value, offset: 0} : {index: value?.index || 0, offset: value?.offset || 0};
    const compare = (a, b) => a.index - b.index || a.offset - b.offset;
    const length = (unit) => unit?.nodes.reduce((n, node) => n + (node.nodeType === Node.COMMENT_NODE ? 0 : node.textContent.length), 0) || 0;
    const parse = (raw) => {
        const match = /^elidx:(\d+)(?::(\d+))?$/.exec(raw || '');
        return match ? {index: Number(match[1]), offset: Number(match[2] || 0)} : position(0);
    };
    function textMap(nodes) {
        const entries = [];
        let size = 0;
        for (const root of nodes) {
            const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let node = root.nodeType === Node.TEXT_NODE ? root : walker.nextNode();
            while (node) {
                entries.push({node, start: size, end: size + node.length});
                size += node.length;
                node = walker.nextNode();
            }
        }
        function point(offset) {
            const entry = entries.find(e => e.end > offset) || entries[entries.length - 1];
            return entry ? [entry.node, Math.max(0, Math.min(entry.node.length, offset - entry.start))] : null;
        }
        function range(from, to) {
            const a = point(from), b = point(to);
            if (!a || !b) return null;
            const out = a[0].ownerDocument.createRange();
            out.setStart(...a); out.setEnd(...b);
            return out;
        }
        return {size, range};
    }
    function splittable(unit) {
        return unit.nodes.every(node => node.nodeType !== Node.ELEMENT_NODE ||
            (!node.matches('h1,h2,h3,h4,h5,h6,pre,table,figure,hr,details,img,svg,math,video,audio,iframe,canvas,object') &&
             !node.querySelector('img,svg,math,table,pre,video,audio,iframe,canvas,object,input,button')));
    }
    function fragment(unit, from, to) {
        const size = length(unit);
        if (from === 0 && to === size) return unit;
        const doc = unit.nodes[0].ownerDocument;
        const holder = doc.createElement('div');
        holder.append(...unit.nodes.map(node => node.cloneNode(true)));
        const map = textMap([...holder.childNodes]);
        // Delete in reverse order: the first deletion must not move the start.
        const after = map.range(to, size);
        if (after) after.deleteContents();
        const before = textMap([...holder.childNodes]).range(0, from);
        if (before) before.deleteContents();
        const nodes = [...holder.childNodes];
        for (const node of nodes) if (node.nodeType === Node.ELEMENT_NODE) {
            if (from) {
                node.style.setProperty('text-indent', '0', 'important');
                node.style.setProperty('margin-top', '0', 'important');
                if (node.localName === 'li') node.style.setProperty('list-style-type', 'none', 'important');
            }
            if (to < size) node.style.setProperty('margin-bottom', '0', 'important');
        }
        return {nodes, chain: unit.chain};
    }
    // Locate line starts using ranges in the FULL paragraph, before truncating
    // it. Binary searches avoid measuring every character of a long paragraph.
    function lines(nodes) {
        const map = textMap(nodes);
        const result = [];
        const rect = offset => map.range(offset, Math.min(map.size, offset + 1))?.getBoundingClientRect();
        let start = 0;
        while (start < map.size) {
            const first = rect(start);
            if (!first || !first.height) { start++; continue; }
            let lo = start + 1, hi = map.size;
            while (lo < hi) {
                const mid = Math.floor((lo + hi) / 2), box = rect(mid);
                if (box && box.top < first.bottom - 1) lo = mid + 1; else hi = mid;
            }
            result.push({start, end: lo, top: first.top, bottom: first.bottom});
            start = lo;
        }
        return result;
    }
    function offsetAt(unit, y) {
        const map = textMap(unit.nodes);
        let lo = 0, hi = map.size;
        while (lo < hi) {
            const mid = Math.floor((lo + hi) / 2);
            const rect = map.range(mid, mid + 1)?.getBoundingClientRect();
            if (rect && rect.bottom <= y + 1) lo = mid + 1; else hi = mid;
        }
        return Math.min(lo, Math.max(0, map.size - 1));
    }
    function topAt(unit, offset) {
        const map = textMap(unit.nodes);
        return map.range(Math.min(offset, Math.max(0, map.size - 1)), Math.min(map.size, offset + 1))?.getBoundingClientRect().top ?? unit.getBoundingClientRect().top;
    }
    function styles(nodes, unit, previous, next) {
        const el = nodes.find(node => node.nodeType === Node.ELEMENT_NODE) || nodes[0]?.parentElement;
        const read = node => node.ownerDocument.defaultView.getComputedStyle(node);
        const own = el ? read(el) : null;
        const result = {breakBefore: own?.breakBefore, breakAfter: own?.breakAfter,
            breakInside: own?.breakInside, avoidContainers: []};
        let parent = nodes[0]?.parentElement;
        for (let i = unit.chain.length - 1; i >= 0 && parent; i--, parent = parent.parentElement) {
            const css = read(parent), original = unit.chain[i];
            if (!previous?.chain.includes(original) && forced(css.breakBefore)) result.breakBefore = css.breakBefore;
            if (!next?.chain.includes(original) && forced(css.breakAfter)) result.breakAfter = css.breakAfter;
            if (css.breakInside.includes('avoid')) result.avoidContainers.push(original);
        }
        return result;
    }
    const forced = value => ['page', 'always', 'left', 'right', 'recto', 'verso'].includes(value);

    async function fill(body, blocks, edge, direction, height, settle) {
        const backward = direction === 'backward';
        const page = window.readerBlocks.builder(body);
        const limit = {index: blocks.length, offset: 0};
        let cursor = position(edge);
        cursor.index = Math.max(0, Math.min(cursor.index, backward ? blocks.length : Math.max(0, blocks.length - 1)));
        cursor.offset = Math.max(0, Math.min(cursor.offset, length(blocks[cursor.index])));
        const anchor = {...cursor};
        const placed = [];
        function keepHeadingTail() {
            if (backward) return;
            let first = placed.length;
            while (first > 0 && (placed[first - 1].heading || placed[first - 1].css.breakAfter?.includes('avoid'))) first--;
            // At least one preceding block must stay, or a huge heading chain
            // could produce the same empty page forever.
            if (first > 0 && first < placed.length) {
                for (let n = placed.length - 1; n >= first; n--) page.undo();
                cursor = placed[first].before;
            }
        }
        // Chromium can retain the scrollbar from an overflowing trial. Its
        // narrower text then wraps differently from the very same fitting
        // fragment tried before it. Reserve the gutter throughout measurement
        // and display, including the scrollable fallback for atomic content.
        body.ownerDocument.documentElement.style.scrollbarGutter = 'stable';
        const fits = () => body.scrollHeight <= height + 1;
        const put = unit => backward ? page.prepend(unit) : page.append(unit);
        while (backward ? compare(cursor, position(0)) > 0 : compare(cursor, limit) < 0) {
            const i = backward ? cursor.index - (cursor.offset ? 0 : 1) : cursor.index;
            const unit = blocks[i], size = length(unit);
            const from = backward ? 0 : cursor.offset;
            const to = backward && cursor.offset ? cursor.offset : size;
            const before = {...cursor};
            const nodes = put(fragment(unit, from, to));
            await settle();
            const css = styles(nodes, unit, blocks[i - 1], blocks[i + 1]);
            const prior = placed[placed.length - 1];
            if (prior && (backward ? forced(css?.breakAfter) || forced(prior.css?.breakBefore) : forced(css?.breakBefore) || forced(prior.css?.breakAfter))) {
                page.undo(); break;
            }
            if (fits()) {
                cursor = backward ? {index: i, offset: 0} : {index: i + 1, offset: 0};
                placed.push({before, css, unit, nodes, index: i, to, heading: nodes.some(node => /^h[1-6]$/.test(node.localName || ''))});
                continue;
            }
            // Try an author's keep-together container on a fresh page first.
            // If it is larger than a page it starts at slot zero there, and the
            // ordinary fragment rule below makes progress instead of looping.
            const keptGroup = css.avoidContainers.map(container => placed.findIndex(p => p.unit.chain.includes(container))).find(index => index > 0);
            if (keptGroup !== undefined) {
                page.undo();
                for (let n = placed.length - 1; n >= keptGroup; n--) page.undo();
                cursor = placed[keptGroup].before;
                break;
            }
            let cut = null;
            if (splittable(unit) && to > from) {
                const measured = lines(nodes);
                const keep = css?.breakInside?.includes('avoid');
                const fullHeight = measured.length ? measured.at(-1).bottom - measured[0].top : 0;
                if (!keep || !prior || fullHeight > height) {
                    // Leave at least two lines on both sides when feasible.
                    // Tiny screens still advance by one complete line.
                    const candidates = measured.slice(1).map(line => line.start);
                    page.undo();
                    let lo = 0, hi = candidates.length - 1;
                    while (lo <= hi) {
                        const mid = Math.floor((lo + hi) / 2), offset = from + candidates[mid];
                        put(fragment(unit, backward ? offset : from, backward ? to : offset));
                        const ok = fits(); page.undo();
                        if (ok) { cut = mid; if (backward) hi = mid - 1; else lo = mid + 1; }
                        else if (backward) lo = mid + 1; else hi = mid - 1;
                    }
                    if (cut !== null) {
                        let firstLines = cut + 1, lastLines = measured.length - firstLines;
                        if (!backward && lastLines === 1 && firstLines > 2) cut--;
                        if (backward && firstLines === 1 && lastLines > 2) cut++;
                        firstLines = cut + 1; lastLines = measured.length - firstLines;
                        if (prior && (backward ? lastLines : firstLines) < 2) cut = null;
                    }
                    if (cut !== null) {
                        const offset = from + candidates[cut];
                        put(fragment(unit, backward ? offset : from, backward ? to : offset));
                        cursor = {index: i, offset};
                        break;
                    }
                    // No complete line fits after the preceding blocks.
                    if (prior) {
                        keepHeadingTail();
                        break;
                    }
                    // A line taller than the viewport is still reachable by scroll.
                    const offset = from + (backward ? candidates.at(-1) : candidates[0]);
                    if (Number.isFinite(offset)) {
                        put(fragment(unit, backward ? offset : from, backward ? to : offset));
                        cursor = {index: i, offset}; break;
                    }
                    put(fragment(unit, from, to));
                } else page.undo();
            } else if (prior) page.undo();
            if (prior) {
                // A heading should travel with at least the beginning of its text.
                keepHeadingTail();
                if (backward && nodes.some(node => /^h[1-6]$/.test(node.localName || '')) && splittable(prior.unit)) {
                    // Leave the paragraph's opening lines with a heading that
                    // cannot fit here. The preceding page will end at this cut.
                    const opening = lines(prior.nodes);
                    if (opening.length >= 4) {
                        const offset = opening[2].start;
                        page.undo();
                        page.prepend(fragment(prior.unit, offset, prior.to));
                        cursor = {index: prior.index, offset};
                    }
                }
                break;
            }
            cursor = backward ? {index: i, offset: 0} : {index: i + 1, offset: 0};
            break;
        }
        return backward ? {start: cursor, end: anchor} : {start: anchor, end: cursor};
    }
    window.readerPagination = {position, compare, length, parse, fill, offsetAt, topAt};
})();
