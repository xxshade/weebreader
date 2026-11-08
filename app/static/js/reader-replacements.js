/** Versioned literal matching shared by rendering and source-position maps. */
(() => {
    const version = 2;
    const space = "[ \u00a0]";
    const rawSpace = space + "|&(?:nbsp;?|NonBreakingSpace;|#0*160(?![0-9]);?|#[xX]0*[aA]0(?![0-9a-fA-F]);?)";
    const escape = value => [...value].map(c => ".*+?^$(){}[]|".includes(c) || c.charCodeAt(0) === 92
        ? String.fromCharCode(92) + c : c).join("");
    const patterns = new Map();
    const hasSpace = find => new RegExp(rawSpace).test(find);
    function pattern(find, {raw = false, legacy = false} = {}) {
        const key = JSON.stringify([find, raw, legacy]);
        if (patterns.has(key)) { const value = patterns.get(key); value.lastIndex = 0; return value; }
        if (patterns.size >= 512) patterns.delete(patterns.keys().next().value);
        if (legacy || !hasSpace(find)) {
            const value = new RegExp(escape(find), "g"); patterns.set(key,value); return value;
        }
        const tokens = new RegExp(raw ? rawSpace : space, "g");
        let output = "", cursor = 0, match;
        while ((match = tokens.exec(find))) {
            output += escape(find.slice(cursor, match.index)) + "(?:" + tokens.source + ")";
            cursor = match.index + match[0].length;
        }
        const value = new RegExp(output + escape(find.slice(cursor)), "g");
        patterns.set(key, value); return value;
    }
    function trace(text, rules, options = {}) {
        const stages = [];
        for (const rule of rules) {
            const edits = [];
            if (rule.find) {
                const matcher = pattern(rule.find, options), replacement = rule.replace || "";
                let cursor = 0, output = "", match;
                while ((match = matcher.exec(text))) {
                    output += text.slice(cursor, match.index);
                    edits.push({a: match.index, b: match.index + match[0].length,
                        c: output.length, d: output.length + replacement.length, match: match[0]});
                    output += replacement;
                    cursor = match.index + match[0].length;
                }
                text = output + text.slice(cursor);
            }
            stages.push(edits);
        }
        return {text, stages};
    }
    function translate(point, stages, backwards = false) {
        for (const edits of backwards ? [...stages].reverse() : stages) {
            let shift = 0;
            for (const edit of edits) {
                const [a, b, c, d] = (backwards ? ["c", "d", "a", "b"] : ["a", "b", "c", "d"]).map(k => edit[k]);
                if (point < a) break;
                if (point < b) { point = c; shift = 0; break; }
                shift = d - b;
            }
            point += shift;
        }
        return point;
    }
    function replaceHtml(html, rules, {legacy = false} = {}) {
        if (!rules.length) return html;
        return html.replace(/<(script|style)\b[\s\S]*?<\/\1\s*>|>([^<]+)</gi, (match, tag, text) => {
            if (text === undefined) return match;
            for (const rule of rules) {
                if (rule.find) text = legacy || !hasSpace(rule.find)
                    ? text.split(rule.find).join(rule.replace || "")
                    : text.replace(pattern(rule.find, {raw: true}), () => rule.replace || "");
            }
            return ">" + text + "<";
        });
    }
    function migrate(text, rules, point) {
        const old = trace(text, rules, {raw: true, legacy: true}).stages;
        const next = trace(text, rules, {raw: true}).stages, inside = new Map();
        for (let index = old.length - 1; index >= 0; index--) {
            const edit = old[index].find(e => e.c <= point && point < e.d);
            if (edit) inside.set(index, {offset: point - edit.c, match: edit.match});
            point = translate(point, [old[index]], true);
        }
        for (let index = 0; index < next.length; index++) {
            const saved = inside.get(index);
            const edit = saved && next[index].find(e => e.a === point && e.match === saved.match);
            point = edit ? edit.c + Math.min(saved.offset, edit.d - edit.c) : translate(point, [next[index]]);
        }
        return point;
    }
    globalThis.readerReplacements = {version, pattern, trace, translate, replaceHtml, migrate,
        assetUrl: document.currentScript?.src};
})();
