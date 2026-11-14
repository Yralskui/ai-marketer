"use strict";
const telegram = window.Telegram?.WebApp;
telegram?.ready();
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
}

document.querySelector("#close").addEventListener("click", () => sheet.close());
sheet.addEventListener("close", () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = undefined;
});
refresh.addEventListener("click", () => guarded(refresh, load));
function openTab(next) {
    tab = next;
    feedback("");
    render();
    if (next !== "chat") window.scrollTo(0, 0);
}

tabs.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-tab]");
    if (!target || !state) return;
    openTab(target.dataset.tab);
});

function render() {
    const company = state.profiles.find((item) => item.id === state.active);
    document.querySelector("#company").textContent = company?.name || "";
    document.querySelector("#company").hidden = !company;
    document.querySelector("#workspace").hidden = !company;
    document.querySelector("#workspace-name").textContent = company?.name || "";
    document.querySelector("#title").textContent = {home:"Публикации", chat:"Чат", profile:"Профиль"}[tab];
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

function home() {
    if (state.stories.length) {
        const stories = el("div", undefined, "stories");
        for (const story of state.stories) {
            const item = button(story.title, () => showSheet(story.title, el("p", story.text)), "story");
            const ring = el("span", undefined, "story-ring");
            ring.append(icon("sparkles"));
            item.prepend(ring);
            stories.append(item);
        }
        section("Обновления").append(stories);
    }
    const hero = el("div", undefined, "hero");
    const start = button("Создать в чате", () => openTab("chat"), "hero-action");
    start.append(icon("arrow-up-right"));
    hero.append(el("h2", "Подготовьте новый пост"), start);
    screen.append(hero);
    const overview = el("div", undefined, "row");
    overview.append(
        button("История публикаций", publicationHistory),
        button("Отчёт", () => reportSheet()),
        button("Аналитика", () => metricsSheet()),
        button("Пул постов", () => poolSheet()),
    );
    screen.append(overview);
    if (state.proposal) {
        const proposal = state.proposal;
        const actions = {publish:"Опубликовать", schedule:"Запланировать", cancel:"Снять с расписания"};
        const request = section("Запрос из Telegram");
        const card = el("div", undefined, "card");
        const when = proposal.when ? " · " + new Date(proposal.when).toLocaleString("ru-RU") : "";
        card.append(el("p", actions[proposal.action] + " «" + proposal.topic + "»" + when));
        card.append(button("Подтвердить действие", async () => {
            await api("post", "POST", proposal);
            await load();
        }, "primary"));
        request.append(card);
    }
    if (state.plans?.length) showPlans();

    const board = el("div", undefined, "board");
    screen.append(board);
    const upcoming = state.posts.filter((post) => post.status === "scheduled");
    upcoming.sort((a, b) => a.when.localeCompare(b.when));
    const ready = state.posts.filter((post) => post.status === "approved");
    const drafts = state.posts.filter((post) => !["scheduled","approved"].includes(post.status));

    const schedule = section("Расписание", board, "calendar-days", upcoming.length);
    if (!upcoming.length) schedule.append(el("p", "Запланированных публикаций пока нет.", "empty"));
    upcoming.forEach((post) => schedule.append(postCard(post)));
    const approved = section("Утверждено", board, "circle-check", ready.length);
    if (!ready.length) approved.append(el("p", "Нет утверждённых постов без даты.", "empty"));
    ready.forEach((post) => approved.append(postCard(post)));
    const proposed = section("Предложения", board, "file-pen-line", drafts.length);
    if (!drafts.length) proposed.append(el("p", "Попросите бота подготовить пост в чате.", "empty"));
    drafts.forEach((post) => proposed.append(postCard(post)));
}

async function publicationHistory() {
    const result = await api("history");
    const body = el("section");
    if (!result.posts.length) body.append(el("p", "История пока пуста.", "empty"));
    for (const post of result.posts) {
        const card = el("article", undefined, "card");
        card.append(el("h3", post.topic || "Пост №" + post.id));
        card.append(el("p", post.published_at ? new Date(post.published_at).toLocaleString("ru-RU") : "Отклонён", "muted"));
        const details = el("details");
        details.append(el("summary", "Текст поста"), el("p", post.text, "post-body"));
        card.append(details);
        if (post.url?.startsWith("https://t.me/")) {
            const link = el("a", "Открыть в Telegram");
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
    const periods = el("div", undefined, "row");
    for (const [hours, label] of [[24, "24 часа"], [168, "7 дней"], [720, "30 дней"]]) {
        periods.append(button(label, () => metricsSheet({hours, post: params.post || 0}),
            Number(params.hours) === hours ? "primary" : undefined));
    }
    body.append(periods);
    if (!data.min_date) {
        body.append(el("p", "Первые данные появятся после сбора метрик подключённого канала."));
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
    if (!data.channels.length) body.append(el("p", "Нет замеров за этот период."));
    for (const channel of data.channels) {
        body.append(el("p", channel.name), metricChart(channel.points, data.timezone));
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
            .catch(error => feedback(error.message)));
        body.append(picker);
    }
    if (Number(params.post)) {
        for (const [key, label] of [["views", "Просмотры"], ["forwards", "Пересылки"]]) {
            body.append(el("h3", label), metricChart(
                data.points.map(point => [point.at, point[key]]), data.timezone
            ));
        }
        const deleted = data.points.find(point => point.deleted);
        if (deleted) body.append(el("p", "Пост удалён из канала."));
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
    const context = canvas.getContext("2d");
    const low = actual.reduce((min, [, value]) => Math.min(min, value), actual[0][1]);
    const high = actual.reduce((max, [, value]) => Math.max(max, value), actual[0][1]);
    const start = Date.parse(actual[0][0]);
    const end = Date.parse(actual.at(-1)[0]);
    const format = new Intl.DateTimeFormat("ru-RU", {
        timeZone: timezone, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    });
    context.font = "18px sans-serif";
    context.fillStyle = "#777";
    context.fillText(String(high), 5, 28);
    if (high !== low) context.fillText(String(low), 5, 197);
    context.fillText(format.format(start), 70, 227);
    if (end !== start) {
        context.textAlign = "right";
        context.fillText(format.format(end), 616, 227);
    }
    context.strokeStyle = "#999";
    context.strokeRect(69, 20, 548, 175);
    context.strokeStyle = context.fillStyle = "#3484ee";
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
    if (!pool.posts.length) body.append(el("p", "Постов пока нет."));
    const statusTitle = {draft:"Черновик", pending_approval:"На согласовании", approved:"Утверждён",
        scheduled:"Запланирован", failed:"Ошибка публикации", published:"Опубликован", rejected:"Отклонён"};
    for (const post of pool.posts) {
        const card = el("article", undefined, "card");
        card.append(el("h3", "№" + post.id + " · " + post.topic),
            el("p", statusTitle[post.status] || post.status, "muted"),
            button("Версии", () => versionSheet(post.id, before)));
        body.append(card);
    }
    if (pool.next) body.append(button("Более ранние посты", () => poolSheet(pool.next)));
    if (before) body.append(button("К последним постам", () => poolSheet()));
    showSheet("Пул постов", body);
}

async function versionSheet(postId, before) {
    const data = await api("versions?id=" + postId);
    const body = el("section");
    body.append(button("К пулу постов", () => poolSheet(before)));
    if (data.plan_item_id) body.append(el("p", "Пункт контент-плана №" + data.plan_item_id));
    const sources = {
        chat: "Запрос в чате", scheduler: "Автономный планировщик", plan: "Контент-план",
        manual: "Правка в Mini App", photo: "Фотобанк", legacy: "Источник неизвестен",
    };
    const decisions = {
        post_approved: "Утверждена", post_published: "Опубликована", post_rejected: "Отклонена",
    };
    if (!data.versions.length) body.append(el("p", "Версий пока нет."));
    for (const version of [...data.versions].reverse()) {
        const card = el("article", undefined, "card");
        card.append(el("h3", "Версия №" + version.id),
            el("p", sources[version.source] || version.source, "muted"),
            el("p", new Date(version.at).toLocaleString("ru-RU"), "muted"));
        if (version.parent_id) card.append(el("p", "На основе версии №" + version.parent_id));
        if (version.request) card.append(el("p", "Запрос · " + version.request, "post-body"));
        if (version.instruction) card.append(el("p", "Изменение · " + version.instruction));
        for (const decision of version.decisions) {
            card.append(el("p", (decisions[decision.type] || decision.type) + " · " +
                new Date(decision.at).toLocaleString("ru-RU")));
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
        }));
        if (version.image_model) card.append(el("p", "Модель изображения · " + version.image_model));
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
    const periods = el("div", undefined, "row");
    for (const period of [7, 30, 90]) {
        const item = button(period + " дн.", () => reportSheet(period), period === days ? "primary" : undefined);
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
        for (const post of report.posts) {
            const row = el("div", undefined, "row");
            const counters = [
                post.views == null ? "" : `Просмотры: ${post.views}`,
                post.forwards == null ? "" : `Пересылки: ${post.forwards}`,
            ];
            row.append(el("strong", post.topic), el("span", counters.filter(Boolean).join(" · ")));
            body.append(row);
        }
    }
    showSheet("Отчёт", body);
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
    await api("post", "POST", {id: post.id, company: state.active, action, version: post.version, ...fields});
    sheet.close();
    await load();
}

function postCard(post) {
    const card = el("article", undefined, "card post-card");
    const labels = {draft:"Черновик", pending_approval:"На согласовании", approved:"Утверждено", scheduled:"Запланировано", failed:"Ошибка публикации"};
    const status = el("p", labels[post.status], "status");
    status.dataset.status = post.status;
    card.append(status, el("h3", post.topic || "Пост №" + post.id));
    if (post.when) {
        const date = el("p", new Date(post.when).toLocaleString("ru-RU"), "post-date");
        date.prepend(icon("calendar-days"));
        card.append(date);
    }
    card.append(el("p", post.text.length > 240 ? post.text.slice(0, 240) + "…" : post.text, "post-excerpt"));
    const open = button("Открыть пост", () => postSheet(post), "text-button");
    open.append(icon("chevron-right"));
    card.append(open);
    return card;
}

function postSheet(post) {
    const content = el("section");
    content.append(el("p", post.text, "post-body"));
    const actions = el("div", undefined, "row");
    const approved = ["scheduled", "approved"].includes(post.status);
    if (approved) actions.append(button("Снять с расписания", () => postAction(post, "cancel"), "danger"));
    else actions.append(button("Удалить предложение", () => postAction(post, "reject"), "danger"));
    if (post.status === "pending_approval") actions.append(button("Утвердить", () => postAction(post, "approve")));
    if (post.status !== "scheduled" && post.status !== "draft") {
        actions.append(button("Опубликовать", () => {
            if (window.confirm("Опубликовать этот пост сейчас?")) return postAction(post, "publish");
        }, "primary"));
    }
    content.append(actions);
    const revision = el("textarea");
    revision.placeholder = "Что изменить в посте?";
    revision.setAttribute("aria-label", "Правки к посту");
    content.append(revision, button("Внести правки", () => postAction(post, "revise", {instruction: revision.value})));
    const company = state.profiles.find(item => item.id === state.active);
    if (company?.editable) {
        const photo = el("input");
        photo.type = "file";
        photo.accept = "image/jpeg,image/png,image/webp";
        const label = el("label", "Своё фото для поста");
        label.append(photo);
        content.append(label, button("Заменить изображение", async () => {
            const file = photo.files[0];
            if (!file) throw new Error("Выберите изображение.");
            await api("post?id=" + post.id + "&version=" + encodeURIComponent(post.version), "PUT", file);
            sheet.close();
            await load();
        }));
        if (post.has_image) content.append(button("Убрать изображение", () => postAction(post, "remove_photo")));
    }
    if (["pending_approval", "approved", "scheduled"].includes(post.status)) {
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
    }
    showSheet(post.topic || "Пост", content);
    if (post.has_image) {
        const image = el("img", undefined, "post-preview");
        image.alt = "Иллюстрация к посту";
        const caption = content.firstChild;
        fetch("/miniapp/api/preview?id=" + post.id, {headers:{Authorization:"Telegram " + initData}})
            .then((response) => response.ok ? response.blob() : null)
            .then((blob) => {
                if (!blob || !sheet.open || !content.isConnected) return;
                if (previewUrl) URL.revokeObjectURL(previewUrl);
                previewUrl = URL.createObjectURL(blob);
                image.src = previewUrl;
                caption.before(image);
            }).catch(() => {});
    }
}

function profile(company) {
    const identity = el("div", undefined, "card identity");
    const fullName = [state.user.first_name, state.user.last_name].filter(Boolean).join(" ");
    const avatar = el("span", fullName.slice(0, 1).toUpperCase() || "Т", "avatar");
    if (state.user.photo_url?.startsWith("https://")) {
        const image = el("img", undefined, "avatar");
        image.alt = "";
        image.src = state.user.photo_url;
        image.onerror = () => image.replaceWith(avatar);
        identity.append(image);
    } else identity.append(avatar);
    const account = el("div");
    account.append(el("h2", fullName || "Профиль Telegram"));
    if (state.user.username) account.append(el("p", "@" + state.user.username, "muted"));
    identity.append(account);
    screen.append(identity);
    const current = section("Текущая компания", screen, "building-2");
    const currentCard = el("div", undefined, "card current-company");
    currentCard.append(el("h3", company?.name || "Профиль не выбран"));
    if (company?.channel) currentCard.append(el("p", company.channel, "muted"));
    current.append(currentCard);
    if (company?.editable) {
        const actions = el("div", undefined, "row");
        actions.append(button("Редактировать", () => profileForm(company)));
        actions.append(button("Удалить профиль", async () => {
            if (!window.confirm("Удалить профиль и его материалы?")) return;
            await api("profile", "DELETE", {company:company.id});
            await load();
        }, "danger"));
        currentCard.append(actions);
    }
    const profiles = section("Профили компаний", screen, "layers");
    const list = el("div", undefined, "list");
    state.profiles.forEach((item) => {
        const row = el("div", undefined, "list-item");
        row.append(el("span", item.name));
        if (item.id === state.active) row.append(el("span", "Выбран", "status"));
        else row.append(button("Выбрать", async () => {
            await api("select", "POST", {id:item.id});
            await load();
        }));
        list.append(row);
    });
    if (state.profiles.length) profiles.append(list);
    const buttons = el("div", undefined, "row");
    const create = button("Создать профиль", () => profileForm(), "primary");
    create.prepend(icon("plus"));
    buttons.append(create);
    if (company) buttons.append(button("Снять выбор", async () => {
        await api("select", "POST", {id:null});
        await load();
    }, "plain"));
    profiles.append(buttons);
    if (company) materials(company);
    if (company?.editable && state.management) management(company);
}

function management(company) {
    const data = state.management;
    const operations = section("Управление компанией");
    operations.append(button("Фотобанк", () => photoLibrary()));
    operations.append(button("Комментарии", () => commentsSheet(company, data)));
    operations.append(button("Диагностика", async () => {
        const info = await api("diagnostics");
        const body = el("section");
        body.append(el("p", info.dry_run ? "Тестовый режим публикации" : "Публикация включена"));
        body.append(el("p", info.channel || "Канал не подключён", "muted"));
        if (info.channel_check) body.append(el("p", info.channel_check.message));
        for (const job of info.jobs) body.append(el("p", job.name + " · " + job.next, "muted"));
        showSheet("Диагностика", body);
    }));
    operations.append(button("Расходы и лимит", () => {
        const form = el("form");
        form.append(el("p", "За месяц: " + Number(data.spend).toFixed(2) + " ₽"));
        const label = el("label", "Месячный лимит в рублях");
        const input = el("input");
        input.type = "number";
        input.min = "0.01";
        input.max = "1000000";
        input.step = "0.01";
        input.value = data.budget ?? "";
        input.placeholder = "Без лимита";
        label.append(input);
        const save = el("button", "Сохранить", "primary");
        form.append(label, save);
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            guarded(save, async () => {
                await api("settings", "POST", {company: company.id, action:"budget", value:input.value ? Number(input.value) : null});
                sheet.close();
                await load();
            });
        });
        showSheet("Расходы и лимит", form);
    }));
    operations.append(button("Журнал действий", () => {
        const rows = el("section");
        for (const event of data.events) {
            const row = el("div", undefined, "card");
            row.append(el("p", event.text || event.type), el("p", new Date(event.date).toLocaleString("ru-RU"), "muted"));
            rows.append(row);
        }
        if (!data.events.length) rows.append(el("p", "Событий пока нет.", "empty"));
        showSheet("Журнал действий", rows);
    }));
    if (company.invite_enabled) operations.append(button("Приглашение", () => {
        const body = el("section");
        if (data.invite) {
            const link = el("input");
            link.value = data.invite;
            link.readOnly = true;
            link.setAttribute("aria-label", "Ссылка-приглашение");
            body.append(link);
        }
        body.append(button("Создать новую ссылку", async () => {
            await api("settings", "POST", {company: company.id, action:"invite"});
            await load();
            sheet.close();
            feedback("Ссылка обновлена. Откройте «Приглашение».");
        }));
        showSheet("Приглашение", body);
    }));
    if (company.invite_enabled && data.clients.length) operations.append(button("Доступ клиентов", () => {
        const body = el("section");
        for (const client of data.clients) {
            const card = el("div", undefined, "card");
            card.append(el("p", client.name), button(client.active ? "Отключить доступ" : "Вернуть доступ", async () => {
                await api("settings", "POST", {company:company.id, action:"client_access", id:client.id, active:!client.active});
                sheet.close();
                await load();
            }));
            body.append(card);
        }
        showSheet("Доступ клиентов", body);
    }));
}

function photoLibrary() {
    const body = el("section");
    const file = el("input");
    file.type = "file";
    file.accept = "image/jpeg,image/png,image/webp";
    const label = el("label", "Добавить фото");
    label.append(file);
    body.append(label, button("Загрузить фото", async () => {
        if (!file.files.length) throw new Error("Выберите изображение.");
        await api("photo", "PUT", file.files[0]);
        await load();
        photoLibrary();
    }));
    for (const photo of state.management.photos || []) {
        const row = el("div", undefined, "list-item");
        row.append(el("span", photo.caption || "Фото №" + photo.id));
        row.append(button("Открыть фото", () => openLibraryPhoto(photo)));
        body.append(row);
    }
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
    const label = el("label", "Тема поста");
    const topic = el("input");
    topic.required = true;
    topic.maxLength = 512;
    topic.value = photo.caption.slice(0, 512);
    label.append(topic);
    const submit = el("button", "Создать черновик с фото", "primary");
    form.append(image, label, submit);
    form.addEventListener("submit", event => {
        event.preventDefault();
        guarded(submit, async () => {
            await api("photo?id=" + photo.id, "POST", {topic: topic.value});
            sheet.close();
            await load();
            feedback("Черновик с фото готов к согласованию.");
        });
    });
    showSheet("Фото компании", form);
}

function commentsSheet(company, data) {
    const body = el("section");
    body.append(button(data.auto_reply ? "Выключить автоответы" : "Включить автоответы", async () => {
        await api("settings", "POST", {company: company.id, action:"auto_reply", enabled:!data.auto_reply});
        await load();
        commentsSheet(company, state.management);
    }));
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
                await load();
                commentsSheet(company, state.management);
            };
            card.append(input, button("Отправить ответ", () => update("send"), "primary"),
                button("Пропустить", () => update("skip")));
        } else if (item.reply && item.status === "sent") card.append(el("p", item.reply, "muted"));
        body.append(card);
    }
    if (!data.comments.length) body.append(el("p", "Комментариев пока нет.", "empty"));
    showSheet("Комментарии", body);
}

