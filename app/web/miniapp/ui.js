"use strict";
const telegram = window.Telegram?.WebApp;
telegram?.ready();
telegram?.expand?.();
function syncTelegramInsets() {
    const root = document.documentElement;
    const safe = telegram?.safeAreaInset || {};
    const content = telegram?.contentSafeAreaInset || {};
    root.style.setProperty("--tg-top", (Number(safe.top || 0) + Number(content.top || 0)) + "px");
    root.style.setProperty("--tg-bottom", (Number(safe.bottom || 0) + Number(content.bottom || 0)) + "px");
    if (telegram?.colorScheme) root.dataset.theme = telegram.colorScheme;
}
syncTelegramInsets();
for (const event of ["safeAreaChanged", "contentSafeAreaChanged", "themeChanged"]) {
    telegram?.onEvent?.(event, syncTelegramInsets);
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
let previewUrl;
let slideUrls = [];
let noticeTimer;

/* ---------- Базовые помощники ---------- */

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
    shape.setAttribute("href", "/miniapp/icons.svg#" + name);
    image.append(shape);
    return image;
}

for (const node of document.querySelectorAll("[data-icon]")) {
    node.append(icon(node.dataset.icon));
}

function button(label, handler, style, symbol) {
    const node = el("button", label, style);
    node.type = "button";
    if (symbol) node.prepend(icon(symbol));
    node.addEventListener("click", () => guarded(node, handler));
    return node;
}

function haptic(kind = "light") {
    const feedbackApi = telegram?.HapticFeedback;
    if (!feedbackApi) return;
    if (["success", "error", "warning"].includes(kind)) feedbackApi.notificationOccurred?.(kind);
    else feedbackApi.impactOccurred?.(kind);
}

function confirmAction(text) {
    if (telegram?.showConfirm && telegram.isVersionAtLeast?.("6.2")) {
        return new Promise((resolve) => telegram.showConfirm(text, resolve));
    }
    return Promise.resolve(window.confirm(text));
}

/** Короткое уведомление внизу экрана; ошибки висят дольше. */
function feedback(text, kind = "info") {
    clearTimeout(noticeTimer);
    notice.textContent = text;
    notice.dataset.kind = kind;
    notice.hidden = !text;
    if (text) noticeTimer = setTimeout(() => { notice.hidden = true; }, kind === "error" ? 7000 : 3500);
}

async function guarded(node, action) {
    feedback("");
    node.disabled = true;
    try { await action(); }
    catch (error) {
        haptic("error");
        const message = error.message || "Не удалось выполнить действие.";
        if (sheet.open) {
            sheetBody.querySelector(".sheet-error")?.remove();
            const alert = el("p", message, "sheet-error");
            alert.setAttribute("role", "alert");
            sheetBody.prepend(alert);
            alert.scrollIntoView({block:"nearest"});
        } else feedback(message, "error");
    }
    finally { node.disabled = false; }
}

async function api(path, method = "GET", data) {
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
        method, headers, body, signal: AbortSignal.timeout(method === "PUT" ? 150000 : 240000),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "Сервис недоступен. Попробуйте ещё раз.");
    return result;
}

function remember(key, value) {
    try {
        if (value === undefined) return localStorage.getItem("marketer:" + key);
        localStorage.setItem("marketer:" + key, value);
    } catch { /* хранилище может быть недоступно */ }
    return null;
}

/** Выбор файла без отдельного поля: открываем системный диалог и сразу обрабатываем. */
function pickFiles(accept, multiple, handler) {
    const input = el("input");
    input.type = "file";
    input.accept = accept;
    input.multiple = multiple;
    input.hidden = true;
    document.body.append(input);
    return new Promise((resolve, reject) => {
        input.addEventListener("change", async () => {
            try {
                if (input.files.length) await handler([...input.files]);
                resolve();
            } catch (error) { reject(error); }
            finally { input.remove(); }
        });
        input.addEventListener("cancel", () => { input.remove(); resolve(); });
        input.click();
    });
}

const MATERIALS_ACCEPT = ".pdf,.png,.jpg,.jpeg,.webp,.docx,.txt,.md,.csv,.json,.rtf";
const IMAGE_ACCEPT = "image/jpeg,image/png,image/webp";

/* ---------- Даты и тексты ---------- */

const dayFormat = new Intl.DateTimeFormat("ru-RU", {weekday: "short", day: "numeric", month: "short"});
const timeFormat = new Intl.DateTimeFormat("ru-RU", {hour: "2-digit", minute: "2-digit"});

function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function humanDate(value) {
    if (!value) return "";
    const date = new Date(value);
    const days = Math.round((startOfDay(date) - startOfDay(new Date())) / 86400000);
    const time = timeFormat.format(date);
    if (days === 0) return "Сегодня, " + time;
    if (days === 1) return "Завтра, " + time;
    if (days === -1) return "Вчера, " + time;
    return dayFormat.format(date) + ", " + time;
}

/** Заголовок карточки: первая содержательная строка поста, а не сырой запрос. */
function postTitle(post) {
    const line = (post.text || "").split("\n").map((item) => item.trim())
        .find((item) => item && !item.startsWith("#"));
    const clean = (line || post.topic || "").replace(/^[^\p{L}\p{N}«"]+/u, "").trim();
    const title = clean || "Пост №" + post.id;
    const short = title.length > 90 ? title.slice(0, 88) + "…" : title;
    return short.charAt(0).toUpperCase() + short.slice(1);
}

function postBody(post) {
    const lines = (post.text || "").split("\n");
    const first = lines.findIndex((item) => item.trim() && !item.trim().startsWith("#"));
    return lines.slice(first + 1).join("\n").trim();
}

const STATUS = {
    draft: "Черновик", pending_approval: "Ждёт решения", approved: "Утверждён",
    scheduled: "Запланирован", failed: "Ошибка публикации", published: "Опубликован", rejected: "Отклонён",
};

function statusChip(status) {
    const chip = el("span", STATUS[status] || status, "status");
    chip.dataset.status = status;
    return chip;
}

function activeCompany() {
    return state?.profiles.find((item) => item.id === state.active);
}

/* ---------- Каркас ---------- */

async function load() {
    state = await api("state");
    if (state.active && !sending) chatLogs.set(state.active, state.history || []);
    if (initial && state.active) tab = "home";
    initial = false;
    if (!state.active) tab = "profile";
    render();
}

function showSheet(title, content) {
    document.querySelector("#sheet-title").textContent = title;
    sheetBody.replaceChildren(content);
    if (!sheet.open) sheet.showModal();
    sheetBody.scrollTop = 0;
    telegram?.BackButton?.show?.();
}

function closeSheet() {
    if (sheet.open) sheet.close();
}

document.querySelector("#close").addEventListener("click", closeSheet);
telegram?.BackButton?.onClick?.(closeSheet);
sheet.addEventListener("close", () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = undefined;
    slideUrls.forEach((url) => URL.revokeObjectURL(url));
    slideUrls = [];
    telegram?.BackButton?.hide?.();
});
refresh.addEventListener("click", () => guarded(refresh, async () => {
    await load();
    feedback("Обновлено");
}));
document.querySelector("#company").addEventListener("click", () => {
    if (state?.profiles.length > 1) companySwitcher();
});

function openTab(next) {
    tab = next;
    feedback("");
    haptic("light");
    render();
    if (next !== "chat") window.scrollTo(0, 0);
}

tabs.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-tab]");
    if (!target || !state) return;
    openTab(target.dataset.tab);
});

function render() {
    const company = activeCompany();
    const companyLabel = document.querySelector("#company");
    companyLabel.replaceChildren(el("span", company?.name || ""));
    if (state.profiles.length > 1) companyLabel.append(icon("chevron-down"));
    companyLabel.hidden = !company;
    companyLabel.disabled = state.profiles.length < 2;
    document.querySelector("#workspace").hidden = !company;
    document.querySelector("#workspace-name").textContent = company?.name || "";
    document.querySelector("#title").textContent = {home:"Главная", chat:"Чат с маркетологом", profile:"Компания"}[tab];
    screen.dataset.screen = tab;
    tabs.hidden = false;
    const waiting = state.posts.filter((post) => post.status === "pending_approval").length;
    for (const item of tabs.querySelectorAll("button")) {
        item.hidden = item.dataset.tab !== "profile" && !company;
        if (item.dataset.tab === tab) item.setAttribute("aria-current", "page");
        else item.removeAttribute("aria-current");
        if (item.dataset.tab === "home") {
            item.querySelector(".tab-badge")?.remove();
            if (waiting) item.append(el("span", String(waiting), "tab-badge"));
        }
    }
    screen.replaceChildren();
    if (tab === "home") home(company);
    else if (tab === "chat") chat();
    else profile(company);
}

