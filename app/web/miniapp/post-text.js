"use strict";

const postFormatOpen = "<telegram-format>";
const postFormatClose = "</telegram-format>";
const postFormatTags = ["blockquote", "a", "b", "i", "u", "s", "tg-spoiler", "pre", "code"];
const postFormatAliases = {strong:"b",em:"i",strike:"s",del:"s"};
const legacyPostTokens = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|(?<![\p{L}\p{N}_])_([^_\n]+)_(?![\p{L}\p{N}_])|~~([^~\n]+)~~|\|\|([^|\n]+)\|\||```(?:[\w+-]+\n)?([\s\S]+?)```|`([^`\n]+)`/gu;
const postAutolinks = /https?:\/\/[^\s<>()]+|#[\p{L}\p{M}\p{N}_]+/gu;

function postLink(value) {
    try {
        const address = new URL(value);
        return ["http:","https:"].includes(address.protocol) ? address.href : "";
    } catch { return ""; }
}

function readPostRuns(root) {
    const runs = [];
    function add(text, marks) {
        if (text) runs.push({text, marks});
    }
    function visit(node, marks) {
        if (node.nodeType === Node.TEXT_NODE) {
            add(node.data, marks);
            return;
        }
        const tag = node.localName;
        if (tag === "br") {
            if (node.nextSibling || (node.previousSibling && node.previousSibling.localName !== "br")) add("\n", marks);
            return;
        }
        const format = node.dataset?.postFormat || postFormatAliases[tag] || tag;
        if (postFormatTags.includes(format)) {
            const value = format === "a" ? postLink(node.getAttribute("href") || node.dataset.href)
                : format === "blockquote" ? (node.hasAttribute("expandable") || node.dataset.expandable ? "expandable" : true) : true;
            if (value) marks = {...marks, [format]:value};
        }
        if (["div", "p"].includes(tag) && runs.length && node.previousSibling) add("\n", {});
        for (const child of node.childNodes) visit(child, marks);
    }
    for (const child of root.childNodes) visit(child, {});
    return runs;
}

function parsePostRuns(value) {
    const text = String(value || "");
    if (text.startsWith(postFormatOpen) && text.endsWith(postFormatClose)) {
        const template = document.createElement("template");
        template.innerHTML = text.slice(postFormatOpen.length,-postFormatClose.length);
        return readPostRuns(template.content);
    }
    const runs = [];
    let end = 0;
    for (const match of text.matchAll(legacyPostTokens)) {
        if (match.index > end) runs.push({text:text.slice(end,match.index),marks:{}});
        if (match[1]) runs.push({text:match[1],marks:{a:postLink(match[2])}});
        else {
            const styles = ["b","u","i","s","tg-spoiler","pre","code"];
            const index = match.slice(3).findIndex(Boolean);
            runs.push({text:match[index + 3],marks:{[styles[index]]:true}});
        }
        end = match.index + match[0].length;
    }
    if (end < text.length) runs.push({text:text.slice(end),marks:{}});
    return runs;
}

function appendAutoLinkedText(root, text) {
    let end = 0;
    for (const match of text.matchAll(postAutolinks)) {
        root.append(document.createTextNode(text.slice(end,match.index)));
        const raw = match[0];
        if (raw.startsWith("#")) root.append(el("span",raw,"tg-hashtag"));
        else {
            const url = raw.replace(/[.,!?:;]+$/, "");
            const link = el("a",url,"tg-link");
            link.href = url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            root.append(link,document.createTextNode(raw.slice(url.length)));
        }
        end = match.index + raw.length;
    }
    root.append(document.createTextNode(text.slice(end)));
}

function makePostFormat(tag, value, mode) {
    if (mode === "edit" && ["tg-spoiler","a","pre","blockquote"].includes(tag)) {
        const span = el("span",undefined,"edit-format-" + tag);
        span.dataset.postFormat = tag;
        if (tag === "a") span.dataset.href = value;
        if (tag === "blockquote" && value === "expandable") span.dataset.expandable = "true";
        return span;
    }
    if (mode === "preview" && tag === "tg-spoiler") {
        const spoiler = el("span",undefined,"tg-spoiler");
        spoiler.setAttribute("role","button");
        spoiler.setAttribute("aria-label","Показать спойлер");
        spoiler.tabIndex = 0;
        const content = el("span",undefined,"tg-spoiler-copy");
        content.inert = true;
        content.setAttribute("aria-hidden","true");
        spoiler.append(content);
        function reveal(event) {
            if (event.type === "keydown" && ![" ","Enter"].includes(event.key)) return;
            event.preventDefault();
            spoiler.classList.add("is-revealed");
            spoiler.removeAttribute("role");
            spoiler.removeAttribute("aria-label");
            spoiler.removeAttribute("tabindex");
            content.inert = false;
            content.removeAttribute("aria-hidden");
            spoiler.removeEventListener("click",reveal);
            spoiler.removeEventListener("keydown",reveal);
        }
        spoiler.addEventListener("click",reveal);
        spoiler.addEventListener("keydown",reveal);
        return [spoiler,content];
    }
    if (mode === "preview" && tag === "blockquote" && value === "expandable") {
        const details = el("details",undefined,"tg-quote");
        const quote = el("blockquote");
        details.append(el("summary","Цитата"),quote);
        return [details,quote];
    }
    const node = el(tag);
    if (tag === "a") {
        node.href = value;
        if (mode === "preview") {
            node.className = "tg-link";
            node.target = "_blank";
            node.rel = "noopener noreferrer";
        }
    }
    if (tag === "blockquote" && value === "expandable") node.setAttribute("expandable","");
    return node;
}

function renderPostRuns(root, runs, mode) {
    root.replaceChildren();
    const stack = [];
    for (const {text,marks} of runs) {
        const active = postFormatTags.filter(tag => marks[tag]);
        let depth = 0;
        while (depth < stack.length && active[depth] === stack[depth].tag &&
            marks[active[depth]] === stack[depth].value) depth++;
        stack.length = depth;
        let parent = stack.at(-1)?.content || root;
        for (const tag of active.slice(depth)) {
            const value = marks[tag];
            const result = makePostFormat(tag,value,mode);
            const [node,content] = Array.isArray(result) ? result : [result,result];
            parent.append(node);
            stack.push({tag,value,content});
            parent = content;
        }
        if (mode === "preview" && !["code","pre","a"].some(tag => marks[tag])) appendAutoLinkedText(parent,text);
        else parent.append(document.createTextNode(text));
    }
}

function appendPostText(container, text) {
    renderPostRuns(container,parsePostRuns(text),"preview");
}

function serializePostRuns(runs) {
    const markup = document.createElement("div");
    renderPostRuns(markup,runs,"save");
    return postFormatOpen + markup.innerHTML + postFormatClose;
}

function postSelection(editor) {
    const selection = document.getSelection();
    if (!selection?.rangeCount) return null;
    const range = selection.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer) || range.collapsed) return null;
    function offset(node, offset) {
        const leading = document.createRange();
        leading.selectNodeContents(editor);
        leading.setEnd(node,offset);
        return readPostRuns(leading.cloneContents()).reduce((sum,run) => sum + run.text.length,0);
    }
    return [offset(range.startContainer,range.startOffset),offset(range.endContainer,range.endOffset)];
}

function restorePostSelection(editor, start, end) {
    const walker = document.createTreeWalker(editor,NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    let index = 0;
    let started = false;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const last = index + node.length;
        if (!started && start <= last) {
            range.setStart(node,start - index);
            started = true;
        }
        if (end <= last) {
            range.setEnd(node,end - index);
            document.getSelection().removeAllRanges();
            document.getSelection().addRange(range);
            return;
        }
        index = last;
    }
}

function postLinks(runs, selection) {
    const links = [];
    let offset = 0;
    for (const {text,marks} of runs) {
        const tail = links.at(-1);
        if (marks.a) {
            if (tail?.href === marks.a && tail.end === offset) tail.end += text.length;
            else links.push({start:offset,end:offset + text.length,href:marks.a});
        }
        offset += text.length;
    }
    return links.filter(link => link.start < selection[1] && selection[0] < link.end);
}

function replacePostLink(runs, selection, href) {
    const links = postLinks(runs,selection);
    for (const {start,end} of links) runs = applyPostFormat(runs,[start,end],"a","");
    if (!href) return runs;
    const start = Math.min(selection[0],links[0]?.start ?? selection[0]);
    const end = Math.max(selection[1],links.at(-1)?.end ?? selection[1]);
    return applyPostFormat(runs,[start,end],"a",href);
}

function applyPostFormat(runs, [start,end], tag, value) {
    const result = [];
    let index = 0;
    const clear = tag !== "a" && runs.filter(run => {
        const first = index;
        index += run.text.length;
        return start < index && end > first;
    }).every(run => run.marks[tag] === value);
    index = 0;
    for (const run of runs) {
        const first = Math.max(0,start - index);
        const last = Math.min(run.text.length,end - index);
        index += run.text.length;
        if (first >= last) {
            result.push(run);
            continue;
        }
        if (first) result.push({...run,text:run.text.slice(0,first)});
        let marks = {...run.marks};
        if (tag === "clear") marks = {};
        else if (clear || !value) delete marks[tag];
        else if (tag === "code" || tag === "pre") marks = {[tag]:value};
        else {
            delete marks.code;
            delete marks.pre;
            marks[tag] = value;
        }
        if (marks.blockquote && (marks.code || marks.pre)) delete marks.blockquote;
        result.push({text:run.text.slice(first,last),marks});
        if (last < run.text.length) result.push({...run,text:run.text.slice(last)});
    }
    return result;
}
