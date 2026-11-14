"use strict";
const telegram = window.Telegram?.WebApp;
if (telegram?.isVersionAtLeast?.("6.9")) telegram.setHeaderColor("#ffffff");
if (telegram?.isVersionAtLeast?.("6.1")) telegram.setBackgroundColor("#ffffff");
if (telegram?.isVersionAtLeast?.("7.10")) {
    telegram.setBottomBarColor?.("#ffffff");
    telegram.disableVerticalSwipes?.();
}
telegram?.ready();
function syncTelegramInsets() {
    const root = document.documentElement;
    const safe = telegram?.safeAreaInset || {};
    const content = telegram?.contentSafeAreaInset || {};
    root.style.setProperty("--tg-top", (Number(safe.top || 0) + Number(content.top || 0)) + "px");
    root.style.setProperty("--tg-bottom", (Number(safe.bottom || 0) + Number(content.bottom || 0)) + "px");
}
syncTelegramInsets();
for (const event of ["safeAreaChanged", "contentSafeAreaChanged", "fullscreenChanged"]) {
    telegram?.onEvent?.(event, syncTelegramInsets);
}
if (telegram?.isVersionAtLeast?.("8.0") && !telegram.isFullscreen) {
    telegram.requestFullscreen?.();
}
const initData = telegram?.initData || "";
const screen = document.querySelector("#screen");
const tabs = document.querySelector("#tabs");
const notice = document.querySelector("#notice");
const sheet = document.querySelector("#sheet");
const sheetBody = document.querySelector("#sheet-body");
const refresh = document.querySelector("#refresh");
const chatLogs = new Map();
let state;
let tab = "profile";
let initial = true;
let sending = false;
let pendingChat;
const readCache = new Map();
const readRequests = new Map();
const critiqueResults = new Map();
const critiqueRequests = new Map();
let cacheVersion = 0;
let activeTabAnimation;
let sheetBack;
let sheetDrag;
let sheetMotion;
let sheetView = 0;
const videoUrls = new Map();
const mediaObserver = new MutationObserver(() => {
    for (const [node, url] of videoUrls) {
        if (node.isConnected) continue;
        URL.revokeObjectURL(url);
        videoUrls.delete(node);
    }
});
mediaObserver.observe(document.body, {childList:true,subtree:true});

function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
}

function icon(name) {
    const namespace = "http://www.w3.org/2000/svg";
    const image = document.createElementNS(namespace, "svg");
    image.setAttribute("class", "icon");
    image.setAttribute("aria-hidden", "true");
    const shape = document.createElementNS(namespace, "use");
    shape.setAttribute("href", "#" + name);
    image.append(shape);
    return image;
}

function chatIcon(path) {
    const namespace = "http://www.w3.org/2000/svg";
    const image = document.createElementNS(namespace, "svg");
    image.setAttribute("class", "icon");
    image.setAttribute("viewBox", "0 0 24 24");
    image.setAttribute("aria-hidden", "true");
    const shape = document.createElementNS(namespace, "path");
    shape.setAttribute("d", path);
    image.append(shape);
    return image;
}

for (const node of document.querySelectorAll("[data-icon]")) {
    node.append(icon(node.dataset.icon));
}

function button(label, handler, style) {
    const node = el("button", label, style);
    node.type = "button";
    node.addEventListener("click", () => guarded(node, handler));
    return node;
}

function feedback(text) {
    notice.textContent = text;
    notice.hidden = !text;
}

async function guarded(node, action) {
    feedback("");
    node.disabled = true;
    const busy = node.matches("button") ? setTimeout(() => {
        node.setAttribute("aria-busy", "true");
        node.classList.add("is-working");
    }, 130) : undefined;
    try { await action(); }
    catch (error) {
        const message = error.message || "Не удалось выполнить действие.";
        if (sheet.open) {
            sheetBody.querySelector(".sheet-error")?.remove();
            const alert = el("p", message, "sheet-error");
            alert.setAttribute("role", "alert");
            sheetBody.prepend(alert);
            alert.scrollIntoView({block:"nearest"});
        } else feedback(message);
    }
    finally {
        clearTimeout(busy);
        node.classList.remove("is-working");
        node.removeAttribute("aria-busy");
        node.disabled = false;
    }
}

async function api(path, method = "GET", data, signal) {
    const headers = {Authorization: "Telegram " + initData};
    let body;
    if (data instanceof File) {
        headers["Content-Type"] = "application/octet-stream";
        headers["X-Filename"] = encodeURIComponent(data.name);
        body = data;
    } else if (data !== undefined) {
        headers["Content-Type"] = "application/json";
        body = JSON.stringify(data);
    }
    const response = await fetch("/miniapp/api/" + path, {
        method, headers, body, signal: signal || AbortSignal.timeout(method === "PUT" ? 150000 : 240000),
    });
    const result = await response.json().catch(() => null);
    if (!response.ok || !result) throw new Error(result?.error || "Сервис недоступен. Попробуйте ещё раз.");
    if (method !== "GET") {
        cacheVersion++;
        readCache.clear();
        readRequests.clear();
    }
    return result;
}

function cachedApi(path, ttl = 15000) {
    const key = state.active + ":" + path;
    const cached = readCache.get(key);
    if (cached?.expires > Date.now()) return Promise.resolve(cached.result);
    if (readRequests.has(key)) return readRequests.get(key);
    const version = cacheVersion;
    const request = api(path,"GET",undefined,AbortSignal.timeout(15000)).then(result => {
        if (version === cacheVersion) readCache.set(key, {result,expires:Date.now() + ttl});
        return result;
    }).finally(() => {
        if (readRequests.get(key) === request) readRequests.delete(key);
    });
    readRequests.set(key, request);
    return request;
}

async function load() {
    state = await api("state","GET",undefined,AbortSignal.timeout(20000));
    if (state.active && !sending) chatLogs.set(state.active, state.history || []);
    if (initial && state.active) tab = "home";
    initial = false;
    if (!state.active) tab = "profile";
    render();
}

function showSheet(title, content, subtitle = "", back = null) {
    sheetView++;
    sheetMotion?.cancel();
    sheetDrag = null;
    sheet.style.transform = "";
    document.querySelector("#sheet-title").textContent = title;
    const description = document.querySelector("#sheet-subtitle");
    description.textContent = subtitle;
    description.hidden = !subtitle;
    sheetBack = back;
    document.querySelector("#sheet-back").hidden = !back;
    sheetBody.setAttribute("aria-busy",String(content.classList.contains("loading-content")));
    sheetBody.replaceChildren(content);
    if (!sheet.open) sheet.showModal();
    sheet.scrollTop = 0;
}

function loadingContent(kind) {
    const content = el("div",undefined,"loading-content");
    content.dataset.kind = kind;
    const status = el("p",undefined,"loading-status");
    status.setAttribute("role","status");
    status.append(el("i",undefined,"loading-spinner"),el("span","Загружаем…"));
    content.append(status);
    const skeleton = el("div",undefined,"loading-skeleton");
    skeleton.setAttribute("aria-hidden","true");
    for (let index = 0; index < 3; index++) skeleton.append(el("div"));
    content.append(skeleton);
    return content;
}

async function loadSheet(title, kind, read, renderContent, {subtitle = "", back = null} = {}) {
    showSheet(title, loadingContent(kind), subtitle, back);
    const view = sheetView;
    const company = state.active;
    const isCurrent = () => sheet.open && sheetView === view && state.active === company;
    try {
        const data = await read();
        if (isCurrent()) renderContent(data);
    } catch (error) {
        if (!isCurrent()) return;
        const panel = el("div",undefined,"loading-error");
        const message = el("p",error.message || "Не удалось загрузить данные.","sheet-error");
        message.setAttribute("role","alert");
        panel.append(message,button("Повторить",() => loadSheet(title,kind,read,renderContent,{subtitle,back}),"primary"));
        sheetBody.setAttribute("aria-busy","false");
        sheetBody.replaceChildren(panel);
    }
}

document.querySelector("#close").addEventListener("click", () => sheet.close());
const backButton = document.querySelector("#sheet-back");
backButton.append(chatIcon("M15 18l-6-6 6-6"));
backButton.addEventListener("click", () => {
    if (sheetBack) guarded(backButton, sheetBack);
});
sheet.addEventListener("close", () => {
    sheetMotion?.cancel();
    sheetDrag = null;
    sheet.style.transform = "";
    for (const video of sheetBody.querySelectorAll("video")) video.pause();
});
sheet.addEventListener("touchstart", event => {
    if (event.touches.length !== 1 || !sheet.contains(event.target) ||
        event.target.closest("button,a,input,textarea,select,summary,video")) return;
    if (!event.target.closest(".sheet-bar") && sheet.scrollTop > 1) return;
    sheetMotion?.cancel();
    const touch = event.touches[0];
    sheetDrag = {x:touch.clientX,y:touch.clientY,at:performance.now(),offset:0};
}, {passive:true});
sheet.addEventListener("touchmove", event => {
    if (!sheetDrag) return;
    const touch = event.touches[0];
    const dx = touch.clientX - sheetDrag.x;
    const dy = touch.clientY - sheetDrag.y;
    if (!sheetDrag.active) {
        if (dy < -6 || (Math.abs(dx) > 8 && Math.abs(dx) > dy)) {
            sheetDrag = null;
            return;
        }
        if (dy < 6 || !event.cancelable) return;
        sheetDrag.active = true;
    }
    if (event.cancelable) event.preventDefault();
    sheetDrag.offset = Math.max(0,dy);
    if (!reduceMotion.matches) sheet.style.transform = "translateY(" + sheetDrag.offset + "px)";
}, {passive:false});
function finishSheetDrag(event) {
    const drag = sheetDrag;
    sheetDrag = null;
    if (!drag?.active) return;
    const quick = performance.now() - drag.at < 260 && drag.offset >= 42;
    const dismiss = event.type === "touchend" && (drag.offset >= 90 || quick);
    if (dismiss) sheetView++;
    if (reduceMotion.matches) {
        if (dismiss) sheet.close();
        return;
    }
    const animation = sheet.animate([
        {transform:"translateY(" + drag.offset + "px)"},
        {transform:dismiss ? "translateY(100%)" : "translateY(0)"},
    ], {duration:160,easing:"cubic-bezier(.28,0,.22,1)",fill:"forwards"});
    sheetMotion = animation;
    sheet.style.transform = "";
    animation.finished.then(() => {
        if (sheetMotion !== animation) return;
        sheetMotion = undefined;
        if (dismiss) sheet.close();
        animation.cancel();
    }, () => {});
}
sheet.addEventListener("touchend",finishSheetDrag,{passive:true});
sheet.addEventListener("touchcancel",finishSheetDrag,{passive:true});
refresh.addEventListener("click", () => guarded(refresh, load));
document.querySelector("#user-avatar").addEventListener("click", () => openTab("profile"));
const tabOrder = ["home", "chat", "profile"];
const page = document.querySelector(".page");
const viewport = document.querySelector(".viewport");
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");
let swipe;
let suppressClickUntil = 0;