function section(title, parent = screen, symbol, count, hint) {
    const node = el("section");
    const heading = el("div", undefined, "section-heading");
    if (symbol) {
        const badge = el("span", undefined, "section-icon");
        badge.append(icon(symbol));
        heading.append(badge);
    }
    const titles = el("div", undefined, "section-titles");
    titles.append(el("h2", title));
    if (hint) titles.append(el("p", hint, "section-hint"));
    heading.append(titles);
    if (count) heading.append(el("span", String(count), "section-count"));
    node.append(heading);
    parent.append(node);
    return node;
}

function companySwitcher() {
    const body = el("section");
    const list = el("div", undefined, "list");
    for (const item of state.profiles) {
        const row = el("button", undefined, "list-item list-button");
        row.type = "button";
        const name = el("span", item.name);
        if (item.preset) name.append(el("small", "Стандартный профиль"));
        row.append(name);
        if (item.id === state.active) row.append(icon("check"));
        row.addEventListener("click", () => guarded(row, async () => {
            await api("select", "POST", {id: item.id});
            closeSheet();
            await load();
            feedback("Компания: " + item.name);
        }));
        list.append(row);
    }
    body.append(list, button("Новая компания", () => profileForm(), "primary", "plus"));
    showSheet("Компании", body);
}

/* ---------- Главная ---------- */

function home(company) {
    const stories = state.stories.filter((story) => !remember("story:" + story.title));
    if (stories.length) {
        const strip = el("div", undefined, "stories");
        for (const story of stories) {
            const item = button(story.title, () => {
                remember("story:" + story.title, "1");
                showSheet(story.title, el("p", story.text));
            }, "story", "sparkles");
            strip.append(item);
        }
        screen.append(strip);
    }

    screen.append(quickCreate(company));

    const pending = state.posts.filter((post) => post.status === "pending_approval");
    const drafts = state.posts.filter((post) => ["draft", "failed"].includes(post.status));
    const upcoming = state.posts.filter((post) => post.status === "scheduled")
        .sort((a, b) => a.when.localeCompare(b.when));
    const ready = state.posts.filter((post) => post.status === "approved");

    const summary = el("div", undefined, "summary");
    for (const [count, label, target, symbol] of [
        [pending.length, "ждут решения", "decide", "circle-alert"],
        [upcoming.length, "запланировано", "upcoming", "clock"],
        [ready.length + drafts.length, "в работе", "ready", "file-pen-line"],
    ]) {
        const tile = button("", () => document.getElementById(target)?.scrollIntoView({behavior: "smooth"}), "summary-tile");
        tile.append(icon(symbol), el("strong", String(count)), el("span", label));
        if (target === "decide" && count) tile.dataset.alert = "true";
        summary.append(tile);
    }
    screen.append(summary);

    setupChecklist(company);

    if (state.proposal) {
        const proposal = state.proposal;
        const actions = {publish:"Опубликовать", schedule:"Запланировать", cancel:"Снять с расписания"};
        const request = section("Подтвердите действие", screen, "megaphone", undefined, "Запрос пришёл из чата Telegram");
        const card = el("div", undefined, "card");
        const when = proposal.when ? " · " + humanDate(proposal.when) : "";
        card.append(el("p", actions[proposal.action] + " «" + proposal.topic + "»" + when));
        card.append(button("Подтвердить", async () => {
            await api("post", "POST", proposal);
            haptic("success");
            await load();
        }, "primary", "check"));
        request.append(card);
    }

    const decide = section("Нужно ваше решение", screen, "circle-alert", pending.length,
        pending.length ? "Без вашего «да» ничего не публикуется" : undefined);
    decide.id = "decide";
    if (!pending.length) decide.append(el("p", "Всё согласовано. Новые черновики появятся здесь.", "empty"));
    pending.forEach((post) => decide.append(postCard(post)));

    if (state.occasions?.length && company?.editable) showOccasions(company);
    if (state.plans?.length) showPlans();

    const board = el("div", undefined, "board");
    screen.append(board);
    const schedule = section("Скоро выйдет", board, "calendar-days", upcoming.length);
    schedule.id = "upcoming";
    if (!upcoming.length) schedule.append(el("p", "Запланированных публикаций пока нет.", "empty"));
    upcoming.forEach((post) => schedule.append(postCard(post)));
    const work = section("Утверждено и черновики", board, "file-pen-line", ready.length + drafts.length);
    work.id = "ready";
    if (!ready.length && !drafts.length) work.append(el("p", "Здесь будут утверждённые посты без даты и черновики.", "empty"));
    [...ready, ...drafts].forEach((post) => work.append(postCard(post)));

    const tools = section("Инструменты", screen, "layers");
    const grid = el("div", undefined, "tiles");
    for (const [title, hint, symbol, handler] of [
        ["История", "Что уже вышло", "history", publicationHistory],
        ["Отчёт", "Итоги и выводы", "file-text", () => reportSheet()],
        ["Аналитика", "Подписчики и просмотры", "chart-line", () => metricsSheet()],
        ["Все посты", "Версии и правки", "list", () => poolSheet()],
    ]) grid.append(tile(title, hint, symbol, handler));
    tools.append(grid);
}

function tile(title, hint, symbol, handler) {
    const node = button("", handler, "tile");
    const mark = el("span", undefined, "tile-icon");
    mark.append(icon(symbol));
    const text = el("span", undefined, "tile-text");
    text.append(el("strong", title));
    if (hint) text.append(el("small", hint));
    node.append(mark, text);
    return node;
}

/** Быстрое создание поста прямо с главной: тема + готовые идеи. */
function quickCreate(company) {
    const card = el("form", undefined, "hero");
    card.append(el("h2", "О чём написать пост?"));
    const input = el("textarea");
    input.rows = 2;
    input.maxLength = 4000;
    input.placeholder = "Например: новинка в ассортименте или повод на этой неделе";
    input.setAttribute("aria-label", "Тема поста");
    const ideas = el("div", undefined, "chips");
    const product = (company?.details.products?.[0] || "").split(",")[0].trim();
    const suggestions = [
        product ? "Пост о " + product : "Пост о нашем продукте",
        "История компании",
        "Полезный совет клиентам",
        "Составь контент-план на неделю",
    ];
    for (const idea of suggestions) {
        const chip = el("button", idea, "chip");
        chip.type = "button";
        chip.addEventListener("click", () => { input.value = idea; input.focus(); haptic("light"); });
        ideas.append(chip);
    }
    const submit = el("button", "Подготовить черновик", "hero-action");
    submit.append(icon("sparkles"));
    card.append(input, ideas, submit);
    card.addEventListener("submit", (event) => {
        event.preventDefault();
        const text = input.value.trim();
        if (!text) {
            feedback("Напишите тему или выберите идею.");
            input.focus();
            return;
        }
        // Тема без глагола — явно просим пост, чтобы классификатор не принял её за вопрос
        const task = /пост|план|напиши|составь|подготовь/i.test(text) ? text : "Напиши пост: " + text;
        guarded(submit, () => askAgent(task, submit));
    });
    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            card.requestSubmit();
        }
    });
    return card;
}

/** Отправка задачи агенту из любого места: ответ попадает и в чат. */
async function askAgent(text, node) {
    const company = state.active;
    const log = chatLogs.get(company) || [];
    chatLogs.set(company, log);
    log.push({text, me: true});
    sending = true;
    const label = node?.firstChild;
    const original = label?.textContent;
    if (label && node.classList.contains("hero-action")) label.textContent = "Готовлю черновик…";
    try {
        const result = await api("chat", "POST", {company, text});
        log.push({text: result.text, proposal: result.proposal});
        haptic("success");
        await load();
        const post = mentionedPost(result.text);
        if (post) postSheet(post);
        else feedback(result.text);
    } finally {
        sending = false;
        if (label && original) label.textContent = original;
    }
}

