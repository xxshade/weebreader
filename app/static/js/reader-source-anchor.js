/** Validated positions across text and structural HTML edits.
 * Raw-HTML output remains authoritative. Unsupported transformations fail closed.
 * Offsets use UTF-16. Inside replacement text, the original start is the anchor.
 */
(() => {
    function edit(text, rules, options = {}) {
        const value = window.readerReplacements.trace(text, rules, options);
        return {text: value.text, ...stageMapper(value.stages)};
    }
    function stageMapper(stages) {
        function translate(point, backwards) {
            const ordered = backwards ? [...stages].reverse() : stages;
            for (const changes of ordered) {
                let shift = 0;
                for (const e of changes) {
                    const a = backwards ? e.c : e.a,
                        b = backwards ? e.d : e.b;
                    const c = backwards ? e.a : e.c,
                        d = backwards ? e.b : e.d;
                    if (point < a) break;
                    if (point < b) {
                        point = c;
                        shift = 0;
                        break;
                    }
                    shift = d - b;
                }
                point += shift;
            }
            return point;
        }
        return {
            toSource: (p) => translate(p, true),
            toDisplay: (p) => translate(p, false),
        };
    }
    function leaves(unit) {
        const result = [];
        function walk(node) {
            if (node.nodeType === 3) result.push(node.data);
            else if (node.nodeType === 1)
                for (const child of node.childNodes) walk(child);
        }
        unit.nodes.forEach(walk);
        return result;
    }
    function shape(unit) {
        function walk(n) {
            if (n.nodeType === 3) return "#";
            if (n.nodeType === 1)
                return [n.localName, [...n.childNodes].map(walk)];
            return n.nodeType;
        }
        return JSON.stringify(unit.nodes.map(walk));
    }
    function build(original, display, rules) {
        if (original.length !== display.length) return null;
        const units = [];
        for (let i = 0; i < original.length; i++) {
            if (shape(original[i]) !== shape(display[i])) return null;
            const a = leaves(original[i]),
                b = leaves(display[i]);
            if (a.length !== b.length) return null;
            const rows = [];
            let source = 0,
                rendered = 0;
            for (let j = 0; j < a.length; j++) {
                const map = edit(a[j], rules);
                // In particular do not reinterpret raw entity spelling as DOM text.
                if (map.text !== b[j]) return null;
                rows.push({
                    source,
                    rendered,
                    sourceLength: a[j].length,
                    displayLength: b[j].length,
                    map: { toSource: map.toSource, toDisplay: map.toDisplay },
                });
                source += a[j].length;
                rendered += b[j].length;
            }
            units.push(rows);
        }
        return unitMapper(units);
    }
    function unitMapper(units) {
        function convert(position, backwards) {
            const rows = units[position.index];
            if (!rows) return null;
            let offset = position.offset || 0;
            for (let j = 0; j < rows.length; j++) {
                const row = rows[j],
                    start = backwards ? row.rendered : row.source;
                const length = backwards ? row.displayLength : row.sourceLength;
                if (offset < start + length || j === rows.length - 1) {
                    const local = Math.min(length, Math.max(0, offset - start));
                    return {
                        index: position.index,
                        offset:
                            (backwards ? row.source : row.rendered) +
                            (backwards
                                ? row.map.toSource(local)
                                : row.map.toDisplay(local)),
                    };
                }
            }
            return { index: position.index, offset: 0 };
        }
        return {
            toSource: (p) => convert(p, true),
            toDisplay: (p) => convert(p, false),
        };
    }
    async function digest(text) {
        const bytes = await crypto.subtle.digest(
            "SHA-256",
            new TextEncoder().encode(text),
        );
        return [...new Uint8Array(bytes)]
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("");
    }
    // Structural mapping keeps provenance in raw HTML and validates the parsed tree.
    // Marker text preserves whitespace/non-whitespace tokenization; no wrappers are added.
    function rawTransform(html, rules) {
        const spans = [];
        let delta = 0;
        const output = html.replace(
            /<(script|style)\b[\s\S]*?<\/\1\s*>|>([^<]+)</gi,
            (match, tag, text, at) => {
                if (text === undefined) return match;
                const value = edit(text, rules, {raw: true}),
                    a = at + 1,
                    c = a + delta;
                spans.push({
                    a,
                    b: a + text.length,
                    c,
                    d: c + value.text.length,
                    map: {
                        toSource: value.toSource,
                        toDisplay: value.toDisplay,
                    },
                });
                delta += value.text.length - text.length;
                return ">" + value.text + "<";
            },
        );
        return { text: output, ...spanMapper(spans) };
    }
    function spanMapper(spans) {
        function point(p, back) {
            let shift = 0;
            for (const s of spans) {
                const a = back ? s.c : s.a,
                    b = back ? s.d : s.b;
                if (p < a) break;
                if (p <= b)
                    return (
                        (back ? s.a : s.c) +
                        (back ? s.map.toSource(p - a) : s.map.toDisplay(p - a))
                    );
                shift = (back ? s.b : s.d) - b;
            }
            return p + shift;
        }
        return {
            toSource: (p) => point(p, true),
            toDisplay: (p) => point(p, false),
        };
    }
    function decodedToken(raw, literal = false) {
        const edits = [];
        let output = "",
            cursor = 0;
        const expression = literal
            ? /\r\n?|\0/g
            : /&(?:#[xX][0-9a-fA-F]+;?|#[0-9]+;?|[A-Za-z][A-Za-z0-9]*;?)|\r\n?|\0/g;
        const box = document.createElement("textarea");
        for (const match of raw.matchAll(expression)) {
            output += raw.slice(cursor, match.index);
            const a = match.index,
                b = a + match[0].length,
                c = output.length;
            let value = match[0];
            if (value[0] === "&") {
                box.innerHTML = value;
                value = box.value;
            } else if (value[0] === "\r") value = "\n";
            else value = literal ? "\ufffd" : "";
            output += value;
            edits.push({ a, b, c, d: output.length });
            cursor = b;
        }
        output += raw.slice(cursor);
        return { text: output, ...decodedMapper(edits) };
    }
    function decodedMapper(edits) {
        function point(p, back) {
            let shift = 0;
            for (const e of edits) {
                const a = back ? e.c : e.a,
                    b = back ? e.d : e.b,
                    c = back ? e.a : e.c,
                    d = back ? e.b : e.d;
                if (p < a) break;
                if (p < b) return c;
                shift = d - b;
            }
            return p + shift;
        }
        return {
            toSource: (p) => point(p, true),
            toDisplay: (p) => point(p, false),
        };
    }
    function lexical(html) {
        const result = [];
        const tag =
            /<!--[\s\S]*?-->|<![^>]*>|<\/?[A-Za-z](?:[^'"<>]|"[^"]*"|'[^']*')*>/g;
        let cursor = 0,
            match;
        while ((match = tag.exec(html))) {
            if (match.index > cursor)
                result.push({
                    a: cursor,
                    b: match.index,
                    text: html.slice(cursor, match.index),
                });
            result.push({ a: match.index, b: tag.lastIndex, tag: match[0] });
            cursor = tag.lastIndex;
            const name = /^<(script|style|textarea|title)\b/i.exec(match[0]);
            if (name) {
                const close = new RegExp("</" + name[1] + "\\s*>", "ig");
                close.lastIndex = cursor;
                const end = close.exec(html);
                const stop = end ? end.index : html.length;
                if (stop > cursor)
                    result.push({
                        a: cursor,
                        b: stop,
                        text: html.slice(cursor, stop),
                        literal: /^(script|style)$/i.test(name[1]),
                    });
                if (end) {
                    result.push({
                        a: end.index,
                        b: close.lastIndex,
                        tag: end[0],
                    });
                    cursor = close.lastIndex;
                    tag.lastIndex = cursor;
                } else {
                    cursor = html.length;
                    break;
                }
            }
        }
        if (cursor < html.length)
            result.push({
                a: cursor,
                b: html.length,
                text: html.slice(cursor),
            });
        return result;
    }
    function parsedProvenance(html, actual) {
        const nonce = "rm" + crypto.randomUUID().replaceAll("-", ""),
            attr = "data-" + nonce;
        const prefix = "\ue000" + nonce + "_",
            suffix = "\ue001",
            tokens = lexical(html),
            byId = new Map();
        let skeleton = "";
        for (let i = 0; i < tokens.length; i++) {
            const token = tokens[i];
            byId.set(String(i), token);
            if (token.tag) {
                skeleton += /^<[A-Za-z]/.test(token.tag)
                    ? token.tag.replace(
                          /\/?\s*>$/,
                          (end) => " " + attr + '="' + i + '"' + end,
                      )
                    : token.tag;
            } else if (/\S/.test(token.text)) {
                token.decoded = decodedToken(token.text, token.literal);
                skeleton += prefix + i + suffix;
            } else skeleton += token.text;
        }
        const doc = new DOMParser().parseFromString(skeleton, "text/html"),
            units = window.readerBlocks.collect(doc.body);
        if (units.length !== actual.length) return null;
        const marker = new RegExp(prefix + "(\\d+)" + suffix, "g"),
            ranges = [],
            anchors = [],
            lengths = [];
        const recorded = new Set();
        function signature(node) {
            if (node.nodeType === 3) return node.data;
            if (node.nodeType === 1)
                return [node.localName, [...node.childNodes].map(signature)];
            return node.nodeType;
        }
        for (let i = 0; i < units.length; i++) {
            let offset = 0;
            function anchor(node, at) {
                const id = node.getAttribute?.(attr),
                    token = byId.get(id);
                if (token && !recorded.has(id)) {
                    anchors.push({
                        a: token.a,
                        b: token.b,
                        index: i,
                        offset: at,
                    });
                    recorded.add(id);
                }
            }
            function walk(node) {
                if (node.nodeType === 3) {
                    const value = node.data;
                    let text = "",
                        cursor = 0;
                    marker.lastIndex = 0;
                    let m;
                    while ((m = marker.exec(value))) {
                        text += value.slice(cursor, m.index);
                        const token = byId.get(m[1]);
                        const at = offset + text.length;
                        text += token.decoded.text;
                        ranges.push({
                            a: token.a,
                            b: token.b,
                            index: i,
                            offset: at,
                            length: token.decoded.text.length,
                            map: {
                                toSource: token.decoded.toSource,
                                toDisplay: token.decoded.toDisplay,
                            },
                        });
                        cursor = marker.lastIndex;
                    }
                    text += value.slice(cursor);
                    node.data = text;
                    offset += text.length;
                } else if (node.nodeType === 1) {
                    anchor(node, offset);
                    for (const child of node.childNodes) walk(child);
                }
            }
            for (const node of units[i].chain || []) anchor(node, 0);
            units[i].nodes.forEach(walk);
            lengths.push(offset);
            if (
                JSON.stringify(units[i].nodes.map(signature)) !==
                JSON.stringify(actual[i].nodes.map(signature))
            )
                return null;
        }
        const byUnit = Array.from({ length: units.length }, () => []);
        for (const r of ranges) byUnit[r.index].push(r);
        const ordered = [...ranges, ...anchors].sort((a, b) => a.a - b.a);
        return provenanceLookup(byUnit, anchors, lengths, ranges, ordered);
    }
    function provenanceLookup(byUnit, anchors, lengths, ranges, ordered) {
        function toRaw(position) {
            const { index, offset = 0 } = position;
            if (
                !Number.isSafeInteger(index) ||
                !Number.isSafeInteger(offset) ||
                offset < 0 ||
                index < 0 ||
                index >= lengths.length ||
                offset > lengths[index]
            )
                return null;
            for (const r of byUnit[index])
                if (offset >= r.offset && offset < r.offset + r.length)
                    return r.a + r.map.toSource(offset - r.offset);
            const next = byUnit[index].find((r) => r.offset >= offset);
            if (next) return next.a;
            const last = byUnit[index].at(-1);
            if (last && offset === lengths[index] && lengths[index])
                return last.b;
            return (
                anchors.find((a) => a.index === index && a.offset === offset)
                    ?.a ?? null
            );
        }
        function fromRaw(point) {
            const tag = anchors.find((a) => point >= a.a && point < a.b);
            if (tag) return { index: tag.index, offset: tag.offset };
            const r = ranges.find((r) => point >= r.a && point < r.b);
            if (r)
                return {
                    index: r.index,
                    offset: r.offset + r.map.toDisplay(point - r.a),
                };
            const next = ordered.find((r) => r.a >= point);
            if (next) return { index: next.index, offset: next.offset };
            return lengths.length
                ? { index: lengths.length - 1, offset: lengths.at(-1) }
                : null;
        }
        return { toRaw, fromRaw };
    }
    function buildHtml(html, display, rules) {
        if (!rules.length)
            return {
                toSource: (p) => ({ ...p }),
                toDisplay: (p) => ({ ...p }),
            };
        const transformed = rawTransform(html, rules);
        if (transformed.text === html)
            return {
                toSource: (p) => ({ ...p }),
                toDisplay: (p) => ({ ...p }),
            };
        const original = window.readerBlocks.collect(
            new DOMParser().parseFromString(html, "text/html").body,
        );
        const direct = build(original, display, rules);
        if (direct) return direct;
        const sourceMap = parsedProvenance(html, original),
            displayMap = parsedProvenance(transformed.text, display);
        if (!sourceMap || !displayMap) return null;
        return composeProvenance(sourceMap, displayMap, {
            toSource: transformed.toSource,
            toDisplay: transformed.toDisplay,
        });
    }
    function composeProvenance(sourceMap, displayMap, change) {
        return {
            toSource: (p) => {
                const raw = displayMap.toRaw(p);
                return raw === null
                    ? null
                    : sourceMap.fromRaw(change.toSource(raw));
            },
            toDisplay: (p) => {
                const raw = sourceMap.toRaw(p);
                return raw === null
                    ? null
                    : displayMap.fromRaw(change.toDisplay(raw));
            },
        };
    }

    function migrateHtml(html, display, rules, position) {
        const old = window.readerReplacements.replaceHtml(html, rules, {legacy: true});
        const next = window.readerReplacements.replaceHtml(html, rules);
        if (old === next) return {...position};
        const originalDisplay = window.readerBlocks.collect(new DOMParser().parseFromString(old, "text/html").body);
        const before = parsedProvenance(old, originalDisplay), after = parsedProvenance(next, display);
        if (!before || !after) return null;
        const point = before.toRaw(position);
        if (point === null) return null;
        const scanner = /<(script|style)\b[\s\S]*?<\/\1\s*>|>([^<]+)</gi;
        let oldDelta = 0, newDelta = 0, match;
        while ((match = scanner.exec(html))) {
            if (match[2] === undefined) continue;
            const oldText = edit(match[2], rules, {raw: true, legacy: true}).text;
            const newText = edit(match[2], rules, {raw: true}).text;
            const a = match.index + 1 + oldDelta, c = match.index + 1 + newDelta;
            if (point < a) break;
            if (point <= a + oldText.length) {
                return after.fromRaw(c + window.readerReplacements.migrate(match[2], rules, point - a));
            }
            oldDelta += oldText.length - match[2].length;
            newDelta += newText.length - match[2].length;
        }
        return after.fromRaw(point + newDelta - oldDelta);
    }
    globalThis.readerSourceAnchor = { build, buildHtml, edit, digest, migrateHtml,
        version: 2, assetUrl: document.currentScript?.src };
})();