function openTab(next, dragged = 0) {
    if (!state || next === tab) return;
    activeTabAnimation?.cancel();
    const direction = Math.sign(tabOrder.indexOf(next) - tabOrder.indexOf(tab));
    const width = page.clientWidth;
    const animate = !reduceMotion.matches && width;
    let outgoing;
    if (animate) {
        outgoing = page.cloneNode(true);
        outgoing.classList.add("page-outgoing");
        outgoing.setAttribute("aria-hidden","true");
        outgoing.inert = true;
        outgoing.removeAttribute("id");
        for (const node of outgoing.querySelectorAll("[id]")) node.removeAttribute("id");
        viewport.append(outgoing);
        outgoing.querySelector("main").scrollTop = screen.scrollTop;
        const outgoingMessages = outgoing.querySelector(".messages");
        if (outgoingMessages) outgoingMessages.scrollTop = screen.querySelector(".messages").scrollTop;
    }
    page.style.transform = "";
    tab = next;
    feedback("");
    render();
    if (next !== "chat") screen.scrollTop = 0;
    if (animate) {
        const motion = {duration:dragged ? 125 : 110,easing:"cubic-bezier(.28,0,.22,1)"};
        const slide = x => ({transform:"translate3d(" + x + "px,0,0)"});
        outgoing.animate([slide(dragged),slide(-direction * width)], {...motion,fill:"forwards"});
        const incoming = page.animate([slide(direction * width + dragged),slide(0)],motion);
        activeTabAnimation = incoming;
        const cleanUp = () => {
            outgoing.remove();
            if (activeTabAnimation === incoming) activeTabAnimation = undefined;
        };
        incoming.finished.then(cleanUp, cleanUp);
    }
}

tabs.addEventListener("click", event => {
    const target = event.target.closest("button[data-tab]");
    if (target) openTab(target.dataset.tab);
});

viewport.addEventListener("touchstart", event => {
    if (!state?.active || sheet.open || storyViewer.open ||
        event.touches.length !== 1 || event.target.closest("input,textarea,select,.profile-switcher")) return;
    const point = event.touches[0];
    swipe = {x:point.clientX,y:point.clientY,offset:0};
}, {passive:true});
viewport.addEventListener("touchmove", event => {
    if (!swipe) return;
    const point = event.touches[0];
    const dx = point.clientX - swipe.x;
    const dy = point.clientY - swipe.y;
    if (!swipe.horizontal) {
        if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) {
            swipe = null;
            return;
        }
        if (Math.abs(dx) < 12 || Math.abs(dx) < 1.35 * Math.abs(dy)) return;
        swipe.horizontal = true;
        activeTabAnimation?.cancel();
    }
    const neighbor = tabOrder[tabOrder.indexOf(tab) + (dx < 0 ? 1 : -1)];
    const width = viewport.clientWidth;
    swipe.offset = Math.sign(dx) * Math.min(width * .85, Math.abs(dx) * (neighbor ? .74 : .14));
    if (!reduceMotion.matches) page.style.transform = "translate3d(" + swipe.offset + "px,0,0)";
}, {passive:true});
function endSwipe(event) {
    if (!swipe) return;
    const offset = swipe.offset;
    const delta = event.changedTouches[0].clientX - swipe.x;
    const horizontal = swipe.horizontal;
    swipe = null;
    if (horizontal) suppressClickUntil = performance.now() + 125;
    const next = tabOrder[tabOrder.indexOf(tab) + (delta < 0 ? 1 : -1)];
    if (event.type === "touchend" && horizontal && Math.abs(delta) >= 54 && next) {
        openTab(next, reduceMotion.matches ? 0 : offset);
        return;
    }
    page.style.transform = "";
    if (offset && !reduceMotion.matches) page.animate(
        [{transform:"translate3d(" + offset + "px,0,0)"},{transform:"translate3d(0,0,0)"}],
        {duration:95,easing:"cubic-bezier(.28,0,.22,1)"}
    );
}
viewport.addEventListener("touchend",endSwipe,{passive:true});
viewport.addEventListener("touchcancel",endSwipe,{passive:true});
viewport.addEventListener("click",event => {
    if (performance.now() < suppressClickUntil) {
        event.preventDefault();
        event.stopImmediatePropagation();
    }
},true);

function greeting(name) {
    const hour = Number(new Intl.DateTimeFormat("ru-RU", {
        hour:"numeric", hourCycle:"h23", timeZone:state.timezone,
    }).format(new Date()));
    const label = hour < 5 ? "Доброй ночи" : hour < 12 ? "Доброе утро" :
        hour < 18 ? "Добрый день" : "Добрый вечер";
    return label + ", " + name;
}

function render() {
    const company = state.profiles.find((item) => item.id === state.active);
    const avatar = document.querySelector("#user-avatar");
    avatar.hidden = tab !== "home";
    const firstName = state.user.first_name;
    if (tab === "home") {
        avatar.textContent = (firstName || "М").slice(0,1);
        if (state.user.photo_url?.startsWith("https://")) {
            const image = el("img");
            image.alt = "";
            image.src = state.user.photo_url;
            avatar.replaceChildren(image);
            image.onerror = () => { avatar.textContent = (firstName || "М").slice(0,1); };
        }
    }
    document.querySelector("#title").textContent = {
        home:firstName ? greeting(firstName) : "Публикации",
        chat:"Ассистент", profile:"Профиль"
    }[tab];
    document.querySelector("#company").textContent = {home:"Маркетолог",chat:"Помощник по контенту",profile:"Аккаунт"}[tab];
    document.querySelector("#company").hidden = false;
    const profileCount = document.querySelector("#profile-count");
    profileCount.hidden = tab !== "profile";
    const count = state.profiles.length;
    const countForm = new Intl.PluralRules("ru").select(count);
    profileCount.textContent = count
        ? count + " " + {one:"профиль",few:"профиля",many:"профилей",other:"профиля"}[countForm] + " в пространстве"
        : "Добавьте первую компанию";
    const app = document.querySelector(".app");
    app.dataset.tab = tab;
    const mark = document.querySelector("#assistant-mark");
    mark.hidden = tab !== "chat";
    page.dataset.tab = tab;
    screen.dataset.screen = tab;
    tabs.hidden = false;
    for (const item of tabs.querySelectorAll("button")) {
        item.hidden = item.dataset.tab !== "profile" && !company;
        if (item.dataset.tab === tab) item.setAttribute("aria-current", "page");
        else item.removeAttribute("aria-current");
    }
    screen.replaceChildren();
    if (tab === "home") home();
    else if (tab === "chat") chat();
    else profile(company);
}

function section(title, parent = screen, symbol, count) {
    const node = el("section");
    const heading = el("div", undefined, "section-heading");
    if (symbol) {
        const badge = el("span", undefined, "section-icon");
        badge.append(icon(symbol));
        heading.append(badge);
    }
    heading.append(el("h2", title));
    if (count) heading.append(el("span", String(count), "section-count"));
    node.append(heading);
    parent.append(node);
    return node;
}

const storyViewer = document.querySelector("#storyViewer");
const storiesSeen = new Set();
let storyIndex = 0;
let storyTimer;

function moveStory(delta) {
    const next = storyIndex + delta;
    if (next >= state.stories.length) storyViewer.close();
    else if (next >= 0) showStory(next);
}

function showStory(index) {
    clearTimeout(storyTimer);
    const story = state.stories[index];
    storyIndex = index;
    storiesSeen.add(index);
    const rings = screen.querySelectorAll(".story");
    rings[index]?.classList.add("is-seen");
    const image = story.image || "/miniapp/glass.webp";
    const media = storyViewer.querySelector(".viewer-media img");
    const thumb = storyViewer.querySelector(".sv-thumb");
    media.src = image;
    thumb.src = image;
    storyViewer.querySelector(".sv-brand b").textContent = "Маркетолог";
    storyViewer.querySelector(".sv-title").textContent = story.title;
    storyViewer.querySelector(".sv-text").textContent = story.text;
    storyViewer.querySelector(".sv-zone.prev").disabled = index === 0;
    storyViewer.querySelector(".sv-bars").replaceChildren(...state.stories.map((_, position) => {
        const bar = el("span",undefined,"sv-bar");
        if (position < index) bar.classList.add("is-complete");
        if (position === index) bar.classList.add("is-current");
        bar.append(el("i"));
        return bar;
    }));
    if (!storyViewer.open) storyViewer.showModal();
    storyTimer = setTimeout(() => moveStory(1), 9000);
}

storyViewer.querySelector(".sv-close").addEventListener("click", () => storyViewer.close());
storyViewer.querySelector(".sv-zone.prev").addEventListener("click", () => moveStory(-1));
storyViewer.querySelector(".sv-zone.next").addEventListener("click", () => moveStory(1));
storyViewer.addEventListener("contextmenu", event => event.preventDefault());
storyViewer.addEventListener("close", () => clearTimeout(storyTimer));
storyViewer.addEventListener("keydown", event => {
    if (event.key === "ArrowLeft") moveStory(-1);
    if (event.key === "ArrowRight") moveStory(1);
});