function mentionedPost(text) {
    const match = /№\s?(\d+)/.exec(text || "");
    return match ? state.posts.find((post) => post.id === Number(match[1])) : undefined;
}

function setupChecklist(company) {
    if (!company?.editable) return;
    const filled = Object.values(company.details).filter((value) => Array.isArray(value) ? value.length : value).length;
    const approvedOnce = remember("approved:" + company.id) ||
        state.posts.some((post) => ["approved", "scheduled"].includes(post.status));
    const steps = [
        ["Заполнить профиль компании", filled >= 6, () => profileForm(company)],
        ["Загрузить материалы (брендбук, каталог)", state.documents.length > 0, () => openTab("profile")],
        ["Подключить Telegram-канал", Boolean(company.channel), () => channelSheet(company)],
        ["Утвердить первый пост", Boolean(approvedOnce), () => document.querySelector(".hero textarea")?.focus()],
    ];
    const done = steps.filter(([, ok]) => ok).length;
    if (done === steps.length || remember("checklist-hidden:" + company.id)) return;
    const node = section("Начало работы", screen, "sparkles", done + "/" + steps.length);
    const card = el("div", undefined, "card checklist");
    const bar = el("div", undefined, "progress");
    const fill = el("span");
    fill.style.width = Math.round(done / steps.length * 100) + "%";
    bar.append(fill);
    card.append(bar);
    for (const [title, ok, action] of steps) {
        const row = el("button", undefined, "check-row");
        row.type = "button";
        row.dataset.done = String(ok);
        const mark = el("span", undefined, "check-mark");
        if (ok) mark.append(icon("check"));
        row.append(mark, el("span", title));
        if (!ok) row.append(icon("chevron-right"));
        row.disabled = ok;
        row.addEventListener("click", action);
        card.append(row);
    }
    card.append(button("Скрыть", () => {
        remember("checklist-hidden:" + company.id, "1");
        render();
    }, "plain"));
    node.append(card);
}

async function publicationHistory() {
    const result = await api("history");
    const body = el("section");
    if (!result.posts.length) body.append(el("p", "История пока пуста.", "empty"));
    for (const post of result.posts) {
        const card = el("article", undefined, "card");
        card.append(el("h3", post.topic || "Пост №" + post.id));
        card.append(el("p", post.published_at ? "Опубликован · " + humanDate(post.published_at) : "Отклонён", "muted"));
        const details = el("details");
        details.append(el("summary", "Текст поста"), el("p", post.text, "post-body"));
        card.append(details);
        if (post.url?.startsWith("https://t.me/")) {
            const link = el("a", "Открыть в Telegram", "link-button");
            link.href = post.url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            card.append(link);
        }
        body.append(card);
    }
    showSheet("История публикаций", body);
}

async function metricsSheet(params = {hours: 24}) {
    const data = await api("metrics?" + new URLSearchParams(params));
    const body = el("section");
    const periods = el("div", undefined, "segmented");
    for (const [hours, label] of [[24, "24 часа"], [168, "7 дней"], [720, "30 дней"]]) {
        periods.append(button(label, () => metricsSheet({hours, post: params.post || 0}),
            Number(params.hours) === hours ? "active" : undefined));
    }
    body.append(periods);
    if (!data.min_date) {
        body.append(el("p", "Первые данные появятся после сбора метрик подключённого канала.", "empty"));
        showSheet("Аналитика", body);
        return;
    }

    const dateFields = el("div", undefined, "row");
    const dates = {};
    for (const [key, caption] of [["from", "С"], ["to", "По"]]) {
        const label = el("label", caption);
        const field = el("input");
        field.type = "date";
        field.min = data.min_date;
        field.max = data.max_date;
        field.value = params[key] || (key === "from" ? data.min_date : data.max_date);
        label.append(field);
        dateFields.append(label);
        dates[key] = field;
    }
    dateFields.append(button("Показать", () => {
        if (!dates.from.value || !dates.to.value || dates.from.value > dates.to.value ||
            !dates.from.checkValidity() || !dates.to.checkValidity()) {
            throw new Error("Выберите даты в пределах сбора аналитики.");
        }
        return metricsSheet({from: dates.from.value, to: dates.to.value, post: params.post || 0});
    }));
    body.append(dateFields);

    body.append(el("h3", "Подписчики"));
    if (!data.channels.length) body.append(el("p", "Нет замеров за этот период.", "muted"));
    for (const channel of data.channels) {
        body.append(el("p", channel.name, "muted"), metricChart(channel.points, data.timezone));
    }

    if (data.posts.length) {
        const picker = el("select");
        picker.setAttribute("aria-label", "Пост для графика");
        const empty = el("option", "Выберите пост");
        empty.value = "0";
        picker.append(empty);
        for (const post of data.posts) {
            const option = el("option", "№" + post.id + " · " + post.topic);
            option.value = post.id;
            picker.append(option);
        }
        picker.value = params.post || 0;
        picker.addEventListener("change", () => metricsSheet({...params, post: picker.value})
            .catch(error => feedback(error.message, "error")));
        body.append(picker);
    }
    if (Number(params.post)) {
        for (const [key, label] of [["views", "Просмотры"], ["forwards", "Пересылки"]]) {
            body.append(el("h3", label), metricChart(
                data.points.map(point => [point.at, point[key]]), data.timezone
            ));
        }
        const deleted = data.points.find(point => point.deleted);
        if (deleted) body.append(el("p", "Пост удалён из канала.", "muted"));
    }
    showSheet("Аналитика", body);
}

function metricChart(points, timezone) {
    const actual = points.filter(([, value]) => value != null);
    if (!actual.length) return el("p", "Нет замеров за этот период.", "muted");
    const canvas = el("canvas");
    canvas.width = 640;
    canvas.height = 240;
    canvas.style.width = "100%";
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", "График. Последнее значение " + actual.at(-1)[1]);
    const styles = getComputedStyle(document.documentElement);
    const context = canvas.getContext("2d");
    const low = actual.reduce((min, [, value]) => Math.min(min, value), actual[0][1]);
    const high = actual.reduce((max, [, value]) => Math.max(max, value), actual[0][1]);
    const start = Date.parse(actual[0][0]);
    const end = Date.parse(actual.at(-1)[0]);
    const format = new Intl.DateTimeFormat("ru-RU", {
        timeZone: timezone, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    });
    context.font = "18px sans-serif";
    context.fillStyle = styles.getPropertyValue("--muted") || "#777";
    context.fillText(String(high), 5, 28);
    if (high !== low) context.fillText(String(low), 5, 197);
    context.fillText(format.format(start), 70, 227);
    if (end !== start) {
        context.textAlign = "right";
        context.fillText(format.format(end), 616, 227);
    }
    context.strokeStyle = styles.getPropertyValue("--line") || "#999";
    context.strokeRect(69, 20, 548, 175);
    context.strokeStyle = context.fillStyle = styles.getPropertyValue("--accent") || "#3484ee";
    context.lineWidth = 3;
    let previous;
    for (const [at, value] of points) {
        if (value == null) {
            previous = null;
            continue;
        }
        const date = Date.parse(at);
        const x = start === end ? 343 : 70 + 546 * (date - start) / (end - start);
        const y = low === high ? 108 : 194 - 173 * (value - low) / (high - low);
        if (previous && date - previous.date <= 3600000) {
            context.beginPath();
            context.moveTo(previous.x, previous.y);
            context.lineTo(x, y);
            context.stroke();
        }
        context.fillRect(x - 2, y - 2, 4, 4);
        previous = {x, y, date};
    }
    return canvas;
}

async function poolSheet(before = 0) {
    const pool = await api("post-pool?before=" + before);
    const body = el("section");
    if (!pool.posts.length) body.append(el("p", "Постов пока нет.", "empty"));
    const list = el("div", undefined, "list");
    for (const post of pool.posts) {
        const row = el("button", undefined, "list-item list-button");
        row.type = "button";
        const name = el("span", "№" + post.id + " · " + post.topic);
        name.append(el("small", STATUS[post.status] || post.status));
        row.append(name, icon("chevron-right"));
        row.addEventListener("click", () => guarded(row, () => versionSheet(post.id, before)));
        list.append(row);
    }
    if (pool.posts.length) body.append(list);
    const nav = el("div", undefined, "row");
    if (pool.next) nav.append(button("Более ранние", () => poolSheet(pool.next)));
    if (before) nav.append(button("К последним", () => poolSheet()));
    body.append(nav);
    showSheet("Все посты", body);
}