const profileFields = {
    name:"Название", description:"Чем занимается", products:"Продукты и услуги",
    audience:"Аудитория", usp:"Что отличает компанию", tone:"Тон общения",
    visual_style:"Визуальный стиль", restrictions:"Ограничения", channel:"Telegram-канал",
};

function profileForm(company) {
    const form = el("form");
    for (const [key, label] of Object.entries(profileFields)) {
        const field = el("label", label);
        const input = el(key === "description" || key === "visual_style" ? "textarea" : "input");
        input.name = key;
        const value = key === "channel" ? company?.channel : company?.details[key];
        input.value = Array.isArray(value) ? value.join(", ") : value || "";
        input.maxLength = key === "name" ? 255 : 2000;
        input.required = key === "name";
        field.append(input);
        form.append(field);
    }
    const references = el("input");
    references.type = "file";
    references.multiple = true;
    references.accept = ".pdf,.png,.jpg,.jpeg,.webp,.docx,.txt,.md,.csv,.json,.rtf";
    const fileField = el("label", "Документы и визуальные референсы");
    fileField.append(references);
    form.append(fileField);
    let savedCompany = company?.id;
    const submit = el("button", "Сохранить профиль", "primary");
    form.append(submit);
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        guarded(submit, async () => {
            const data = Object.fromEntries(new FormData(form));
            if (savedCompany) data.company = savedCompany;
            if (references.files.length && !state.storage) throw new Error("Хранилище материалов не подключено.");
            const saved = await api(savedCompany ? "profile" : "profiles", savedCompany ? "PATCH" : "POST", data);
            if (!savedCompany) savedCompany = saved.id;
            await uploadFiles(savedCompany, references.files);
            sheet.close();
            await load();
        });
    });
    showSheet(company ? "Редактировать профиль" : "Новая компания", form);
}