const reviewStatuses = ["pending_approval", "draft", "failed", "approved"];

function home() {
    const company = state.profiles.find(item => item.id === state.active);
    if (state.stories.length) {
        const stories = el("div", undefined, "stories");
        stories.setAttribute("aria-label", "Истории");
        for (const [index, story] of state.stories.entries()) {
            const item = button("", () => showStory(index), "story");
            item.classList.toggle("is-seen", storiesSeen.has(index));
            const ring = el("span", undefined, "story-ring");
            if (story.image) {
                const image = el("img", undefined, "story-img");
                image.src = story.image;
                image.alt = "";
                ring.append(image);
            } else ring.append(icon("sparkles"));
            item.append(ring, el("span", story.title, "story-name"));
            stories.append(item);
        }
        screen.append(stories);
    }
    const upcoming = state.posts.filter(post => post.status === "scheduled").sort((a, b) => a.when.localeCompare(b.when));
    const drafts = state.posts.filter(post => reviewStatuses.includes(post.status));
    const radar = section("Радар", screen);
    radar.className = "apple-section";
    radar.dataset.odId = "radar";
    radar.firstChild.classList.add("section-head");
    const timeZone = state.timezone;
    const day = date => date.toLocaleDateString("ru-RU", {timeZone});
    const today = day(new Date());
    const scheduledToday = upcoming.filter(post => day(new Date(post.when)) === today).length;
    radar.firstChild.append(el("span", "сегодня · " + scheduledToday + " публикаций", "section-note"));
    const schedule = el("div", undefined, "radar");
    if (!upcoming.length) schedule.append(el("p", "Запланированных публикаций пока нет.", "muted"));
    for (const post of upcoming) {
        const row = el("article", undefined, "post-row");
        const date = new Date(post.when);
        const clock = el("div", undefined, "post-time");
        clock.append(el("strong", date.toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit",timeZone})),
            el("span", day(date) === today ? "сегодня" : day(date)));
        const card = button("", () => postSheet(post), "radar-card");
        const thumbnail = postThumbnail(post);
        if (thumbnail) card.append(thumbnail);
        const copy = el("span", undefined, "radar-copy");
        const meta = el("span", undefined, "post-meta");
        meta.append(icon("send"), el("span",company?.name),el("i",undefined,"dot"),
            el("span", platformTitle[post.platform] || "Telegram"));
        copy.append(meta, el("span",post.topic || "Пост №" + post.id,"post-title"));
        card.append(copy);
        row.append(clock, card);
        schedule.append(row);
    }
    radar.append(schedule);
    const offer = button("", offersSheet, "offer");
    offer.dataset.odId = "ai-offers";
    const offerIcon = el("span",undefined,"offer-icon");
    offerIcon.append(icon("sparkles"));
    const offerCopy = el("span");
    offerCopy.append(el("span","Новые предложения · " + drafts.length,"offer-eyebrow"),
        el("span",drafts.length ? "Ассистент подготовил черновики" : "Нет черновиков на согласование","offer-title"));
    const arrow = el("span",undefined,"offer-chevron");
    arrow.append(icon("chevron-right"));
    offer.append(offerIcon,offerCopy,arrow);
    radar.append(offer);
    if (state.proposal) {
        const proposal = state.proposal;
        const labels = {publish:"Опубликовать", schedule:"Запланировать", cancel:"Снять с расписания"};
        const request = section("Запрос из Telegram");
        request.append(el("p", labels[proposal.action] + " «" + proposal.topic + "»"));
        if (proposal.when) request.append(el("p", new Date(proposal.when).toLocaleString("ru-RU"), "muted"));
        request.append(button("Подтвердить действие", async () => {
            const result = await api("post", "POST", proposal);
            await load();
            if (result.simulated) feedback("Тестовая отправка. Пост в канал не ушёл.");
        }, "primary"));
    }
    const departments = section("Отделы");
    departments.className = "apple-section";
    departments.dataset.odId = "departments";
    departments.firstChild.classList.add("section-head");
    const hub = el("div", "Рабочее пространство", "hub-node");
    hub.prepend(el("i"));
    const branches = el("div", undefined, "hub-branches");
    const data = state.dashboard || {};
    for (const [label, symbol, action, value, detail, series] of [
        ["Аналитика","chart-no-axes-column", () => metricsSheet(), data.views_7d ?? 0,
            "просмотров за 7 дней", data.views_series],
        ["История","clock", publicationHistory,data.published ?? 0,"публикаций", data.publication_series]
    ]) {
        const tile = button("", action, "dept");
        const badge = el("span",undefined,"dept-icon");
        badge.append(icon(symbol));
        tile.append(badge,el("span",label,"dept-kicker"),
            el("span",value.toLocaleString("ru-RU"),"dept-value"),el("span",detail,"dept-sub"));
        if (series?.some(Boolean)) {
            const graph = el("span",undefined,"mini-bars");
            const peak = Math.max(...series);
            graph.setAttribute("aria-hidden","true");
            series.forEach(value => {
                const bar = el("i");
                bar.style.height = (value ? Math.round(100 * value / peak) : 0) + "%";
                if (value === peak) bar.className = "peak";
                graph.append(bar);
            });
            tile.append(graph);
        }
        branches.append(tile);
    }
    departments.append(hub, el("div", undefined,"hub-stem"), branches);
    if (state.plans?.length) showPlans();
}

function appendPostText(container, text) {
    const tokens = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()]+)|\*\*([^*\n]+)\*\*|_([^_\n]+)_/g;
    let end = 0;
    for (const match of String(text || "").matchAll(tokens)) {
        container.append(document.createTextNode(text.slice(end, match.index)));
        if (match[1] || match[3]) {
            const url = match[2] || match[3].replace(/[.,!?:;]+$/, "");
            const link = el("a", match[1] || url);
            link.href = url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            container.append(link);
            if (match[3]) container.append(document.createTextNode(match[3].slice(url.length)));
        } else {
            container.append(el(match[4] ? "strong" : "em", match[4] || match[5]));
        }
        end = match.index + match[0].length;
    }
    container.append(document.createTextNode((text || "").slice(end)));
}

const platformTitle = {telegram:"Telegram", vk:"VK", max:"MAX"};
function postMedia(post) {
    const revision = post.revision ? "&revision=" + post.revision : "";
    const base = "preview?id=" + post.id + revision;
    const version = post.version ? "&v=" + encodeURIComponent(post.version) : "";
    if (post.media) return post.media.map((item, index) => ({...item, key:String(index), url:base + "&media=" + index + version}));
    return post.has_image ? [{type:"image", name:"Фото", key:"0", url:base + version}] : [];
}

const postImages = new Map();

function loadMedia(node, item) {
    function display(blob) {
        const url = URL.createObjectURL(blob);
        if (item.type === "video") videoUrls.set(node, url);
        else {
            node.addEventListener("load", () => URL.revokeObjectURL(url), {once:true});
            node.addEventListener("error", () => URL.revokeObjectURL(url), {once:true});
        }
        node.src = url;
    }
    if (item.file) {
        display(item.file);
        return;
    }
    const key = state.active + ":" + item.url;
    let source = item.type === "image" ? postImages.get(key) : null;
    if (!source || source.expires <= Date.now()) {
        const blob = fetch("/miniapp/api/" + item.url, {headers:{Authorization:"Telegram " + initData}})
            .then(response => response.ok ? response.blob() : null)
            .catch(() => null);
        source = {blob,expires:Date.now() + 30000};
        if (item.type === "image") {
            postImages.set(key,source);
            if (postImages.size > 24) postImages.delete(postImages.keys().next().value);
            blob.then(image => { if ((!image || image.size > 2_000_000) && postImages.get(key) === source) postImages.delete(key); });
        }
    }
    source.blob
        .then(blob => {
            if (blob && node.isConnected) display(blob);
        }).catch(() => {});
}

function postThumbnail(post) {
    const item = postMedia(post).find(media => media.type === "image");
    if (!item) return null;
    const image = el("img", undefined, "post-thumb");
    image.alt = "";
    loadMedia(image, item);
    return image;
}

function telegramPost(post, attachments = postMedia(post), remove = null) {
    const preview = el("div", undefined, "tg-preview");
    const bubble = el("div", undefined, "tg-bubble");
    const channel = el("div", undefined, "tg-channel");
    const company = state.profiles.find(item => item.id === state.active);
    channel.append(el("strong", post.channel_title || company?.channel || company?.name, "tg-channel-name"),
        el("span", platformTitle[post.platform] || "Telegram", "tg-platform"));
    bubble.append(channel);
    if (attachments.length) {
        const group = el("div", undefined, "tg-media");
        group.dataset.count = attachments.length > 3 ? "many" : String(attachments.length);
        attachments.forEach((item, index) => {
            const cell = el("div", undefined, "tg-media-cell");
            if (item.type === "document") {
                cell.append(el("p","PDF · " + item.name,"tg-document"));
            } else {
                const media = el(item.type === "video" ? "video" : "img");
                if (item.type === "video") {
                    media.controls = true;
                    media.playsInline = true;
                    media.preload = "metadata";
                    media.setAttribute("aria-label", item.name);
                } else media.alt = item.name;
                loadMedia(media, item);
                cell.append(media);
            }
            if (remove) {
                const cross = button("×", () => remove(index), "tg-media-remove");
                cross.setAttribute("aria-label", "Убрать " + item.name);
                cell.append(cross);
            }
            group.append(cell);
        });
        bubble.append(group);
    }
    if (post.text) {
        const copy = el("p", undefined, "tg-copy");
        appendPostText(copy, post.text);
        bubble.append(copy);
    }
    bubble.append(el("p", "Предпросмотр", "tg-meta"));
    preview.append(bubble);
    return preview;
}