async function versionSheet(postId, before) {
    const data = await api("versions?id=" + postId);
    const body = el("section");
    body.append(button("Ко всем постам", () => poolSheet(before), "plain"));
    if (data.plan_item_id) body.append(el("p", "Пункт контент-плана №" + data.plan_item_id, "muted"));
    const sources = {
        chat: "Запрос в чате", scheduler: "Автономный планировщик", plan: "Контент-план",
        manual: "Правка в Mini App", photo: "Фотобанк", occasion: "Календарь поводов",
        legacy: "Источник неизвестен",
    };
    const decisions = {
        post_approved: "Утверждена", post_published: "Опубликована", post_rejected: "Отклонена",
    };
    if (!data.versions.length) body.append(el("p", "Версий пока нет.", "empty"));
    for (const version of [...data.versions].reverse()) {
        const card = el("article", undefined, "card");
        card.append(el("h3", "Версия №" + version.id),
            el("p", (sources[version.source] || version.source) + " · " + humanDate(version.at), "muted"));
        if (version.parent_id) card.append(el("p", "На основе версии №" + version.parent_id, "muted"));
        if (version.request) card.append(el("p", "Запрос · " + version.request, "post-body"));
        if (version.instruction) card.append(el("p", "Изменение · " + version.instruction));
        for (const decision of version.decisions) {
            card.append(el("p", (decisions[decision.type] || decision.type) + " · " + humanDate(decision.at)));
        }
        card.append(el("p", version.text, "post-body"));
        if (version.has_image) card.append(button("Показать изображение", async () => {
            const response = await fetch("/miniapp/api/preview?id=" + postId + "&revision=" + version.id,
                {headers: {Authorization: "Telegram " + initData}});
            if (!response.ok) throw new Error("Изображение недоступно.");
            const image = el("img", undefined, "post-preview");
            image.alt = "Изображение версии №" + version.id;
            const objectUrl = URL.createObjectURL(await response.blob());
            image.onload = image.onerror = () => URL.revokeObjectURL(objectUrl);
            image.src = objectUrl;
            card.append(image);
        }, undefined, "image"));
        if (version.image_model) card.append(el("p", "Модель изображения · " + version.image_model, "muted"));
        if (version.image_prompt) {
            const details = el("details");
            details.append(el("summary", "Промпт генерации"), el("p", version.image_prompt, "post-body"));
            card.append(details);
        }
        body.append(card);
    }
    showSheet(data.topic || "Версии поста", body);
}

