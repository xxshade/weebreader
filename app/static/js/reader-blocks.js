/**
 * What the text reader counts as ONE BLOCK of a chapter, and how a run of
 * blocks is put back together as a page.
 *
 * A block is two things at once. It is the unit of POSITION -- `elidx:N`
 * is the Nth block, the same number in both modes and on every screen --
 * and it is what the PAGINATOR fits onto a page. The rule used to be a
 * flat `querySelectorAll("p, h1..h6, li, blockquote, pre, td, figure, dd,
 * dt")`, each match carried onto the page as its own outerHTML. Measured
 * with that extraction, the same in Chromium and Firefox, a paginated
 * page:
 *
 *   - lost everything outside those tags: a <th>, a <caption>, an <hr>,
 *     text straight inside a <div> (a "* * *" break), an image in a <div>
 *     mid-chapter, text loose between paragraphs, and the book's own CSS;
 *   - took each <td> out of its table, where the parser drops the tag: a
 *     character sheet became one line of run-together text;
 *   - showed a <p> inside a <blockquote>, <li> or <td> TWICE -- once as
 *     part of its container, once on its own.
 *
 * Here the chapter is walked from <body> down instead. A paragraph,
 * heading, <pre>, table, figure or <hr> is one block whatever is inside
 * it. Any other element that holds block-level markup is descended into;
 * one that holds only text and inline markup is a block if it holds
 * anything to see. Text and inline markup lying loose between blocks is a
 * block of its own. Every node lands in at most one block.
 *
 * A block keeps the chain of elements it was found inside, and a page
 * copies that chain around it, merged with its neighbour's where they
 * share one: three paragraphs of a quotation are one <blockquote> on the
 * page, a status box is still one box, a list item is still in its list.
 * A container split across two pages is drawn on both.
 *
 * Loaded from <head> for the reason reader-resume.js is: the engine that
 * calls it is page content, and a swap does not keep script order.
 */