function draftActions(post, options = {}) {
    const actions = el("div", undefined, "review-actions");
    const when = options.when || post.suggested_at;
    const scheduled = Boolean(when);
    let date;
    const company = state.active;
    async function decide(action) {
        let outcome;
        try {
            const planned = date?.value ? new Date(date.value).toISOString() : when;
            if (scheduled && action === "approve" && new Date(planned) <= new Date()) {
                throw new Error("Время публикации прошло. Укажите новую дату.");
            }
            outcome = await api("post","POST",{
                company,id:post.id,version:post.version,action,
                ...(action === "approve" && scheduled ? {when:planned} : {}),
            });
        } catch (error) {
            await load();
            if (options.refresh) options.refresh();
            throw error;
        }
        await load();
        if (options.refresh) options.refresh();
        if (outcome?.note && sheet.open) {
            sheetBody.prepend(el("p",outcome.note,"muted"));
        } else if (outcome?.note) {
            feedback(outcome.note);
        } else if (action === "approve" && !sheet.open) {
            feedback(scheduled ? "Публикация запланирована." : "Пост опубликован.");
        }
    }
    actions.append(button("Отклонить",() => decide("reject"),"plain"),
        button("Редактировать",() => editPost(post,options.refresh),"plain"),
        button("Принять",() => decide("approve"),"primary"));
    if (scheduled) {
        const hint = el("p","Публикация · " + historyTime(when),"draft-schedule-hint");
        actions.prepend(hint);
        if (new Date(when) <= new Date()) {
            date = el("input");
            date.type = "datetime-local";
            date.className = "draft-schedule-hint";
            date.setAttribute("aria-label","Новая дата публикации");
            actions.prepend(date);
        }
    }
    return actions;
}

function offersSheet() {
    const body = el("section");
    const drafts = state.posts.filter(post => reviewStatuses.includes(post.status));
    if (!drafts.length) body.append(el("p", "Пока нет черновиков на согласование.", "muted"));
    for (const post of drafts) {
        const card = el("article", undefined, "review-card");
        card.append(telegramPost(post),draftActions(post,{refresh:offersSheet}));
        body.append(card);
    }
    showSheet("Новые предложения", body);
}

function editPost(post, back = () => sheet.close()) {
    const company = state.profiles.find(item => item.id === state.active);
    const body = el("section");
    const host = el("div");
    const field = el("label", "Текст поста");
    const input = el("textarea");
    input.value = post.text;
    input.maxLength = post.platform === "vk" ? 15000 : 4096;
    field.append(input);
    const attachments = postMedia(post);
    let changed = false;
    function redraw() {
        host.replaceChildren(telegramPost({...post,text:input.value}, attachments, company.editable ? index => {
            attachments.splice(index, 1);
            changed = true;
            redraw();
        } : null));
        if (company.editable && attachments.length > 1) showOrder();
        else order.replaceChildren();
    }
    let copyTimer;
    input.addEventListener("input", () => {
        clearTimeout(copyTimer);
        copyTimer = setTimeout(() => {
            let copy = host.querySelector(".tg-copy");
            if (!copy) {
                copy = el("p", undefined, "tg-copy");
                host.querySelector(".tg-meta").before(copy);
            }
            copy.replaceChildren();
            appendPostText(copy, input.value);
        }, 60);
    });
    const toolbar = el("div", undefined, "row");
    for (const [label, mark] of [["Жирный","**"],["Курсив","_"]]) {
        toolbar.append(button(label, () => {
            const start = input.selectionStart;
            const end = input.selectionEnd;
            input.setRangeText(mark + (input.value.slice(start,end) || "текст") + mark,start,end,"end");
            input.dispatchEvent(new Event("input"));
            input.focus();
        }, "plain"));
    }
    const linkPanel = el("div", undefined, "row");
    linkPanel.hidden = true;
    const linkAddress = el("input");
    linkAddress.type = "url";
    linkAddress.placeholder = "https://…";
    linkAddress.setAttribute("aria-label", "Адрес ссылки");
    let selection;
    toolbar.append(button("Ссылка", () => {
        selection = [input.selectionStart, input.selectionEnd];
        linkPanel.hidden = false;
        linkAddress.focus();
    }, "plain"));
    linkPanel.append(linkAddress, button("Вставить", () => {
        const url = new URL(linkAddress.value);
        if (!["http:","https:"].includes(url.protocol)) throw new Error("Укажите ссылку http или https.");
        const label = input.value.slice(...selection).replace(/[\]\n]/g," ") || "Ссылка";
        input.setRangeText("[" + label + "](" + url.href.replace(/[()]/g,encodeURIComponent) + ")",...selection,"end");
        input.dispatchEvent(new Event("input"));
        linkPanel.hidden = true;
        input.focus();
    }));
    body.append(host, field, toolbar, linkPanel);
    const order = el("div", undefined, "attachment-order");
    body.append(order);
    function showOrder() {
        order.replaceChildren();
        attachments.forEach((item, index) => {
            const row = el("div", undefined, "list-item");
            function move(delta) {
                const other = attachments[index + delta];
                attachments[index + delta] = item;
                attachments[index] = other;
                changed = true;
                redraw();
            }
            const up = button("↑", () => move(-1), "plain");
            const down = button("↓", () => move(1), "plain");
            up.setAttribute("aria-label","Поднять " + item.name);
            down.setAttribute("aria-label","Опустить " + item.name);
            up.disabled = !index;
            down.disabled = index === attachments.length - 1;
            const controls = el("div", undefined, "row");
            controls.append(up,down);
            row.append(el("span",item.name),controls);
            order.append(row);
        });
    }
    if (company.editable) {
        const fileLabel = el("label", "Добавить фото или видео");
        const fileInput = el("input");
        fileInput.type = "file";
        fileInput.multiple = true;
        fileInput.accept = "image/jpeg,image/png,image/webp,video/mp4";
        fileLabel.append(fileInput);
        body.append(fileLabel);
        fileInput.addEventListener("change", () => guarded(fileInput, () => {
            const files = Array.from(fileInput.files);
            fileInput.value = "";
            if (files.length + attachments.length > 10) throw new Error("До 10 фото и видео на пост.");
            for (const file of files) {
                const video = /\.mp4$/i.test(file.name);
                if (!file.size || file.size > (video ? 50_000_000 : 20 * 1024 * 1024)) {
                    throw new Error("Фото до 20 МБ, видео MP4 до 50 МБ.");
                }
            }
            for (const file of files) attachments.push({
                file,name:file.name,type:/\.mp4$/i.test(file.name) ? "video" : "image"
            });
            changed = true;
            redraw();
        }));
    }
    const actions = el("div", undefined, "row");
    actions.append(button("Отмена", back, "plain"), button("Сохранить", async () => {
        if (!input.value.trim() && !attachments.length) throw new Error("Добавьте текст или вложение.");
        const payload = {company:company.id,id:post.id,version:post.version,action:"edit_text",text:input.value};
        if (changed) {
            payload.media = [];
            for (const item of attachments) {
                if (item.file && !item.key) item.key = (await api("attachment?company=" + company.id,"PUT",item.file)).id;
                payload.media.push(item.key);
            }
        }
        await api("post", "POST", payload);
        await load();
        back();
    }, "primary"));
    body.append(actions);
    showSheet("Редактировать пост", body, "", back);
    redraw();
}

function publicationHistory(filter = "all") {
    return loadSheet("История публикаций","list",() => cachedApi("history"),
        ({posts}) => renderPublicationHistory(posts,filter));
}

function renderPublicationHistory(posts, filter) {
    const body = el("section");
    const filters = el("div", undefined, "filters");
    const platforms = [...new Set(posts.map(post => post.platform))];
    for (const platform of ["all", ...platforms]) {
        const chip = button(platform === "all" ? "Все" : platformTitle[platform] || platform,
            () => publicationHistory(platform), platform === filter ? "is-active" : undefined);
        chip.setAttribute("aria-pressed", String(platform === filter));
        filters.append(chip);
    }
    body.append(filters);
    const filtered = posts.filter(post => filter === "all" || post.platform === filter);
    if (!filtered.length) body.append(el("p", "История пока пуста.", "muted"));
    for (const post of filtered) {
        const row = publicationRow(post, () => versionSheet(post.id, () => publicationHistory(filter)));
        body.append(row);
    }
    body.append(button("Пул постов", () => poolSheet(), "text-button"));
    showSheet("История публикаций", body);
}

function publicationRow(post, action) {
    const row = button("", action, "publication-row");
    const image = postThumbnail(post);
    if (image) row.append(image);
    const copy = el("span");
    copy.append(el("strong", post.topic || "Пост №" + post.id), el("small",
        "№" + post.id + " · " + (platformTitle[post.platform] || "Telegram") +
        (post.published_at ? " · " + new Date(post.published_at).toLocaleDateString("ru-RU") : "")));
    row.append(copy);
    return row;
}

function metricsSheet(params = {hours:24}, posts = null) {
    return loadSheet("Аналитика","analytics",async () => {
        const publications = posts ?? (await cachedApi("history")).posts.filter(post => post.status === "published" && !post.simulated);
        const filtered = publications.filter(post => !params.platform || params.platform === "all" || post.platform === params.platform);
        const selected = filtered.find(post => post.id === Number(params.post)) || filtered[0];
        const query = new URLSearchParams({hours:params.hours});
        if (params.platform) query.set("platform",params.platform);
        if (selected) query.set("post",selected.id);
        const data = await cachedApi("metrics?" + query);
        return {data,selected,publications};
    }, ({data,selected,publications}) => renderMetrics(params,publications,selected,data),
    {subtitle:"Просмотры публикации"});
}