async function reportSheet(days = 7) {
    const report = await api("report?days=" + days);
    const body = el("section");
    const periods = el("div", undefined, "segmented");
    for (const period of [7, 30, 90]) {
        const item = button(period + " дней", () => reportSheet(period), period === days ? "active" : undefined);
        item.setAttribute("aria-pressed", String(period === days));
        periods.append(item);
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    const bytes = Uint8Array.from(atob(report.chart), char => char.charCodeAt(0));
    previewUrl = URL.createObjectURL(new Blob([bytes], {type: "image/png"}));
    const image = el("img", undefined, "post-preview");
    image.src = previewUrl;
    image.alt = "Реакции и комментарии по публикациям";
    body.append(periods, image, el("p", report.text, "post-body"));
    if (report.posts?.length) {
        body.append(el("h3", "Счётчики по постам"));
        const list = el("div", undefined, "list");
        for (const post of report.posts) {
            const row = el("div", undefined, "list-item");
            const counters = [
                post.views == null ? "" : `👁 ${post.views}`,
                post.forwards == null ? "" : `↗ ${post.forwards}`,
            ];
            row.append(el("span", post.topic), el("strong", counters.filter(Boolean).join("  ")));
            list.append(row);
        }
        body.append(list);
    }
    showSheet("Отчёт", body);
}

/** Ближайшие поводы из календаря компании: пост готовится сам за 3 дня или сразу по кнопке. */
function showOccasions(company) {
    const node = section("Ближайшие поводы", screen, "calendar-days", undefined,
        "За 3 дня до даты пост придёт на согласование сам");
    const list = el("div", undefined, "list");
    const dateLabel = new Intl.DateTimeFormat("ru-RU", {day: "numeric", month: "long"});
    for (const item of state.occasions) {
        const row = el("div", undefined, "list-item");
        const name = el("span", item.title);
        const days = item.days === 0 ? "сегодня" : item.days === 1 ? "завтра" : "через " + item.days + " дн.";
        name.append(el("small", dateLabel.format(new Date(item.date + "T12:00:00")) + " · " + days));
        row.append(name, button("Пост сейчас", async () => {
            const result = await api("occasion", "POST", {company: company.id, key: item.key});
            haptic("success");
            await load();
            const post = state.posts.find((entry) => entry.id === result.id);
            if (post) postSheet(post);
            else feedback("Черновик готов — он в «Нужно ваше решение»", "success");
        }, "small", "sparkles"));
        list.append(row);
    }
    node.append(list);
}

/** Слайды в фирменном стиле из текста поста: выберите один — он станет картинкой поста. */
async function carouselSheet(post) {
    const data = await api("carousel?id=" + post.id);
    const body = el("section");
    body.append(el("p", "Выберите слайд — он станет картинкой этого поста, текст останется. Пост снова придёт на согласование.", "muted"));
    const strip = el("div", undefined, "slides");
    // CSP страницы разрешает картинки только из self, https: и blob:, поэтому не data:
    slideUrls.forEach((url) => URL.revokeObjectURL(url));
    slideUrls = data.slides.map((encoded) => {
        const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
        return URL.createObjectURL(new Blob([bytes], {type: "image/png"}));
    });
    let chosen = 0;
    const choice = el("p", undefined, "slide-choice");
    const frames = slideUrls.map((url, index) => {
        const frame = el("button", undefined, "slide-pick");
        frame.type = "button";
        const image = el("img", undefined, "slide");
        image.alt = "Слайд " + (index + 1);
        image.src = url;
        frame.append(image, el("span", String(index + 1), "slide-number"));
        frame.addEventListener("click", () => select(index));
        strip.append(frame);
        return frame;
    });
    function select(index) {
        chosen = index;
        frames.forEach((frame, i) => frame.setAttribute("aria-pressed", String(i === index)));
        choice.textContent = "Выбран слайд " + (index + 1) + " из " + frames.length;
        haptic("light");
    }
    select(0);
    body.append(strip, choice, button("Сделать картинкой поста", async () => {
        await api("carousel", "POST", {company: state.active, id: post.id, slide: chosen, version: post.version});
        haptic("success");
        closeSheet();
        await load();
        const updated = state.posts.find((entry) => entry.id === post.id);
        feedback("Картинка добавлена — пост снова на согласовании", "success");
        if (updated) postSheet(updated);
    }, "primary", "image"));
    body.append(button("Прислать все слайды в чат", async () => {
        await api("carousel", "POST", {company: state.active, id: post.id});
        haptic("success");
        feedback("Слайды отправлены в чат с ботом — можно переслать в канал", "success");
    }, "plain", "send"));
    showSheet("Картинка из слайдов", body);
}

function showPlans() {
    const plans = section("Контент-планы", screen, "calendar-days", state.plans.length);
    const list = el("div", undefined, "list");
    for (const plan of state.plans) {
        const row = el("button", undefined, "list-item list-button");
        row.type = "button";
        const label = el("span", plan.title);
        const approved = plan.status === "approved";
        label.append(el("small", approved ? "Утверждён" : "Ждёт утверждения"));
        row.append(label, icon("chevron-right"));
        row.addEventListener("click", () => {
            const body = el("section");
            const dates = el("ol", undefined, "plan-dates");
            const statuses = {drafted:"Черновик готов", published:"Опубликовано", skipped:"Отклонено"};
            for (const item of plan.items) {
                const entry = el("li");
                const date = item.when ? dayFormat.format(new Date(item.when)) : "";
                entry.append(el("strong", item.topic),
                    el("p", [date, item.rubric, statuses[item.status]].filter(Boolean).join(" · "), "muted"));
                dates.append(entry);
            }
            body.append(dates);
            const company = activeCompany();
            if (company?.editable) {
                const actions = el("div", undefined, "row");
                const changePlan = async (action, fields = {}) => {
                    await api("plan", "POST", {
                        company: company.id, id: plan.id, version: plan.version, action, ...fields,
                    });
                    haptic("success");
                    closeSheet();
                    await load();
                };
                if (!approved) {
                    actions.append(button("Утвердить план", () => changePlan("approve"), "primary", "check"));
                    actions.append(button("Изменить темы", () => editPlan(plan, changePlan), undefined, "pencil"));
                }
                actions.append(button("В архив", async () => {
                    if (await confirmAction("Отправить план в архив? Готовые черновики сохранятся.")) {
                        return changePlan("archive");
                    }
                }, "danger"));
                body.append(actions);
            }
            showSheet(plan.title, body);
        });
        list.append(row);
    }
    plans.append(list);
}

function editPlan(plan, changePlan) {
    const form = el("form");
    const topics = plan.items.map((item, index) => {
        const date = item.when ? dayFormat.format(new Date(item.when)) : "";
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

/* ---------- Посты ---------- */

async function postAction(post, action, fields = {}, message) {
    await api("post", "POST", {id: post.id, company: state.active, action, version: post.version, ...fields});
    if (["approve", "schedule", "publish"].includes(action)) remember("approved:" + state.active, "1");
    haptic("success");
    closeSheet();
    await load();
    if (message) feedback(message, "success");
}

function postCard(post) {
    const card = el("article", undefined, "card post-card");
    card.dataset.status = post.status;
    const meta = el("div", undefined, "post-meta");
    meta.append(statusChip(post.status));
    if (post.when) {
        const date = el("span", humanDate(post.when), "post-date");
        date.prepend(icon("clock"));
        meta.append(date);
    }
    if (post.has_image) {
        const media = el("span", undefined, "post-date");
        media.append(icon("image"));
        media.setAttribute("aria-label", "С изображением");
        meta.append(media);
    }
    if (post.brand_issues?.length) {
        const warn = el("span", "Бренд-гид", "status warn");
        warn.prepend(icon("circle-alert"));
        warn.title = post.brand_issues.join("; ");
        meta.append(warn);
    }
    card.append(meta, el("h3", postTitle(post)));
    const excerpt = postBody(post);
    if (excerpt) card.append(el("p", excerpt, "post-excerpt"));
    const actions = el("div", undefined, "row card-actions");
    if (post.status === "pending_approval") {
        actions.append(button("Утвердить", () => postAction(post, "approve", {}, "Пост утверждён"), "primary small", "check"));
    }
    const open = button(post.status === "pending_approval" ? "Посмотреть" : "Открыть", () => postSheet(post), "small");
    actions.append(open);
    card.append(actions);
    card.addEventListener("click", (event) => {
        if (event.target.closest("button")) return;
        postSheet(post);
    });
    return card;
}

/** Предпросмотр в виде поста Telegram-канала. */
function postPreview(post) {
    const company = activeCompany();
    const bubble = el("div", undefined, "tg-post");
    const header = el("div", undefined, "tg-post-header");
    header.append(el("span", (company?.name || "К").slice(0, 1), "tg-avatar"),
        el("strong", company?.channel || company?.name || "Канал"));
    bubble.append(header);
    const text = el("p", post.text, "post-body");
    bubble.append(text);
    bubble.append(el("span", post.when ? humanDate(post.when) : "Предпросмотр", "tg-post-time"));
    if (post.has_image) {
        fetch("/miniapp/api/preview?id=" + post.id, {headers:{Authorization:"Telegram " + initData}})
            .then((response) => response.ok ? response.blob() : null)
            .then((blob) => {
                if (!blob || !sheet.open || !bubble.isConnected) return;
                if (previewUrl) URL.revokeObjectURL(previewUrl);
                previewUrl = URL.createObjectURL(blob);
                const image = el("img", undefined, "tg-post-image");
                image.alt = "Иллюстрация к посту";
                image.src = previewUrl;
                header.after(image);
            }).catch(() => {});
    }
    return bubble;
}

function quickTimes(post) {
    const now = new Date();
    const at = (days, hours) => {
        const date = startOfDay(now);
        date.setDate(date.getDate() + days);
        date.setHours(hours, 0, 0, 0);
        return date;
    };
    const options = [];
    const suggested = new Date(post.suggested_at || 0);
    if (suggested > now) options.push(["Рекомендуем · " + humanDate(suggested), suggested]);
    if (now.getHours() < 17) options.push(["Сегодня 18:00", at(0, 18)]);
    options.push(["Завтра 10:00", at(1, 10)], ["Завтра 18:00", at(1, 18)]);
    const monday = at((8 - now.getDay()) % 7 || 7, 10);
    options.push(["Пн " + timeFormat.format(monday), monday]);
    return options;
}

const QUICK_EDITS = [
    ["Короче", "Сделай текст короче, сохрани главное"],
    ["Официальнее", "Сделай тон более деловым и официальным"],
    ["Проще", "Сделай текст проще и живее, без канцелярита"],
    ["Добавь эмодзи", "Добавь немного уместных эмодзи"],
    ["Без эмодзи", "Убери все эмодзи"],
    ["Призыв к действию", "Добавь в конце понятный призыв к действию"],
    ["Другой вариант", "Напиши другой вариант этого поста на ту же тему"],
];

function postSheet(post) {
    const content = el("section", undefined, "post-sheet");
    const status = el("div", undefined, "post-meta");
    status.append(statusChip(post.status));
    if (post.when) status.append(el("span", "Выйдет: " + humanDate(post.when), "post-date"));
    content.append(status, postPreview(post));
    if (post.brand_issues?.length) {
        const box = el("div", undefined, "brand-box");
        const head = el("strong", "Проверка по бренд-гиду");
        head.prepend(icon("circle-alert"));
        const list = el("ul");
        for (const issue of post.brand_issues) list.append(el("li", issue));
        box.append(head, list, button("Исправить по бренд-гиду", () => postAction(post, "revise", {
            instruction: "Исправь текст по бренд-гиду компании: " + post.brand_issues.join("; ") +
                ". Остальное не меняй.",
        }, "Готова исправленная версия"), "primary small", "sparkles"));
        content.append(box);
    }

    const canPublish = !["scheduled", "draft"].includes(post.status);
    const canSchedule = ["pending_approval", "approved", "scheduled"].includes(post.status);
    const primary = el("div", undefined, "action-bar");
    if (canPublish) {
        primary.append(button(post.status === "failed" ? "Повторить публикацию" : "Опубликовать сейчас", async () => {
            if (await confirmAction("Опубликовать этот пост в канал сейчас?")) {
                return postAction(post, "publish", {}, "Пост отправлен в канал");
            }
        }, "primary", "send"));
    }
    if (post.status === "pending_approval") {
        primary.append(button("Утвердить без даты", () => postAction(post, "approve", {}, "Пост утверждён"), undefined, "check"));
    }
    if (primary.childElementCount) content.append(primary);

    if (canSchedule) {
        const block = section(post.status === "scheduled" ? "Перенести публикацию" : "Запланировать", content, "clock");
        const chips = el("div", undefined, "chips");
        for (const [label, date] of quickTimes(post)) {
            chips.append(button(label, () => postAction(post, "schedule", {when: date.toISOString()},
                "Запланировано: " + humanDate(date)), "chip"));
        }
        const custom = el("div", undefined, "inline-field");
        const when = el("input");
        when.type = "datetime-local";
        when.setAttribute("aria-label", "Своя дата и время");
        const proposed = new Date(post.when || post.suggested_at || 0);
        if (proposed > new Date()) {
            const local = new Date(proposed.getTime() - proposed.getTimezoneOffset() * 60000);
            when.value = local.toISOString().slice(0, 16);
        }
        custom.append(when, button("ОК", () => {
            if (!when.value) throw new Error("Выберите дату и время.");
            const date = new Date(when.value);
            if (date <= new Date()) throw new Error("Выберите время в будущем.");
            return postAction(post, "schedule", {when: date.toISOString()}, "Запланировано: " + humanDate(date));
        }, "primary"));
        block.append(chips, custom);
    }

    const edit = section("Изменить текст", content, "pencil", undefined, "После правки пост снова придёт на согласование");
    const edits = el("div", undefined, "chips");
    for (const [label, instruction] of QUICK_EDITS) {
        edits.append(button(label, () => postAction(post, "revise", {instruction}, "Готова новая версия"), "chip"));
    }
    const revision = el("textarea");
    revision.rows = 2;
    revision.placeholder = "Своя правка: например, упомяни выставку";
    revision.setAttribute("aria-label", "Правки к посту");
    edit.append(edits, revision, button("Внести правку", () => {
        if (!revision.value.trim()) throw new Error("Опишите, что изменить.");
        return postAction(post, "revise", {instruction: revision.value}, "Готова новая версия");
    }, undefined, "sparkles"));

    const company = activeCompany();
    if (company?.editable) {
        const media = section("Изображение", content, "image");
        const row = el("div", undefined, "row");
        row.append(button(post.has_image ? "Заменить фото" : "Своё фото", () => pickFiles(IMAGE_ACCEPT, false, async ([file]) => {
            await api("post?id=" + post.id + "&version=" + encodeURIComponent(post.version), "PUT", file);
            haptic("success");
            closeSheet();
            await load();
            feedback("Фото добавлено — пост снова на согласовании", "success");
        }), undefined, "camera"));
        if (post.has_image) row.append(button("Убрать", () => postAction(post, "remove_photo", {}, "Изображение убрано"), "plain"));
        row.append(button("Из слайдов", () => carouselSheet(post), undefined, "layers"));
        media.append(row);
    }

    const footer = el("div", undefined, "row sheet-footer");
    footer.append(button("Версии поста", () => versionSheet(post.id, 0), "plain", "history"));
    const approved = ["scheduled", "approved"].includes(post.status);
    footer.append(button(approved ? "Снять с расписания" : "Отклонить", async () => {
        const question = approved ? "Снять пост с расписания?" : "Отклонить этот черновик? Он не будет опубликован.";
        if (await confirmAction(question)) {
            return postAction(post, approved ? "cancel" : "reject", {}, approved ? "Снято с расписания" : "Черновик отклонён");
        }
    }, "danger", "trash-2"));
    content.append(footer);
    showSheet(postTitle(post), content);
}

/* ---------- Компания ---------- */

const profileFields = {
    name:"Название", description:"Чем занимается", products:"Продукты и услуги",
    audience:"Аудитория", usp:"Что отличает компанию", tone:"Тон общения",
    visual_style:"Визуальный стиль", restrictions:"Ограничения", channel:"Telegram-канал",
};
const profileHints = {
    description: "2–3 предложения: кто вы и чем полезны клиентам",
    products: "Через запятую",
    tone: "Например: деловой, дружелюбный, без сленга",
    restrictions: "Что нельзя писать: цены, обещания, сравнения…",
    channel: "@username или ссылка на канал; бот должен быть админом",
};

function profile(company) {
    if (company) {
        const card = el("div", undefined, "card company-card");
        const top = el("div", undefined, "company-top");
        top.append(el("span", company.name.slice(0, 1), "avatar"));
        const name = el("div");
        name.append(el("h2", company.name));
        const badges = el("div", undefined, "row");
        const channelChip = el("button", company.channel ? "Канал: " + company.channel : "Подключить канал",
            company.channel ? "status" : "status warn");
        channelChip.type = "button";
        channelChip.prepend(icon("megaphone"));
        channelChip.disabled = !company.editable;
        channelChip.addEventListener("click", () => channelSheet(company));
        badges.append(channelChip);
        if (company.preset) {
            const shield = el("span", "Стандартный · защищён", "status ok");
            shield.prepend(icon("shield-check"));
            badges.append(shield);
        }
        name.append(badges);
        top.append(name);
        card.append(top);

        const keys = Object.keys(profileFields).filter((key) => key !== "channel");
        const filled = keys.filter((key) => {
            const value = company.details[key];
            return Array.isArray(value) ? value.length : value;
        }).length;
        const percent = Math.round(filled / keys.length * 100);
        const meter = el("div", undefined, "meter");
        meter.append(el("span", "Профиль заполнен на " + percent + "%"));
        const bar = el("div", undefined, "progress");
        const fill = el("span");
        fill.style.width = percent + "%";
        bar.append(fill);
        meter.append(bar);
        card.append(meter);

        const details = el("details", undefined, "profile-details");
        details.append(el("summary", "Что знает маркетолог о компании"));
        for (const key of keys) {
            const value = company.details[key];
            const text = Array.isArray(value) ? value.join("\n") : value;
            if (!text) continue;
            const item = el("div", undefined, "detail");
            item.append(el("small", profileFields[key]), el("p", text, "post-body"));
            details.append(item);
        }
        card.append(details);
        if (company.editable) {
            card.append(button("Редактировать профиль", () => profileForm(company), "primary", "pencil"));
        }
        screen.append(card);
        materials(company);
        if (company.editable && state.management) management(company);
    }

    const profiles = section("Компании", screen, "building-2", state.profiles.length);
    const list = el("div", undefined, "list");
    for (const item of state.profiles) {
        const row = el("button", undefined, "list-item list-button");
        row.type = "button";
        const name = el("span", item.name);
        if (item.preset) name.append(el("small", "Стандартный профиль"));
        row.append(name);
        if (item.id === state.active) row.append(el("span", "Выбрана", "status ok"));
        else row.append(icon("chevron-right"));
        row.addEventListener("click", () => guarded(row, async () => {
            if (item.id === state.active) return;
            await api("select", "POST", {id: item.id});
            await load();
            feedback("Компания: " + item.name);
        }));
        list.append(row);
    }
    if (state.profiles.length) profiles.append(list);
    profiles.append(button("Новая компания", () => profileForm(), company ? undefined : "primary", "plus"));

    if (company?.editable && !company.preset) {
        screen.append(button("Удалить компанию", async () => {
            if (!await confirmAction("Удалить «" + company.name + "» вместе с материалами? Это нельзя отменить.")) return;
            await api("profile", "DELETE", {company: company.id});
            await load();
            feedback("Компания удалена");
        }, "danger plain", "trash-2"));
    }

    const fullName = [state.user.first_name, state.user.last_name].filter(Boolean).join(" ");
    screen.append(el("p", "Вы вошли как " + (fullName || "пользователь Telegram") +
        (state.user.username ? " (@" + state.user.username + ")" : ""), "footnote"));
}

/** Подключение канала по шагам: что сделать в Telegram и куда вписать адрес. */
function channelSheet(company) {
    const form = el("form");
    if (company.channel) {
        const current = el("div", undefined, "card toggle-card");
        const text = el("div");
        text.append(el("strong", company.channel), el("p", "Посты публикуются в этот канал", "muted"));
        current.append(text, button("Отключить", async () => {
            if (!await confirmAction("Отключить канал? Публикации остановятся.")) return;
            await api("profile", "PATCH", {company: company.id, channel: ""});
            closeSheet();
            await load();
            feedback("Канал отключён");
        }, "danger small"));
        form.append(current);
    }
    const steps = el("ol", undefined, "steps");
    for (const step of [
        "Откройте канал в Telegram → «Управление каналом» → «Администраторы».",
        "Добавьте бота-маркетолога и включите право «Публикация сообщений».",
        "Впишите адрес канала ниже и нажмите «Проверить и подключить».",
    ]) steps.append(el("li", step));
    const label = el("label", "Адрес канала");
    const input = el("input");
    input.required = true;
    input.autocomplete = "off";
    input.placeholder = "@my_channel или https://t.me/my_channel";
    input.value = company.channel || "";
    label.append(input, el("small", "Для закрытого канала — ID вида -100…; подключить может только админ канала", "hint"));
    const submit = el("button", "Проверить и подключить", "primary");
    submit.prepend(icon("link"));
    form.append(steps, label, submit);
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        guarded(submit, async () => {
            await api("profile", "PATCH", {company: company.id, channel: input.value.trim()});
            haptic("success");
            closeSheet();
            await load();
            feedback("Канал подключён — бот может публиковать", "success");
        });
    });
    showSheet(company.channel ? "Канал компании" : "Подключить канал", form);
    if (!company.channel) input.focus();
}

function management(company) {
    const data = state.management;
    const operations = section("Управление", screen, "layers");
    const grid = el("div", undefined, "tiles");
    const waiting = data.comments.filter((item) => item.status === "pending").length;
    grid.append(
        tile("Фотобанк", (data.photos?.length || 0) + " фото", "camera", () => photoLibrary()),
        tile("Комментарии", waiting ? waiting + " ждут ответа" : "Ответы подписчикам", "message-circle",
            () => commentsSheet(company, data)),
        tile("Расходы", Number(data.spend).toFixed(0) + " ₽ за месяц" + (data.budget ? " из " + data.budget : ""),
            "wallet", () => budgetSheet(company, data)),
        tile("Журнал", "Кто что делал", "history", () => eventsSheet(data)),
        tile("Диагностика", "Канал и расписание", "activity", diagnosticsSheet),
    );
    if (company.invite_enabled) grid.append(tile("Приглашение", "Ссылка для клиента", "link", () => inviteSheet(company, data)));
    if (company.invite_enabled && data.clients.length) {
        grid.append(tile("Доступ клиентов", data.clients.length + " клиент(ов)", "users", () => clientsSheet(company, data)));
    }
    operations.append(grid);
}

async function diagnosticsSheet() {
    const info = await api("diagnostics");
    const body = el("section");
    body.append(el("p", info.dry_run ? "Тестовый режим: посты в канал не уходят" : "Публикация в канал включена",
        info.dry_run ? "status warn" : "status ok"));
    body.append(el("p", info.channel || "Канал не подключён", "muted"));
    if (info.channel_check) body.append(el("p", info.channel_check.message));
    const list = el("div", undefined, "list");
    for (const job of info.jobs) {
        const row = el("div", undefined, "list-item");
        row.append(el("span", job.name), el("small", job.next));
        list.append(row);
    }
    if (info.jobs.length) body.append(el("h3", "Фоновые задачи"), list);
    showSheet("Диагностика", body);
}

function budgetSheet(company, data) {
    const form = el("form");
    form.append(el("p", "Потрачено в этом месяце: " + Number(data.spend).toFixed(2) + " ₽"));
    const label = el("label", "Месячный лимит, ₽");
    const input = el("input");
    input.type = "number";
    input.inputMode = "decimal";
    input.min = "0.01";
    input.max = "1000000";
    input.step = "0.01";
    input.value = data.budget ?? "";
    input.placeholder = "Без лимита";
    label.append(input, el("small", "При 80% придёт предупреждение, при 100% нейросеть остановится", "hint"));
    const save = el("button", "Сохранить", "primary");
    form.append(label, save);
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        guarded(save, async () => {
            await api("settings", "POST", {company: company.id, action:"budget", value:input.value ? Number(input.value) : null});
            closeSheet();
            await load();
            feedback("Лимит сохранён", "success");
        });
    });
    showSheet("Расходы и лимит", form);
}