(() => {
    if (window.readerBlocks) return;

    // One block whatever is inside it, counted even when empty -- so a
    // chapter of plain paragraphs is numbered exactly as it always was.
    const WHOLE = new Set(["p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "figure", "hr", "details"]);
    // Blocks under the old rule too, and still counted when empty -- but
    // only when they hold no block-level markup. Otherwise what they hold
    // is the blocks: the <p> in an <li>, not the <li> and then the <p>.
    const COUNTED = new Set(["li", "blockquote", "dd", "dt"]);
    // What makes an element something to descend into rather than inline
    // content inside a block.
    const BLOCK_LEVEL = new Set([...WHOLE, ...COUNTED,
        "address", "article", "aside", "caption", "center", "dialog", "div", "dl", "fieldset",
        "figcaption", "footer", "form", "header", "hgroup", "legend", "main", "menu", "nav",
        "ol", "section", "summary", "ul"]);
    // Something to see that is not text. Never descended into: a <div>
    // inside an SVG's <foreignObject> does not make the SVG a container.
    const MEDIA = new Set(["img", "svg", "video", "audio", "picture", "canvas", "object", "embed", "iframe", "math"]);
    // Neither content nor a block. A <style> in the body styles the whole
    // chapter (see pageShell). A <noscript> is markup to DOMParser, which
    // runs no scripts, and raw text in the iframe, which does -- counting
    // what is inside one would number the two modes differently.
    const SKIPPED = new Set(["script", "style", "link", "meta", "title", "template", "base", "noscript"]);

    const ELEMENT = 1;
    const TEXT = 3;

    function hasContent(node) {
        if (node.nodeType === TEXT) return /\S/.test(node.data);
        if (node.nodeType !== ELEMENT) return false;
        const name = node.localName;
        if (SKIPPED.has(name)) return false;
        if (MEDIA.has(name)) return true;
        for (const child of node.childNodes) {
            if (hasContent(child)) return true;
        }
        return false;
    }

    function holdsBlocks(element) {
        for (const child of element.children) {
            const name = child.localName;
            if (SKIPPED.has(name) || MEDIA.has(name)) continue;
            if (BLOCK_LEVEL.has(name) || holdsBlocks(child)) return true;
        }
        return false;
    }

    function block(nodes, chain) {
        return {
            nodes,
            chain,
            // What continuous scroll reads a position from. Loose text has
            // no element of its own, so it is measured as a range.
            getBoundingClientRect() {
                if (nodes.length === 1 && nodes[0].nodeType === ELEMENT) return nodes[0].getBoundingClientRect();
                const range = nodes[0].ownerDocument.createRange();
                range.setStartBefore(nodes[0]);
                range.setEndAfter(nodes[nodes.length - 1]);
                return range.getBoundingClientRect();
            },
        };
    }

    function walk(container, chain, blocks) {
        let run = [];
        const flush = () => {
            // Whitespace and comments at either end draw nothing; they
            // would only widen the range the block is measured by.
            const quiet = (node) => node.nodeType !== ELEMENT && !hasContent(node);
            let first = 0;
            let last = run.length - 1;
            while (first <= last && quiet(run[first])) first++;
            while (last >= first && quiet(run[last])) last--;
            const nodes = run.slice(first, last + 1);
            if (nodes.some(hasContent)) blocks.push(block(nodes, chain));
            run = [];
        };
        for (const node of container.childNodes) {
            if (node.nodeType !== ELEMENT) {
                run.push(node);
                continue;
            }
            const name = node.localName;
            if (SKIPPED.has(name)) continue;
            if (WHOLE.has(name)) {
                flush();
                blocks.push(block([node], chain));
            } else if (MEDIA.has(name)) {
                run.push(node);
            } else if (holdsBlocks(node)) {
                flush();
                walk(node, chain.concat(node), blocks);
            } else if (BLOCK_LEVEL.has(name) && (COUNTED.has(name) || hasContent(node))) {
                flush();
                blocks.push(block([node], chain));
            } else {
                // Inline markup -- or an empty block, kept in the run for
                // the line break it makes between the text either side.
                run.push(node);
            }
        }
        flush();
    }

    function collect(body) {
        const blocks = [];
        if (!body) return blocks;
        walk(body, [], blocks);
        // Nothing counted but something there -- a cover drawn as a CSS
        // background on an empty <div> -- is still shown, whole.
        if (!blocks.length) {
            const nodes = Array.from(body.childNodes).filter((node) => (node.nodeType === ELEMENT
                ? !SKIPPED.has(node.localName)
                : node.nodeType === TEXT && /\S/.test(node.data)));
            if (nodes.length) blocks.push(block(nodes, []));
        }
        return blocks;
    }

    // An <li> of an <ol> is given its number, or a list continued on the
    // next page would start again at 1 there.
    function ordinal(item) {
        const list = item.parentElement;
        if (!list || list.localName !== "ol") return null;
        const items = Array.from(list.children).filter((child) => child.localName === "li");
        const reversed = list.hasAttribute("reversed");
        const start = parseInt(list.getAttribute("start"), 10);
        let n = Number.isNaN(start) ? (reversed ? items.length : 1) : start;
        for (const other of items) {
            const own = parseInt(other.getAttribute("value"), 10);
            if (!Number.isNaN(own)) n = own;
            if (other === item) return n;
            n += reversed ? -1 : 1;
        }
        return null;
    }

    function copy(doc, node, deep) {
        const out = doc.importNode(node, deep);
        if (node.nodeType === ELEMENT && node.localName === "li" && !node.hasAttribute("value")) {
            const n = ordinal(node);
            if (n !== null) out.setAttribute("value", String(n));
        }
        return out;
    }

    // Places blocks into `body` one at a time, at either end, merging each
    // one's containers with those already open at that end. `undo` takes
    // off the block placed LAST, which is what lets a page built from the
    // edge it is anchored to be trimmed at the other one.
    function builder(body) {
        const doc = body.ownerDocument;
        // The copied containers open at each edge, outermost first.
        let front = [];
        let back = [];
        const placed = [];

        function place(unit, atEnd) {
            const open = atEnd ? back : front;
            let depth = 0;
            while (depth < open.length && depth < unit.chain.length && open[depth].original === unit.chain[depth]) depth++;
            const chain = open.slice(0, depth);
            let parent = depth ? chain[depth - 1].copy : body;
            let created = null;
            for (let i = depth; i < unit.chain.length; i++) {
                const shell = copy(doc, unit.chain[i], false);
                if (atEnd) parent.append(shell);
                else parent.prepend(shell);
                created = created || shell;
                chain.push({ original: unit.chain[i], copy: shell });
                parent = shell;
            }
            const nodes = unit.nodes.map((node) => copy(doc, node, true));
            if (atEnd) parent.append(...nodes);
            else parent.prepend(...nodes);
            placed.push({ created, nodes, front, back });
            if (placed.length === 1) {
                front = chain;
                back = chain;
            } else if (atEnd) {
                back = chain;
            } else {
                front = chain;
            }
            return nodes;
        }

        return {
            append: (unit) => place(unit, true),
            prepend: (unit) => place(unit, false),
            undo() {
                const last = placed.pop();
                if (!last) return;
                if (last.created) last.created.remove();
                else for (const node of last.nodes) node.remove();
                front = last.front;
                back = last.back;
            },
            get count() { return placed.length; },
        };
    }

    function attributes(element) {
        if (!element) return "";
        return Array.from(element.attributes)
            .filter((attr) => !/^on/i.test(attr.name))
            .map((attr) => ` ${attr.name}="${attr.value.replace(/&/g, "&amp;").replace(/"/g, "&quot;")}"`)
            .join("");
    }

    // The empty page blocks are placed into: the chapter's own <html> and
    // <body> attributes and its own stylesheets, which is what the book's
    // CSS is written against. Stylesheets only, wherever the chapter put
    // them -- a script would run again on every page turn.
    function pageShell(doc) {
        const styles = Array.from(doc.querySelectorAll("style, link[rel~='stylesheet' i]"), (el) => el.outerHTML).join("");
        return `<html${attributes(doc.documentElement)}><head>${styles}</head><body${attributes(doc.body)}></body></html>`;
    }

    window.readerBlocks = { collect, builder, pageShell };
})();