function renderMetrics(params, posts, selected, data) {
    const body = el("section", undefined, "analytics-sheet");
    const company = state.profiles.find(item => item.id === state.active);
    if (company?.vk_channel || posts.some(post => post.platform === "vk")) {
        const filters = el("div",undefined,"filters");
        filters.setAttribute("aria-label","Соцсеть");
        for (const [platform,label] of [["all","Все"],["telegram","Telegram"],["vk","ВКонтакте"]]) {
            const active = (params.platform || "all") === platform;
            const choice = button(label,() => metricsSheet({hours:params.hours,platform},posts),active ? "is-active" : undefined);
            choice.setAttribute("aria-pressed",String(active));
            filters.append(choice);
        }
        body.append(filters);
    }
    const preview = el("div");
    preview.tabIndex = -1;
    if (selected) {
        preview.setAttribute("aria-label", "Предпросмотр публикации");
        preview.append(telegramPost(selected));
        body.append(preview);
    }
    const periods = el("div", undefined, "filters analytics-periods");
    periods.setAttribute("role", "group");
    periods.setAttribute("aria-label", "Период");
    for (const [hours, label] of [[24,"За сутки"],[168,"За 7 дней"],[720,"За 30 дней"]]) {
        const active = Number(params.hours) === hours && !params.from;
        const chip = button(label, () => metricsSheet({...params,hours,post:selected?.id},posts), active ? "is-active" : undefined);
        chip.setAttribute("aria-pressed",String(active));
        periods.append(chip);
    }
    body.append(periods, viewBars(data, params.hours));
    if (selected?.platform === "vk" && !company?.vk_business_account) {
        body.append(el("p","Просмотры записей недоступны с ключом сообщества ВКонтакте.","muted"));
    }
    if (selected?.platform === "vk" || params.platform === "vk") {
        const channel = data.channels?.find(item => item.platform === "vk");
        const subscribers = channel?.points.at(-1)?.[1];
        if (subscribers != null) {
            body.append(el("p","Подписчики ВКонтакте · " + subscribers.toLocaleString("ru-RU"),"muted"));
        }
    }
    const companyId = state.active;
    const critiqueKey = companyId + ":" + selected?.id;
    const panel = el("section", undefined, "critique-panel");
    const heading = el("div", undefined, "row");
    heading.append(el("h3", "Разбор поста"));
    function showCritique(result) {
        panel.querySelector(".critique-result")?.remove();
        const details = el("div", undefined, "critique-result");
        const analysis = el("dl");
        analysis.append(el("dt", "Что могло повлиять"), el("dd",result.hypothesis),
            el("dt", "Что проверить дальше"), el("dd",result.next));
        details.append(el("p", result.facts, "critique-facts"), analysis);
        panel.append(details);
    }
    panel.append(heading);
    if (data.critique || critiqueResults.has(critiqueKey)) showCritique(data.critique || critiqueResults.get(critiqueKey));
    else {
        const ask = button("Разобрать пост", async () => {
            ask.textContent = "Готовим разбор…";
            let request = critiqueRequests.get(critiqueKey);
            if (!request) {
                request = api("critique","POST",{company:companyId,id:selected.id}).then(({critique}) => {
                    critiqueResults.set(critiqueKey,critique);
                    return critique;
                }).finally(() => critiqueRequests.delete(critiqueKey));
                critiqueRequests.set(critiqueKey,request);
            }
            try { showCritique(await request); ask.remove(); }
            finally { ask.textContent = "Разобрать пост"; }
        }, "text-button");
        ask.disabled = !data.can_critique;
        heading.append(ask);
        const running = critiqueRequests.get(critiqueKey);
        if (running) {
            ask.disabled = true;
            ask.textContent = "Готовим разбор…";
            running.then(result => { showCritique(result); ask.remove(); }).catch(() => {
                ask.disabled = false;
                ask.textContent = "Разобрать пост";
            });
        }
        if (!data.can_critique) panel.append(el("p", selected
            ? "Разбор станет доступен после первого замера просмотров."
            : "Опубликуйте пост, чтобы появился разбор.", "critique-facts"));
    }
    body.append(panel);
    const publications = el("section", undefined, "analytics-publications");
    publications.append(el("h3", "Публикации"));
    if (!posts.length) publications.append(el("p", "Пока нет опубликованных постов.", "muted"));
    for (const post of posts.filter(post => !params.platform || params.platform === "all" || post.platform === params.platform)) {
        const row = publicationRow(post, () => metricsSheet({...params,post:post.id},posts));
        row.setAttribute("aria-pressed", String(post.id === selected.id));
        publications.append(row);
    }
    const actions = el("div", undefined, "analytics-actions");
    actions.append(button("Готово", () => sheet.close(), "primary"));
    body.append(publications,actions);
    showSheet("Аналитика", body, "Просмотры публикации");
    if (params.post) preview.focus({preventScroll:true});
}

function viewBars(data, hours) {
    const box = el("section", undefined, "views-block");
    const count = hours === 24 ? 24 : hours === 168 ? 7 : 30;
    const end = Date.parse(data.end);
    const start = Date.parse(data.start);
    const width = (end - start) / count;
    const buckets = Array(count).fill(0);
    let previous = data.baseline;
    for (const point of data.points) {
        if (point.views == null || point.deleted) continue;
        if (previous && Date.parse(point.at) - Date.parse(previous.at) === 3600000) {
            const index = Math.min(count - 1, Math.max(0, Math.ceil((Date.parse(point.at) - start) / width) - 1));
            buckets[index] += Math.max(0, point.views - previous.views);
        }
        previous = point;
    }
    const peak = Math.max(...buckets);
    const total = buckets.reduce((sum,value) => sum + value,0);
    const period = hours === 24 ? "24 часа" : hours === 168 ? "7 дней" : "30 дней";
    box.append(el("strong", total.toLocaleString("ru-RU"),"views-count"),
        el("p","просмотров за последние " + period,"views-caption"));
    const graph = el("div", undefined, "view-bars");
    graph.dataset.count = count;
    graph.setAttribute("role","img");
    graph.setAttribute("aria-label", "Просмотры по интервалам, от ранних к поздним: " + buckets.join(", "));
    const peakIndex = peak > 0 ? buckets.indexOf(peak) : -1;
    for (const [index,value] of buckets.entries()) {
        const column = el("div", undefined, "view-column");
        const number = count - index;
        const label = hours === 24 ? String(number) : number === 1 ? "Сегодня" : String(number - 1);
        const bar = el("span", undefined, "view-bar" + (index === peakIndex ? " peak" : ""));
        bar.style.height = value ? Math.round(value / peak * 82) + "%" : "2px";
        column.title = label + " · " + value + " просмотров";
        column.append(bar, el("small", count !== 30 || [30,26,21,16,11,6,1].includes(number) ? label : "\u00a0"));
        graph.append(column);
    }
    const legend = el("p", undefined, "views-legend");
    legend.append(el("span", hours === 24 ? "Часов назад" : "Дней назад"));
    const maximum = el("span", (hours === 24 ? "Пик за час" : "Пик за день") + " · " + peak.toLocaleString("ru-RU"), "views-peak");
    maximum.classList.toggle("has-peak", peak > 0);
    legend.append(maximum);
    box.append(graph, legend);
    return box;
}


function poolSheet(before = 0) {
    return loadSheet("Пул постов","list",() => cachedApi("post-pool?before=" + before),
        pool => renderPool(pool,before));
}

function renderPool(pool, before) {
    const body = el("section");
    if (!pool.posts.length) body.append(el("p", "Постов пока нет."));
    const statusTitle = {draft:"Черновик", pending_approval:"На согласовании", approved:"Утверждён",
        scheduled:"Запланирован", failed:"Ошибка публикации", published:"Опубликован", rejected:"Отклонён"};
    for (const post of pool.posts) {
        const card = el("article", undefined, "card");
        card.append(el("h3", "№" + post.id + " · " + post.topic),
            el("p", statusTitle[post.status] || post.status, "muted"),
            button("Версии", () => versionSheet(post.id, () => poolSheet(before))));
        body.append(card);
    }
    if (pool.next) body.append(button("Более ранние посты", () => poolSheet(pool.next)));
    if (before) body.append(button("К последним постам", () => poolSheet()));
    body.append(button("К истории публикаций", () => publicationHistory(), "text-button"));
    showSheet("Пул постов", body);
}

function versionSheet(postId, back) {
    return loadSheet("История поста","list",() => cachedApi("versions?id=" + postId),
        data => renderVersion(data,postId,back),{back});
}

function renderVersion(data, postId, back) {
    const body = el("section");
    const table = el("table", undefined, "post-timeline");
    const columns = el("tr");
    for (const title of ["Время","Тип действия","Данные"]) {
        const column = el("th",title);
        column.scope = "col";
        columns.append(column);
    }
    const head = el("thead");
    head.append(columns);
    const entries = el("tbody");
    const events = {
        request:"Запрос", revision:"Правка", chat:"Черновик", scheduler:"Автономный черновик",
        plan:"Контент-план", manual:"Ручной черновик", photo:"Вложение", legacy:"Черновик",
        post_scheduled:"Подтверждено", post_published:"Опубликовано", post_rejected:"Отклонено",
        post_failed:"Ошибка публикации", post_unscheduled:"Снято с расписания", simulation:"Тестовая отправка",
    };
    for (const event of data.timeline || []) {
        const row = el("tr");
        let detail = event.data || "";
        if (event.type === "post_scheduled" && detail) {
            const when = new Date(detail + "Z");
            if (Number.isFinite(+when)) detail = "Запланировано на " + historyTime(when, false);
        }
        row.append(el("td",historyTime(event.at)),el("td",events[event.type] || event.type),el("td",detail));
        entries.append(row);
    }
    table.append(head,entries);
    if (entries.childElementCount) body.append(table);
    else body.append(el("p","История действий пуста.","muted"));
    if (data.plan_item_id) body.append(el("p", "Пункт контент-плана №" + data.plan_item_id));
    const sources = {
        chat: "Запрос в чате", scheduler: "Автономный планировщик", plan: "Контент-план",
        manual: "Правка в Mini App", photo: "Фотобанк", legacy: "Источник неизвестен",
    };
    const versions = el("details",undefined,"history-versions");
    versions.append(el("summary","Версии поста"));
    for (const version of [...data.versions].reverse()) {
        const card = el("article", undefined, "card");
        card.append(el("h3", "Версия №" + version.id),
            el("p", sources[version.source] || version.source, "muted"),
            el("p", new Date(version.at).toLocaleString("ru-RU"), "muted"));
        if (version.parent_id) card.append(el("p", "На основе версии №" + version.parent_id));
        card.append(telegramPost({
            id:postId, revision:version.id, text:version.text, media:version.media,
            has_image:version.has_image, platform:data.platform,
        }));
        if (version.image_model) card.append(el("p", "Модель изображения · " + version.image_model));
        if (version.image_prompt) {
            const details = el("details");
            details.append(el("summary", "Промпт генерации"), el("p", version.image_prompt, "post-body"));
            card.append(details);
        }
        versions.append(card);
    }
    if (data.versions.length) body.append(versions);
    const company = state.profiles.find(item => item.id === state.active);
    const post = data.post;
    if (company?.vk_channel && post?.platform === "telegram") {
        body.append(button("Копия для ВКонтакте", () => copyToVk(post), "text-button"));
    }
    if (company?.editable && post?.editable_live) {
        body.append(button("Изменить запись во ВКонтакте", () => editLiveVk(post, () => versionSheet(postId,back)), "text-button"));
    }
    showSheet(data.topic || "История поста", body, "", back);
}