function eventsSheet(data) {
    const list = el("div", undefined, "list");
    for (const event of data.events) {
        const row = el("div", undefined, "list-item");
        const text = el("span", event.text || event.type);
        text.append(el("small", humanDate(event.date)));
        row.append(text);
        list.append(row);
    }
    showSheet("Журнал действий", data.events.length ? list : el("p", "Событий пока нет.", "empty"));
}

function inviteSheet(company, data) {
    const body = el("section");
    if (data.invite) {
        const link = el("input");
        link.value = data.invite;
        link.readOnly = true;
        link.setAttribute("aria-label", "Ссылка-приглашение");
        body.append(link, button("Скопировать", async () => {
            await navigator.clipboard.writeText(data.invite);
            haptic("success");
            feedback("Ссылка скопирована", "success");
        }, "primary", "link"));
    }
    body.append(button("Создать новую ссылку", async () => {
        await api("settings", "POST", {company: company.id, action:"invite"});
        await load();
        closeSheet();
        feedback("Старая ссылка больше не работает. Откройте «Приглашение».");
    }, "plain"));
    showSheet("Приглашение", body);
}

function clientsSheet(company, data) {
    const list = el("div", undefined, "list");
    for (const client of data.clients) {
        const row = el("div", undefined, "list-item");
        const name = el("span", client.name);
        name.append(el("small", client.active ? "Есть доступ" : "Доступ отключён"));
        row.append(name, button(client.active ? "Отключить" : "Вернуть", async () => {
            await api("settings", "POST", {company:company.id, action:"client_access", id:client.id, active:!client.active});
            closeSheet();
            await load();
        }, client.active ? "danger small" : "small"));
        list.append(row);
    }
    showSheet("Доступ клиентов", list);
}