async function uploadFiles(company, files) {
    for (const file of files) {
        if (!file.size || file.size > 100_000_000) throw new Error("Файл должен быть от 1 байта до 100 МБ.");
        await api("document?company=" + company, "PUT", file);
    }
}

function materials(company) {
    const sectionNode = section("Материалы", screen, "paperclip");
    const list = el("div", undefined, "list");
    const labels = {queued:"В очереди", processing:"Обрабатывается", ready:"Готово", failed:"Ошибка"};
    state.documents.forEach((document) => {
        const row = el("div", undefined, "list-item");
        const title = el("span", document.name);
        title.append(el("small", labels[document.status] + (document.error ? " · " + document.error : "")));
        row.append(title);
        if (company.editable) {
            const actions = el("div", undefined, "row");
            const data = {company:company.id,id:document.id};
            if (document.status === "failed") actions.append(button("Повторить", async () => {
                await api("document", "PATCH", data); await load();
            }));
            actions.append(button("Убрать", async () => {
                if (!window.confirm("Удалить этот файл и связанные фрагменты?")) return;
                await api("document", "DELETE", data); await load();
            }, "danger"));
            row.append(actions);
        }
        list.append(row);
    });
    if (state.documents.length) sectionNode.append(list);
    if (!company.editable) return;
    const upload = el("input");
    upload.type = "file";
    upload.multiple = true;
    upload.accept = ".pdf,.png,.jpg,.jpeg,.webp,.docx,.txt,.md,.csv,.json,.rtf";
    const field = el("label", "PDF, изображения и текстовые документы · до 100 МБ на профиль");
    field.append(upload);
    sectionNode.append(field, button("Загрузить материалы", async () => {
        if (!upload.files.length) throw new Error("Выберите файл.");
        if (!state.storage) throw new Error("Хранилище материалов не подключено.");
        await uploadFiles(company.id, upload.files);
        await load();
        feedback("Материалы добавлены в очередь.");
    }, "primary"));
    sectionNode.append(button("Добавить сайт компании", () => websiteForm(company)));
}