function historyTime(value, relative = true) {
    const date = new Date(value);
    const timeZone = state.timezone;
    const calendar = new Intl.DateTimeFormat("ru-RU",{timeZone,day:"2-digit",month:"2-digit",year:"numeric"});
    const clock = new Intl.DateTimeFormat("ru-RU",{timeZone,hour:"2-digit",minute:"2-digit"});
    const now = new Date();
    const day = calendar.format(date);
    if (relative && day === calendar.format(now)) return "Сегодня " + clock.format(date);
    if (relative && day === calendar.format(new Date(+now - 86400000))) return "Вчера " + clock.format(date);
    return day + " · " + clock.format(date);
}

function showPlans() {
    const plans = section("Контент-планы", screen, "calendar-days");
    const list = el("div", undefined, "list");
    for (const plan of state.plans) {
        const row = el("div", undefined, "list-item");
        const label = el("span", plan.title);
        const approved = plan.status === "approved";
        label.append(el("small", approved ? "Утверждён" : "На согласовании"));
        const open = button("Открыть план", () => {
            const body = el("section");
            const dates = el("ol", undefined, "plan-dates");
            const statuses = {drafted:"Черновик готов", published:"Опубликовано", skipped:"Отклонено"};
            for (const item of plan.items) {
                const entry = el("li");
                const date = item.when ? new Date(item.when).toLocaleDateString("ru-RU") : "";
                entry.append(el("strong", item.topic),
                    el("p", [date, item.rubric, statuses[item.status]].filter(Boolean).join(" · "), "muted"));
                dates.append(entry);
            }
            body.append(dates);
            const company = state.profiles.find((item) => item.id === state.active);
            if (company?.editable) {
                const actions = el("div", undefined, "row");
                const changePlan = async (action, fields = {}) => {
                    await api("plan", "POST", {
                        company: company.id, id: plan.id, version: plan.version, action, ...fields,
                    });
                    sheet.close();
                    await load();
                };
                if (!approved) {
                    actions.append(button("Утвердить план", () => changePlan("approve"), "primary"));
                    actions.append(button("Изменить темы", () => editPlan(plan, changePlan)));
                }
                actions.append(button("В архив", () => {
                    if (window.confirm("Отправить план в архив? Готовые черновики сохранятся.")) {
                        return changePlan("archive");
                    }
                }, "danger"));
                body.append(actions);
            }
            showSheet(plan.title, body);
        }, "text-button");
        row.append(label, open);
        list.append(row);
    }
    plans.append(list);
}

function editPlan(plan, changePlan) {
    const form = el("form");
    const topics = plan.items.map((item, index) => {
        const date = item.when ? new Date(item.when).toLocaleDateString("ru-RU") : "";
        const label = el("label", date ? "Тема · " + date : "Тема " + (index + 1));
        const input = el("input");
        input.value = item.topic;
        input.maxLength = 240;
        input.required = true;
        label.append(input);
        form.append(label);
        return input;
    });
    const save = el("button", "Сохранить темы", "primary");
    form.append(save);
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const items = topics.map((input, index) => ({
            topic: input.value.trim(), rubric: plan.items[index].rubric,
        }));
        guarded(save, () => changePlan("revise", {items}));
    });
    showSheet("Изменить темы", form);
}

async function postAction(post, action, fields = {}) {
    const result = await api("post", "POST", {id: post.id, company: state.active, action, version: post.version, ...fields});
    sheet.close();
    await load();
    if (result.simulated) feedback("Тестовая отправка. Пост в канал не ушёл.");
    else if (result.note) feedback(result.note);
}

async function copyToVk(post) {
    await postAction(post,"copy_to_vk");
    feedback("Копия для ВКонтакте ждёт согласования.");
}

function editLiveVk(post, back) {
    const form = el("form");
    const field = el("label","Текст опубликованной записи");
    const input = el("textarea");
    input.value = post.text;
    input.required = true;
    input.maxLength = 15000;
    field.append(input);
    const save = el("button","Сохранить во ВКонтакте","primary");
    form.append(field,save);
    form.addEventListener("submit",event => {
        event.preventDefault();
        guarded(save,async () => {
            await api("post","POST",{company:state.active,id:post.id,version:post.version,action:"edit_published",text:input.value});
            await back();
        });
    });
    showSheet("Редактировать запись",form,"",back);
}

function postSheet(post) {
    const content = el("section");
    content.append(telegramPost(post));
    if (post.status === "scheduled") {
        const actions = el("div", undefined, "row");
        actions.append(button("Снять с расписания", () => postAction(post, "cancel"), "danger"),
            button("Редактировать", () => editPost(post)));
        content.append(actions);
        const when = el("input");
        when.type = "datetime-local";
        when.setAttribute("aria-label", "Дата и время публикации");
        const proposed = new Date(post.when || post.suggested_at || 0);
        if (proposed > new Date()) {
            const local = new Date(proposed.getTime() - proposed.getTimezoneOffset() * 60000);
            when.value = local.toISOString().slice(0, 16);
        }
        content.append(when, button("Утвердить и запланировать", () => {
            if (!when.value) throw new Error("Выберите дату и время.");
            return postAction(post, "schedule", {when: new Date(when.value).toISOString()});
        }, "primary"));
    } else content.append(draftActions(post,{refresh:() => sheet.close()}));
    if (post.platform === "telegram" && state.profiles.find(item => item.id === state.active)?.vk_channel) {
        content.append(button("Копия для ВКонтакте",() => copyToVk(post),"text-button"));
    }
    showSheet(post.topic || "Пост", content);
}

function profile(company) {
    const initials = name => name.split(/\s+/).slice(0,2).map(word => word[0]).join("").toUpperCase();
    const switcher = el("div", undefined, "profile-switcher");
    for (const item of state.profiles) {
        const chip = button(item.name, async () => {
            await api("select", "POST", {id:item.id});
            await load();
        }, "profile-chip");
        chip.setAttribute("aria-pressed", String(item.id === state.active));
        const avatar = el("span", initials(item.name), "profile-initials");
        avatar.setAttribute("aria-hidden", "true");
        chip.prepend(avatar);
        switcher.append(chip);
    }
    if (state.profiles.length) screen.append(switcher);
    else screen.append(el("p","Добавьте компанию, чтобы вести публикации и смотреть аналитику.","muted"));
    if (company) {
        const card = el("section", undefined, "company-card");
        const top = el("div", undefined, "company-top");
        const identity = el("div",undefined,"company-id");
        identity.append(el("h2",company.name,"company-name"));
        identity.append(el("p",company.channel || "Канал не подключён","company-handle"));
        const logo = el("span", initials(company.name), "company-logo");
        logo.setAttribute("aria-hidden", "true");
        top.append(logo,identity);
        if (company.editable) {
            const pencil = button("", () => profileForm(company), "icon-button");
            pencil.setAttribute("aria-label", "Редактировать профиль");
            pencil.append(icon("pencil"));
            top.append(pencil);
        }
        card.append(top);
        const stats = el("div",undefined,"company-stats");
        const dashboard = state.dashboard || {};
        for (const [value, label] of [[dashboard.subscribers,"подписчики"],
            [dashboard.published,"публикации"],[dashboard.views_7d,"просмотры за 7 дней"]]) {
            const stat = el("div",undefined,"stat");
            stat.append(el("b",(value ?? 0).toLocaleString("ru-RU")),el("span",label));
            stats.append(stat);
        }
        card.append(stats);
        if (company.details.description) card.append(el("p",company.details.description,"company-description"));
        const channels = el("div",undefined,"company-channels");
        if (company.channel) {
            const connection = el("div",undefined,"channel-line");
            const channelName = el("span","Telegram · " + company.channel);
            const status = el("span","Проверяем…","channel-state");
            status.setAttribute("role", "status");
            connection.append(icon("send"),channelName,status);
            channels.append(connection);
            cachedApi("channel-status?company=" + company.id, 20000).then(result => {
                if (result.label) {
                    identity.querySelector(".company-handle").textContent = result.label;
                    channelName.textContent = "Telegram · " + result.label;
                }
                status.textContent = result.message;
                status.dataset.status = result.ok ? "connected" : "error";
            }).catch(() => {
                status.textContent = "Не удалось проверить подключение";
                status.dataset.status = "error";
            });
        } else if (!company.vk_channel) {
            channels.append(el("p","Подключите канал, чтобы публиковать и видеть аналитику.","channels-empty"));
        }
        if (company.vk_channel) {
            const connection = el("div",undefined,"channel-line");
            connection.append(el("span","ВКонтакте · " + company.vk_channel),
                el("span","Подключено","channel-state"));
            channels.append(connection);
        }
        card.append(channels);
        screen.append(card);
    }
    const actions = el("div",undefined,"profile-actions");
    const add = button("Добавить профиль", () => profileForm(), "primary");
    add.prepend(icon("plus"));
    actions.append(add);
    if (company?.editable && !company.is_default) actions.append(button("Удалить текущий профиль", () => {
        const body = el("section");
        body.append(el("p","Профиль «" + company.name + "» и его материалы будут удалены."),
            button("Удалить профиль",async () => {
                await api("profile","POST",{company:company.id,action:"delete"});
                sheet.close();
                await load();
            },"danger"));
        showSheet("Удалить профиль?",body);
    },"plain danger"));
    screen.append(actions);
}