function photoLibrary() {
    const body = el("section");
    body.append(button("Добавить фото", () => pickFiles(IMAGE_ACCEPT, false, async ([file]) => {
        await api("photo", "PUT", file);
        await load();
        photoLibrary();
        feedback("Фото добавлено", "success");
    }), "primary", "camera"));
    const photos = state.management.photos || [];
    if (!photos.length) body.append(el("p", "Загрузите фото объектов и производства — бот сделает из них посты.", "empty"));
    const list = el("div", undefined, "list");
    for (const photo of photos) {
        const row = el("button", undefined, "list-item list-button");
        row.type = "button";
        row.append(el("span", photo.caption || "Фото №" + photo.id), icon("chevron-right"));
        row.addEventListener("click", () => guarded(row, () => openLibraryPhoto(photo)));
        list.append(row);
    }
    if (photos.length) body.append(list);
    showSheet("Фотобанк", body);
}

async function openLibraryPhoto(photo) {
    const response = await fetch("/miniapp/api/photo?id=" + photo.id, {
        headers: {Authorization: "Telegram " + initData},
    });
    if (!response.ok) throw new Error("Фото недоступно.");
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(await response.blob());
    const image = el("img", undefined, "post-preview");
    image.src = previewUrl;
    image.alt = photo.caption || "Фото компании";
    const form = el("form");
    const label = el("label", "О чём пост с этим фото?");
    const topic = el("input");
    topic.required = true;
    topic.maxLength = 512;
    topic.value = (photo.caption || "").slice(0, 512);
    label.append(topic);
    const submit = el("button", "Создать черновик с фото", "primary");
    form.append(image, label, submit);
    form.addEventListener("submit", event => {
        event.preventDefault();
        guarded(submit, async () => {
            await api("photo?id=" + photo.id, "POST", {topic: topic.value});
            haptic("success");
            closeSheet();
            await load();
            feedback("Черновик с фото готов к согласованию", "success");
        });
    });
    showSheet("Фото компании", form);
}

function commentsSheet(company, data) {
    const body = el("section");
    const toggle = el("div", undefined, "card toggle-card");
    const text = el("div");
    text.append(el("strong", "Автоответы"),
        el("p", data.auto_reply ? "Бот отвечает сам, жалобы всё равно придут вам" : "Каждый ответ сначала приходит вам", "muted"));
    toggle.append(text, button(data.auto_reply ? "Выключить" : "Включить", async () => {
        await api("settings", "POST", {company: company.id, action:"auto_reply", enabled:!data.auto_reply});
        await load();
        commentsSheet(company, state.management);
    }, data.auto_reply ? "small" : "primary small"));
    body.append(toggle);
    for (const item of data.comments) {
        const card = el("article", undefined, "card");
        card.append(el("h3", item.author || "Подписчик"), el("p", item.text));
        if (item.status === "pending") {
            const input = el("textarea");
            input.value = item.reply || "";
            input.maxLength = 1000;
            input.setAttribute("aria-label", "Ответ на комментарий");
            const update = async (action) => {
                await api("comment", "POST", {company:company.id, id:item.id, action, text:input.value});
                haptic("success");
                await load();
                commentsSheet(company, state.management);
            };
            const actions = el("div", undefined, "row");
            actions.append(button("Отправить ответ", () => update("send"), "primary", "send"),
                button("Пропустить", () => update("skip"), "plain"));
            card.append(input, actions);
        } else if (item.reply && item.status === "sent") card.append(el("p", "Ответ: " + item.reply, "muted"));
        body.append(card);
    }
    if (!data.comments.length) body.append(el("p", "Комментариев пока нет. Подключите группу обсуждения к каналу.", "empty"));
    showSheet("Комментарии", body);
}