function websiteForm(company) {
    const form = el("form");
    const label = el("label", "Адрес сайта");
    const input = el("input");
    input.type = "url";
    input.placeholder = "https://example.com";
    input.maxLength = 2048;
    input.required = true;
    label.append(input);
    const submit = el("button", "Импортировать", "primary");
    form.append(label, submit);
    form.addEventListener("submit", event => {
        event.preventDefault();
        guarded(submit, async () => {
            await api("website", "POST", {company: company.id, url: input.value.trim()});
            sheet.close();
            await load();
            feedback("Сайт добавлен в очередь обработки.");
        });
    });
    showSheet("Добавить сайт компании", form);
}

function chat() {
    const company = state.active;
    const log = chatLogs.get(company) || [];
    chatLogs.set(company, log);
    const messages = el("div", undefined, "messages");
    if (!log.length) {
        const empty = el("div", undefined, "chat-empty");
        const mark = el("span", undefined, "chat-mark");
        mark.append(icon("message-circle"));
        empty.append(mark, el("h2", "Что подготовим?"),
            el("p", "Попросите написать пост или задайте вопрос о компании."));
        messages.append(empty);
    }
    for (const message of log) {
        const bubble = el("div", undefined, "bubble" + (message.me ? " me" : ""));
        bubble.append(el("p", message.text));
        if (message.proposal) bubble.append(button("Подтвердить", async () => {
            await api("post", "POST", message.proposal);
            delete message.proposal;
            await load();
            feedback("Действие выполнено.");
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
    send.append(icon("send"));
    send.disabled = sending;
    form.append(input, send);
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (sending || !input.value.trim()) return;
        const text = input.value.trim();
        sending = true;
        log.push({text, me:true});
        chat();
        guarded(send, async () => {
            try {
                const result = await api("chat", "POST", {company,text});
                log.push({text:result.text,proposal:result.proposal});
                await load();
            } finally {
                sending = false;
                if (tab === "chat") chat();
            }
        });
    });
    if (sending) {
        const pending = el("p", "Готовлю ответ…", "chat-pending");
        pending.setAttribute("role", "status");
        messages.append(pending);
    }
    screen.replaceChildren(messages, form);
    if (log.length) requestAnimationFrame(() => window.scrollTo(0, document.body.scrollHeight));
}

if (initData) guarded(refresh, load);
else {
    screen.replaceChildren(el("p", "Откройте «Маркетолог» через меню бота в Telegram.", "empty"));
    refresh.hidden = true;
}