const profileFields = {
    name:"Название", description:"Описание компании", products:"Продукты и услуги",
    audience:"Аудитория", usp:"Что отличает компанию",
    visual_style:"Визуальный стиль", restrictions:"Ограничения", website:"Сайт компании", channel:"Telegram-канал",
};

const profileImageExtensions = new Set(
    "png apng jpg jpeg jfif webp gif bmp dib tif tiff heic heif avif ico icns jp2 j2k jpf jpx ppm pgm pbm pnm tga qoi psd svg".split(" ")
);
const profileFileLimit = 100_000_000;
const profileImageLimit = 5;
const profileDocumentExtensions = new Set(["pdf","docx","txt","md"]);
const fileExtension = name => name.includes(".") ? name.split(".").at(-1).toLowerCase() : "";

const fileSymbols = {
    image:"M3 3h18v18H3z M8 9h.01 M3 17l5-5 4 4 3-3 6 6",
    pdf:"M6 3h8l4 4v14H6z M14 3v5h4 M8 15h8",
    doc:"M6 3h8l4 4v14H6z M14 3v5h4 M8 11l1.5 7 2.5-5 2.5 5 1.5-7",
    text:"M6 3h8l4 4v14H6z M14 3v5h4 M8 12h8 M8 15h8 M8 18h5",
    md:"M4 6h16v12H4z M7 15V9l2.5 3L12 9v6 M15 12l2 2.5 2-2.5 M17 9.5v5",
};
const fileKinds = {pdf:"pdf", docx:"doc", doc:"doc", md:"md", txt:"text"};
const fileKindLabels = {pdf:"PDF", doc:"DOCX", md:"MD", text:"TXT"};
const documentPreviews = new Map();
const confirmFile = document.querySelector("#confirm-file");

async function confirmDocumentRemoval(name) {
    confirmFile.returnValue = "";
    confirmFile.querySelector("p").textContent = "Удалить «" + name + "» из профиля?";
    confirmFile.showModal();
    await new Promise(resolve => confirmFile.addEventListener("close",resolve,{once:true}));
    return confirmFile.returnValue === "delete";
}

function fileTile(item, company, remove, sourceType = "document") {
    const tile = el("figure",undefined,"profile-file");
    const square = el("div",undefined,"profile-file-square");
    const suffix = fileExtension(item.name);
    const isImage = sourceType === "content" || profileImageExtensions.has(suffix);
    const kind = isImage ? "image" : fileKinds[suffix] || "text";
    square.dataset.kind = kind;
    square.append(chatIcon(fileSymbols[kind]));
    if (kind !== "image") square.append(el("span",fileKindLabels[kind] || suffix.toUpperCase()));
    const close = button("",remove,"profile-file-remove");
    close.setAttribute("aria-label","Удалить " + item.name);
    close.append(chatIcon("M6 6l12 12M18 6L6 18"));
    tile.append(square,close,el("figcaption",item.name));
    if (item.preview || (item.file && isImage && item.file.size <= 12_000_000)) {
        const image = el("img");
        image.alt = "";
        if (item.file) {
            const url = URL.createObjectURL(item.file);
            image.onload = image.onerror = () => URL.revokeObjectURL(url);
            image.src = url;
            square.append(image);
        } else {
            const key = sourceType + ":" + company + ":" + item.id;
            let source = documentPreviews.get(key);
            if (!source) {
                source = fetch("/miniapp/api/" + sourceType + "?" + new URLSearchParams({company,id:item.id}), {
                    headers:{Authorization:"Telegram " + initData}, signal:AbortSignal.timeout(9000),
                }).then(response => {
                    if (!response.ok) throw new Error("Предпросмотр недоступен");
                    return response.blob();
                }).then(blob => URL.createObjectURL(blob)).catch(() => {
                    documentPreviews.delete(key);
                    return "";
                });
                documentPreviews.set(key,source);
                if (documentPreviews.size > 64) {
                    const oldest = documentPreviews.keys().next().value;
                    documentPreviews.get(oldest).then(url => { if (url) URL.revokeObjectURL(url); });
                    documentPreviews.delete(oldest);
                }
            }
            source.then(url => { if (url && tile.isConnected) { image.src = url; square.append(image); } });
        }
    }
    return tile;
}

function profileForm(company) {
    const form = el("form",undefined,"profile-form");
    const grid = el("div",undefined,"profile-files");
    const pending = new Map();
    let savedCompany = company?.id;
    let savedWebsite = company?.website || "";
    let available = company ? (state.document_bytes_available ?? profileFileLimit) : profileFileLimit;
    const fileError = el("p",undefined,"sheet-error");
    fileError.setAttribute("role","alert");
    fileError.hidden = true;
    const pendingBytes = () => [...pending.keys()].reduce((total,file) => total + file.size,0);
    function showFileError(message) {
        fileError.textContent = message;
        fileError.hidden = !message;
    }
    const profileCharLimit = state.profile_chars_limit ?? 2000;
    const textInputs = {};
    const fixedChars = (company?.details?.tone || "").length;  // тон общения в форме не редактируется
    const counter = el("p",undefined,"muted");
    // Лимит общий на все текстовые поля: фронт следит за суммой и не даёт ввести лишнее
    function updateCounter() {
        const inputs = Object.values(textInputs);
        const total = fixedChars + inputs.reduce((sum,input) => sum + input.value.length,0);
        for (const input of inputs) {
            input.maxLength = Math.max(input.value.length,profileCharLimit - (total - input.value.length));
        }
        counter.textContent = "Описание компании: " + total + " из " + profileCharLimit + " символов";
        counter.classList.toggle("sheet-error",total > profileCharLimit);
        const submitButton = form.querySelector("button.primary");
        if (submitButton) submitButton.disabled = total > profileCharLimit;
    }
    for (const [key, label] of Object.entries(profileFields)) {
        const field = el("label",label + (key === "name" ? " *" : ""));
        const input = el(["description","visual_style"].includes(key) ? "textarea" : "input");
        input.name = key;
        const value = key === "channel" ? company?.channel :
            key === "website" ? savedWebsite : company?.details[key];
        input.value = Array.isArray(value) ? value.join(", ") : value || "";
        input.maxLength = key === "website" ? 2048 : 2000;
        input.required = key === "name";
        if (key !== "website" && key !== "channel") {
            textInputs[key] = input;
            input.addEventListener("input",updateCounter);
        }
        input.readOnly = key === "name" && Boolean(company?.is_default);
        if (key === "website") {
            input.type = "url";
            input.placeholder = "https://example.ru";
        }
        if (key === "channel") input.placeholder = "@канал или ссылка t.me";
        field.append(input);
        form.append(field);
    }
    form.append(counter);
    form.append(el("p","Для публикации добавьте бота в администраторы канала.","muted"));
    if (state.profiles.some(profile => profile.vk_enabled) || company?.vk_channel) {
        const field = el("label","Сообщество ВКонтакте");
        const input = el("input");
        input.name = "vk_channel";
        input.value = company?.vk_id || "";
        input.placeholder = "vk.com/club123 или ID";
        field.append(input);
        form.append(field);
    }

    function addTile(item) {
        const tile = fileTile(item,savedCompany,async () => {
            if (!await confirmDocumentRemoval(item.name)) return;
            if (item.file) pending.delete(item.file);
            else {
                await api("document","POST",{company:savedCompany,id:item.id,action:"delete"});
                available += item.size || 0;
                if (state.active === savedCompany) {
                    state.documents = state.documents.filter(document => document.id !== item.id);
                    state.document_bytes_available = available;
                }
                const key = "document:" + savedCompany + ":" + item.id;
                documentPreviews.get(key)?.then(url => { if (url) URL.revokeObjectURL(url); });
                documentPreviews.delete(key);
            }
            tile.remove();
            showFileError("");
        });
        grid.append(tile);
        return tile;
    }
    if (company) for (const document of state.documents) addTile(document);

    const picker = el("input");
    picker.type = "file";
    picker.multiple = true;
    picker.hidden = true;
    picker.accept = ["image/*",...[...profileDocumentExtensions].map(suffix => "." + suffix),
        ...[...profileImageExtensions].map(suffix => "." + suffix)].join(",");
    picker.addEventListener("change",() => {
        const files = [...picker.files];
        picker.value = "";
        if (!files.length) return;
        if (files.some(file => !profileDocumentExtensions.has(fileExtension(file.name)) && !profileImageExtensions.has(fileExtension(file.name)))) {
            showFileError("Принимаются PDF, Word (.docx), текстовые файлы (.txt, .md) и изображения. Презентации экспортируйте в PDF.");
            return;
        }
        const imageNames = item => profileImageExtensions.has(fileExtension(item.name));
        const savedImages = (company ? state.documents : []).filter(imageNames).length;
        const pendingImages = [...pending.keys()].filter(imageNames).length;
        if (savedImages + pendingImages + files.filter(imageNames).length > profileImageLimit) {
            showFileError("Отдельных картинок в профиле не больше " + profileImageLimit + ". Остальные добавьте внутри PDF.");
            return;
        }
        if (files.some(file => !file.size) ||
            pendingBytes() + files.reduce((total,file) => total + file.size,0) > available) {
            showFileError("Файлы должны быть непустыми и укладываться в общий лимит профиля 100 МБ.");
            return;
        }
        showFileError("");
        for (const file of files) pending.set(file,addTile({name:file.name,file}));
    });
    const add = button("Добавить файлы +",() => picker.click(),"profile-add-files");
    form.append(el("h3","Файлы профиля"),el("p","PDF, Word, TXT, MD и до " + profileImageLimit + " картинок · до 100 МБ на профиль","muted"),grid,picker,add,fileError);
    if (company) {
        const content = el("div",undefined,"profile-files");
        const empty = el("p","Здесь будут картинки, которые ассистент создал для постов, и те, что вы добавите сами. Только их ассистент может использовать в постах.","muted");
        const contentError = el("p",undefined,"sheet-error");
        contentError.setAttribute("role","alert");
        contentError.hidden = true;
        const addContentTile = item => {
            const tile = fileTile(item,savedCompany,async () => {
                if (!await confirmDocumentRemoval(item.name)) return;
                await api("content","DELETE",{company:savedCompany,id:item.id});
                state.content = (state.content || []).filter(entry => entry.id !== item.id);
                const key = "content:" + savedCompany + ":" + item.id;
                documentPreviews.get(key)?.then(url => { if (url) URL.revokeObjectURL(url); });
                documentPreviews.delete(key);
                tile.remove();
                empty.hidden = Boolean(content.childElementCount);
            },"content");
            content.append(tile);
        };
        for (const item of state.content || []) addContentTile(item);
        empty.hidden = Boolean(content.childElementCount);
        const contentPicker = el("input");
        contentPicker.type = "file";
        contentPicker.multiple = true;
        contentPicker.hidden = true;
        contentPicker.accept = ["image/*",...[...profileImageExtensions].map(suffix => "." + suffix)].join(",");
        contentPicker.addEventListener("change",async () => {
            const files = [...contentPicker.files];
            contentPicker.value = "";
            if (!files.length) return;
            if (files.some(file => !profileImageExtensions.has(fileExtension(file.name)))) {
                contentError.textContent = "В раздел «Контент» добавляются только изображения.";
                contentError.hidden = false;
                return;
            }
            contentError.hidden = true;
            try {
                for (const file of files) await api("content?company=" + savedCompany,"PUT",file);
                await load();
                sheet.close();
                profileForm(state.profiles.find(item => item.id === savedCompany));
            } catch (error) {
                contentError.textContent = error.message;
                contentError.hidden = false;
            }
        });
        form.append(el("h3","Контент"),el("p","Только изображения · до 100 МБ","muted"),content,empty,contentPicker,
            button("Добавить картинки +",() => contentPicker.click(),"profile-add-files"),contentError);
    }
    const submit = el("button","Сохранить профиль","primary");
    form.append(submit);
    form.addEventListener("submit", event => {
        event.preventDefault();
        guarded(submit, async () => {
            const {website,...data} = Object.fromEntries(new FormData(form));
            const url = website.trim();
            if ((pending.size || url) && !state.storage) throw new Error("Хранилище материалов не подключено.");
            for (const file of pending.keys()) {
                if (!file.size || file.size > profileFileLimit) throw new Error("Файл должен быть от 1 байта до 100 МБ.");
            }
            if (savedCompany) data.company = savedCompany;
            const saved = await api(savedCompany ? "profile" : "profiles",savedCompany ? "PATCH" : "POST",data);
            savedCompany = savedCompany || saved.id;
            for (const [file,tile] of pending) {
                await api("document?company=" + savedCompany,"PUT",file);
                pending.delete(file);
                available -= file.size;
                tile.querySelector(".profile-file-remove").remove();
            }
            if (url !== savedWebsite) {
                await api("website","POST",{company:savedCompany,url});
                savedWebsite = url;
            }
            sheet.close();
            await load();
        });
    });
    updateCounter();
    showSheet(company ? "Редактировать профиль" : "Новая компания",form);
}