function profileForm(company) {
    const form = el("form");
    for (const [key, label] of Object.entries(profileFields)) {
        const field = el("label", label);
        const multiline = ["description", "visual_style", "products", "restrictions", "tone", "usp", "audience"].includes(key);
        const input = el(multiline ? "textarea" : "input");
        input.name = key;
        if (multiline) input.rows = key === "description" ? 4 : 2;
        const value = key === "channel" ? company?.channel : company?.details[key];
        input.value = Array.isArray(value) ? value.join(", ") : value || "";
        input.maxLength = key === "name" ? 255 : 2000;
        input.required = key === "name";
        if (key === "channel") input.placeholder = "@my_channel";
        field.append(input);
        if (profileHints[key]) field.append(el("small", profileHints[key], "hint"));
        form.append(field);
    }
    let savedCompany = company?.id;
    let pendingFiles = [];
    const attach = el("div", undefined, "attach");
    const attachLabel = el("span", "Брендбук, каталог, презентации — маркетолог их изучит", "muted");
    attach.append(button("Прикрепить файлы", () => pickFiles(MATERIALS_ACCEPT, true, async (files) => {
        pendingFiles = files;
        attachLabel.textContent = files.map((file) => file.name).join(", ");
    }), undefined, "paperclip"), attachLabel);
    form.append(attach);
    const submit = el("button", company ? "Сохранить" : "Создать компанию", "primary");
    form.append(submit);
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        guarded(submit, async () => {
            const data = Object.fromEntries(new FormData(form));
            if (savedCompany) data.company = savedCompany;
            if (pendingFiles.length && !state.storage) throw new Error("Хранилище материалов не подключено.");
            const saved = await api(savedCompany ? "profile" : "profiles", savedCompany ? "PATCH" : "POST", data);
            if (!savedCompany) savedCompany = saved.id;
            await uploadFiles(savedCompany, pendingFiles);
            haptic("success");
            closeSheet();
            await load();
            feedback(company ? "Профиль сохранён" : "Компания создана", "success");
        });
    });
    showSheet(company ? "Профиль компании" : "Новая компания", form);
}

async function uploadFiles(company, files) {
    for (const file of files) {
        if (!file.size || file.size > 100_000_000) throw new Error("Файл должен быть от 1 байта до 100 МБ.");
        await api("document?company=" + company, "PUT", file);
    }
}

function materials(company) {
    const sectionNode = section("Материалы", screen, "paperclip", state.documents.length,
        "Из них маркетолог берёт факты для постов");
    const labels = {queued:"В очереди", processing:"Изучаю…", ready:"Готово", failed:"Ошибка"};
    const list = el("div", undefined, "list");
    state.documents.forEach((document) => {
        const row = el("div", undefined, "list-item");
        const title = el("span", document.name);
        const chip = el("small", labels[document.status] + (document.error ? " · " + document.error : ""), "doc-status");
        chip.dataset.status = document.status;
        title.append(chip);
        row.append(title);
        if (company.editable) {
            const actions = el("div", undefined, "row");
            const data = {company:company.id, id:document.id};
            if (document.status === "failed") actions.append(button("Повторить", async () => {
                await api("document", "PATCH", data); await load();
            }, "small"));
            if (!(company.preset && document.name.includes("стандартная база знаний"))) {
                const remove = button("", async () => {
                    if (!await confirmAction("Удалить «" + document.name + "» из материалов?")) return;
                    await api("document", "DELETE", data); await load();
                }, "icon-button danger", "trash-2");
                remove.setAttribute("aria-label", "Удалить " + document.name);
                actions.append(remove);
            }
            row.append(actions);
        }
        list.append(row);
    });
    if (state.documents.length) sectionNode.append(list);
    if (!company.editable) return;
    const row = el("div", undefined, "row");
    row.append(button("Загрузить файлы", () => pickFiles(MATERIALS_ACCEPT, true, async (files) => {
        if (!state.storage) throw new Error("Хранилище материалов не подключено.");
        await uploadFiles(company.id, files);
        await load();
        feedback("Файлы загружены — маркетолог их изучит", "success");
    }), "primary", "upload"), button("Сайт компании", () => websiteForm(company), undefined, "globe"));
    sectionNode.append(row, el("small", "PDF, изображения, DOCX, TXT · до 100 МБ на компанию", "hint"));
}

function websiteForm(company) {
    const form = el("form");
    const label = el("label", "Адрес сайта");
    const input = el("input");
    input.type = "url";
    input.inputMode = "url";
    input.placeholder = "https://example.com";
    input.maxLength = 2048;
    input.required = true;
    label.append(input, el("small", "Маркетолог прочитает главные страницы и дополнит профиль", "hint"));
    const submit = el("button", "Изучить сайт", "primary");
    form.append(label, submit);
    form.addEventListener("submit", event => {
        event.preventDefault();
        guarded(submit, async () => {
            await api("website", "POST", {company: company.id, url: input.value.trim()});
            closeSheet();
            await load();
            feedback("Сайт добавлен в очередь", "success");
        });
    });
    showSheet("Сайт компании", form);
}

/* ---------- Чат ---------- */

const CHAT_PROMPTS = [
    "Напиши пост о нашем продукте",
    "Составь контент-план на неделю",
    "Что ты знаешь о нашей компании?",
    "Предложи 5 идей для постов",
];

function chat() {
    const company = state.active;
    const log = chatLogs.get(company) || [];
    chatLogs.set(company, log);
    const messages = el("div", undefined, "messages");
    const form = el("form", undefined, "composer");
    const input = el("textarea");
    input.rows = 1;
    input.placeholder = "Задача для маркетолога…";
    input.setAttribute("aria-label", "Сообщение маркетологу");
    input.maxLength = 4000;
    input.required = true;
    const send = el("button", undefined, "primary send-button");
    send.setAttribute("aria-label", sending ? "Жду ответ…" : "Отправить");
    send.append(icon("send"));
    send.disabled = sending;
    form.append(input, send);

    if (!log.length) {
        const empty = el("div", undefined, "chat-empty");
        const mark = el("span", undefined, "chat-mark");
        mark.append(icon("message-circle"));
        empty.append(mark, el("h2", "Что подготовим?"),
            el("p", "Пишите как сотруднику: «пост про новую серию к пятнице» или «что у нас с расписанием?»"));
        const prompts = el("div", undefined, "chips center");
        for (const prompt of CHAT_PROMPTS) {
            const chip = el("button", prompt, "chip");
            chip.type = "button";
            chip.addEventListener("click", () => { input.value = prompt; form.requestSubmit(); });
            prompts.append(chip);
        }
        empty.append(prompts);
        messages.append(empty);
    }
    for (const message of log) {
        const bubble = el("div", undefined, "bubble" + (message.me ? " me" : ""));
        bubble.append(el("p", message.text));
        const post = message.me ? undefined : mentionedPost(message.text);
        if (post) bubble.append(button("Открыть черновик", () => postSheet(post), "small", "file-pen-line"));
        if (message.proposal) bubble.append(button("Подтвердить", async () => {
            await api("post", "POST", message.proposal);
            delete message.proposal;
            haptic("success");
            await load();
            feedback("Действие выполнено", "success");
        }, "primary small", "check"));
        messages.append(bubble);
    }
    if (sending) {
        const pending = el("div", undefined, "bubble typing");
        pending.setAttribute("role", "status");
        pending.setAttribute("aria-label", "Маркетолог печатает");
        pending.append(el("span"), el("span"), el("span"));
        messages.append(pending);
    }
    const grow = () => {
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 160) + "px";
    };
    input.addEventListener("input", grow);
    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            form.requestSubmit();
        }
    });
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (sending || !input.value.trim()) return;
        const text = input.value.trim();
        sending = true;
        log.push({text, me:true});
        haptic("light");
        chat();
        guarded(send, async () => {
            try {
                const result = await api("chat", "POST", {company, text});
                log.push({text:result.text, proposal:result.proposal});
                await load();
            } finally {
                sending = false;
                if (tab === "chat") chat();
            }
        });
    });
    screen.replaceChildren(messages, form);
    if (log.length) requestAnimationFrame(() => window.scrollTo(0, document.body.scrollHeight));
}

if (initData) guarded(refresh, load);
else {
    screen.replaceChildren(el("p", "Откройте «Маркетолог» через меню бота в Telegram.", "empty"));
    refresh.hidden = true;
}