const chatStages = {
    thinking:"Разбираю запрос…", search:"Ищу в интернете…", write:"Пишу пост…",
    image:"Готовлю изображение…", revise:"Редактирую пост…", plan:"Составляю контент-план…",
    critique:"Анализирую публикацию…",
};

async function waitForChat(job) {
    const query = new URLSearchParams({company:job.company,id:job.id});
    const deadline = Date.now() + 8 * 60_000;
    let failures = 0;
    while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve,1000));
        let status;
        try {
            status = await api("chat-progress?" + query,"GET",undefined,AbortSignal.timeout(5000));
        } catch {
            if (++failures < 12) continue;
            break;
        }
        failures = 0;
        if (status.result) return status.result;
        if (!status.stage) throw new Error("Сервис перезапустился. Проверьте черновики перед повтором.");
        if (chatStages[status.stage]) job.stage = status.stage;
        const label = screen.querySelector(".chat-pending-label");
        if (pendingChat === job && state.active === job.company && label) label.textContent = chatStages[job.stage];
    }
    throw new Error("Не удалось получить ответ. Проверьте черновики перед повтором.");
}

function chat() {
    const company = state.active;
    const log = chatLogs.get(company) || [];
    chatLogs.set(company, log);
    const messages = el("div", undefined, "messages");
    if (!log.length && !state.draft_question) {
        const empty = el("div", undefined, "chat-empty");
        const mark = el("span", undefined, "chat-mark");
        mark.append(chatIcon("M7.9 20A9 9 0 1 0 4 16.1L2 22Z M8 12h.01M12 12h.01M16 12h.01"));
        empty.append(mark, el("h2", "Что подготовим?"),
            el("p", "Попросите написать пост или задайте вопрос о компании."));
        messages.append(empty);
    }
    const latestDrafts = new Map();
    const draftDates = new Map();
    for (const message of log) {
        if (!message.draft_id) continue;
        latestDrafts.set(message.draft_id,message);
        if (message.draft_when) draftDates.set(message.draft_id,message.draft_when);
    }
    for (const message of log) {
        const bubble = el("div", undefined, "bubble" + (message.me ? " me" : ""));
        const copy = el("p");
        appendPostText(copy, message.text);
        bubble.append(copy);
        const draft = latestDrafts.get(message.draft_id) === message &&
            (state.posts.find(post => post.id === message.draft_id) ||
             state.chat_posts?.find(post => post.id === message.draft_id));
        if (draft) {
            bubble.classList.add("chat-draft");
            if (!reviewStatuses.includes(draft.status)) {
                copy.remove();
                const status = {
                    published:draft.simulated ? "Тестовая отправка" : "Принято · опубликовано",
                    scheduled:"Принято · запланировано", rejected:"Отклонено",
                }[draft.status];
                bubble.append(telegramPost(draft));
                if (status) {
                    const label = el("p","Статус: " + status,"chat-draft-status");
                    label.dataset.status = draft.status;
                    bubble.append(label);
                }
                messages.append(bubble);
                continue;
            }
            const closeEditor = () => { if (sheet.open) sheet.close(); };
            bubble.append(telegramPost(draft),draftActions(draft,{
                when:draftDates.get(draft.id) || message.proposal?.when,refresh:closeEditor,
            }));
        } else if (message.proposal && !message.draft_id) bubble.append(button("Подтвердить", async () => {
            const result = await api("post", "POST", message.proposal);
            delete message.proposal;
            await load();
            feedback(result.simulated ? "Тестовая отправка. Пост в канал не ушёл." : "Действие выполнено.");
        }, "primary"));
        messages.append(bubble);
    }
    const form = el("form", undefined, "composer");
    const input = el("input");
    input.placeholder = "Сообщение";
    input.setAttribute("aria-label", "Сообщение боту");
    input.maxLength = 4000;
    input.required = true;
    const send = el("button", undefined, "primary send-button");
    send.setAttribute("aria-label", sending ? "Жду ответ…" : "Отправить");
    send.append(chatIcon("M12 19V5 M6 11l6-6 6 6"));
    send.disabled = sending;
    form.append(input, send);
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (input.value.trim()) submit({text:input.value.trim()});
    });
    const question = state.draft_question;
    if (question && !sending) {
        const bubble = el("div",undefined,"bubble");
        const choices = el("div",undefined,"row");
        for (const option of question.draft_options.options) {
            choices.append(button(option.label,() => submit({
                draft_option:{token:question.draft_options.token,choice:option.id},
            }),option.id === "cancel" ? "plain" : undefined));
        }
        bubble.append(el("p",question.text),choices);
        messages.append(bubble);
    }
    async function submit(payload) {
        if (sending) return;
        const job = {company,id:crypto.randomUUID(),stage:"thinking"};
        pendingChat = job;
        sending = true;
        if (payload.text) log.push({text:payload.text, me:true});
        chat();
        try {
            const request = {company,...payload,id:job.id,background:true};
            let result;
            try {
                result = await api("chat","POST",request,AbortSignal.timeout(10000));
            } catch (error) {
                if (!["TimeoutError","TypeError"].includes(error.name)) throw error;
                result = await waitForChat(job);
            }
            if (result.accepted) result = await waitForChat(job);
            if (result.error) throw new Error(result.error);
            if (!result.text?.trim()) throw new Error("Сервис вернул пустой ответ.");
            if (!result.draft_options) log.push({text:result.text,proposal:result.proposal,draft_id:result.draft_id,draft_when:result.proposal?.when});
            if (state.active === company && (result.draft_options || payload.draft_option)) {
                state.draft_question = result.draft_options ? result : null;
            }
            try {
                await load();
                if (state.active === company && result.reload_history) chatLogs.set(company,state.history || []);
            } catch {
                feedback("Ответ получен. Не удалось обновить список черновиков.");
            }
        } catch (error) {
            log.push({text:error.message || "Не удалось получить ответ. Проверьте черновики перед повтором."});
        } finally {
            pendingChat = undefined;
            sending = false;
            if (tab === "chat") chat();
        }
    }
    if (pendingChat?.company === company) {
        const pending = el("p", undefined, "chat-pending");
        pending.setAttribute("role", "status");
        const dots = el("span", undefined, "chat-pending-dots");
        dots.setAttribute("aria-hidden", "true");
        dots.append(el("i"),el("i"),el("i"));
        pending.append(dots,el("span",chatStages[pendingChat.stage],"chat-pending-label"));
        messages.append(pending);
    }
    screen.replaceChildren(messages, form);
    if (log.length || question) requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
}

async function startApp() {
    screen.replaceChildren(loadingContent("list"));
    try {
        await load();
    } catch (error) {
        const panel = el("div",undefined,"loading-error");
        const message = el("p",error.message || "Не удалось загрузить приложение.","sheet-error");
        message.setAttribute("role","alert");
        panel.append(message,button("Повторить",startApp,"primary"));
        screen.replaceChildren(panel);
    }
}

if (initData) startApp();
else {
    screen.replaceChildren(el("p", "Откройте «Маркетолог» через меню бота в Telegram.", "empty"));
    refresh.hidden = true;
}
