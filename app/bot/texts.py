"""Все тексты бота в одном месте. Разметка — HTML; пользовательский/LLM-текст экранируем."""

from __future__ import annotations

from datetime import tzinfo
from html import escape

from app.db.models import Post, PostStatus
from app.services.business_profile import PROFILE_FIELD_TITLES, BusinessProfile
from app.services.posts import PLATFORM_TITLES, STATUS_TITLES
from app.utils.time import format_local

# ---------------------------------------------------------------- общее

START = (
    "👋 Привет, {name}! Я — ваш AI-маркетолог.\n\n"
    "Я изучу ваш бизнес, буду готовить посты и картинки, а публиковать — только после "
    "вашего согласия.\n\n"
    "{next_step}"
)
START_NEED_PROFILE = "Начнём со знакомства: нажмите /onboarding и расскажите о бизнесе."
START_READY = "Просто напишите задачу, например: <i>«напиши пост про осеннее меню»</i>."

HELP = (
    "<b>Что я умею</b>\n"
    "• Основные действия — на кнопках под полем ввода.\n"
    "• Пишите задачи обычным языком: <i>«напиши пост про скидки на выходных с картинкой»</i>, "
    "<i>«сделай пост #3 короче»</i>, <i>«покажи мои посты»</i>.\n"
    "• Каждый пост сначала приходит черновиком с кнопками — без вашего «Опубликовать» "
    "ничего не выйдет.\n\n"
    "<b>Команды</b>\n"
    "/onboarding — рассказать о бизнесе (текст, файлы PDF/DOCX/TXT, ссылка на сайт)\n"
    "/profile — профиль бизнеса\n"
    "/posts — последние посты и статусы\n"
    "/post N — открыть пост №N\n"
    "/channel — Telegram-канал для публикаций\n"
    "/status — диагностика\n"
    "/cancel — отменить текущее действие"
)
CANCELLED = "Отменено. Чем ещё помочь?"
NOTHING_TO_CANCEL = "Нечего отменять 🙂"
ACCESS_DENIED = (
    "⛔ У вас нет доступа к этому боту.\n"
    "Попросите у администратора ссылку-приглашение для вашей компании.\n"
    "Ваш Telegram ID: <code>{user_id}</code>."
)
UNKNOWN_COMMAND = "Не знаю такой команды. Список команд — /help"
UNSUPPORTED_MESSAGE = "Я понимаю текст и документы (PDF, DOCX, TXT). Подробнее — /help"
DOCUMENT_OUTSIDE_ONBOARDING = (
    "Чтобы я изучил документ и добавил его в профиль бизнеса, сначала нажмите /onboarding."
)
AGENT_ERROR = "😔 Не получилось связаться с AI-моделью. Попробуйте ещё раз чуть позже."

# ---------------------------------------------------------------- онбординг

ONBOARDING_INTRO = (
    "Расскажите о вашем бизнесе — можно несколькими сообщениями:\n"
    "• текстом: чем занимаетесь, что продаёте, для кого, чем отличаетесь, каким тоном "
    "общаться и о чём нельзя писать;\n"
    "• файлом: PDF, DOCX или TXT (презентация, меню, прайс);\n"
    "• ссылкой на сайт.\n\n"
    "Когда закончите — нажмите «Готово» или /done."
)
ONBOARDING_TEXT_SAVED = "✍️ Записал. Ещё что-нибудь? Когда закончите — «Готово»."
ONBOARDING_DOC_SAVED = "📄 Прочитал «{name}» ({chars} символов). Ещё материалы или «Готово»?"
ONBOARDING_SITE_LOADING = "🌐 Читаю сайт {url} …"
ONBOARDING_SITE_SAVED = (
    "🌐 Прочитал сайт: {pages} стр., {chars} символов. Ещё что-нибудь или «Готово»?"
)
ONBOARDING_SITE_ERROR = "Не получилось прочитать сайт: {error}"
ONBOARDING_DOC_ERROR = "Не получилось прочитать файл: {error}"
ONBOARDING_DOC_TOO_BIG = "Файл больше 20 МБ — Telegram не даёт боту его скачать."
ONBOARDING_BUILDING = "🧠 Изучаю материалы и составляю профиль бизнеса…"
ONBOARDING_NO_SOURCES = (
    "Пока нет ни одного материала — расскажите о бизнесе текстом, файлом или ссылкой."
)
ONBOARDING_DONE = (
    "Готово! Вот что я понял о бизнесе:\n\n{profile}\n\nТеперь можно ставить задачи текстом."
)
ONBOARDING_FAILED = "Не получилось составить профиль: {error}"

PROFILE_EMPTY = "Профиль бизнеса пока пуст. Нажмите /onboarding, чтобы рассказать о себе."

# ---------------------------------------------------------------- черновики и согласование

DRAFT_HEADER = "📝 <b>Черновик #{id}</b> · {platform}{revision}\n\n"
DRAFT_TRUNCATED = "\n\n<i>…текст сокращён в превью, опубликуется целиком</i>"
POST_NOT_FOUND = "Пост не найден."
POST_WRONG_STATUS = "Пост #{id} уже {status} — это действие недоступно."
ASK_REVISION = "✏️ Напишите, что изменить в посте #{id}. Например: «короче и добавь эмодзи»."
REVISING = "✏️ Переписываю…"
ASK_SCHEDULE_TIME = (
    "🕒 Когда опубликовать пост #{id}? Напишите обычным языком: "
    "«завтра в 10 утра», «в пятницу в 18:00», «25.11 12:30».\nЧасовой пояс: {tz}."
)
SCHEDULE_PARSE_ERROR = (
    "Не понял время ({error}). Попробуйте, например: «завтра в 10:00». /cancel — отмена."
)
SCHEDULED_OK = "🕒 Пост #{id} одобрен и запланирован на <b>{when}</b>."
REJECTED_OK = "❌ Пост #{id} отклонён."
PUBLISHING = "🚀 Публикую…"
PUBLISHED_OK = "✅ Пост #{id} опубликован."
PUBLISHED_DRY_RUN = "✅ Пост #{id} «опубликован» в режиме DRY-RUN (в канал ничего не ушло)."
PUBLISH_FAILED = (
    "⚠️ Не удалось опубликовать пост #{id}: {error}\nМожно попробовать снова: /post {id}"
)

POSTS_EMPTY = "Постов пока нет. Напишите, например: «напиши пост про новинки»."
POSTS_HEADER = "<b>Последние посты</b>\n"
POST_USAGE = "Укажите номер: /post 3"


def start(name: str, profile_filled: bool) -> str:
    return START.format(
        name=escape(name or "друг"),
        next_step=START_READY if profile_filled else START_NEED_PROFILE,
    )


def profile_card(profile: BusinessProfile) -> str:
    lines = []
    for key, title in PROFILE_FIELD_TITLES.items():
        value = getattr(profile, key)
        if isinstance(value, list):
            value = "; ".join(value)
        lines.append(f"<b>{title}:</b> {escape(value) if value else '—'}")
    return "\n".join(lines)


def draft_card(post: Post, revision_no: int | None = None) -> str:
    header = DRAFT_HEADER.format(
        id=post.id,
        platform=PLATFORM_TITLES.get(post.platform, str(post.platform)),
        revision=f" · версия {revision_no}" if revision_no and revision_no > 1 else "",
    )
    body = escape(post.text)
    limit = 4096 - len(header) - len(DRAFT_TRUNCATED) - 16
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0] + DRAFT_TRUNCATED
    return header + body


def post_line(post: Post, tz: tzinfo) -> str:
    status = STATUS_TITLES.get(PostStatus(post.status), str(post.status))
    line = f"<b>#{post.id}</b> {status} — {escape(post.topic[:60] or post.text[:60])}"
    if post.status == PostStatus.SCHEDULED:
        line += f" · на {format_local(post.scheduled_at, tz)}"
    if post.status == PostStatus.PUBLISHED and post.external_url:
        line += f' · <a href="{escape(post.external_url)}">ссылка</a>'
    if post.status == PostStatus.FAILED and post.error:
        line += f"\n    <i>{escape(post.error[:120])}</i>"
    return line


def posts_list(posts: list[Post], tz: tzinfo) -> str:
    if not posts:
        return POSTS_EMPTY
    return POSTS_HEADER + "\n".join(post_line(p, tz) for p in posts) + "\n\nОткрыть пост: /post N"


def status_report(
    *,
    company: str,
    llm: str,
    model: str,
    images: str,
    dry_run: bool,
    channel: str,
    counts: dict[PostStatus, int],
    jobs: list[tuple[str, str]],
    tools: int,
) -> str:
    counts_text = (
        "\n".join(f"  {STATUS_TITLES[s]}: {n}" for s, n in counts.items() if n)
        or "  постов пока нет"
    )
    jobs_text = "\n".join(f"  • {escape(t)} — {escape(n)}" for t, n in jobs) or "  нет задач"
    return (
        "<b>Диагностика</b>\n"
        f"LLM: <code>{escape(llm)}</code> ({escape(model)})\n"
        f"Картинки: <code>{escape(images)}</code>\n"
        f"Инструментов у агента: {tools}\n"
        f"Режим DRY-RUN: {'включён — в канал ничего не уходит' if dry_run else 'выключен'}\n"
        f"Компания: {company}\n"
        f"Telegram-канал: {channel}\n\n"
        f"<b>Посты</b>\n{counts_text}\n\n"
        f"<b>Планировщик</b>\n{jobs_text}"
    )


# --- поток B ---------------------------------------------------- компании и каналы

START_JOINED = "🤝 Вы подключены к компании <b>{company}</b>.\n\n"
START_ADMIN = (
    "\n\n🛠 Вы администратор. Текущая компания: <b>{company}</b> (#{id}).\n"
    "/companies — все компании, /new_company — добавить, /invite — ссылка для клиента."
)
ADMIN_ONLY = "Эта команда доступна только администратору."
COMPANY_CREATED = (
    "🏢 Компания <b>{company}</b> (#{id}) создана, вы переключены на неё.\n\n"
    "Дальше:\n"
    "1. /onboarding — загрузите сведения о компании (текст, файлы, сайт).\n"
    "2. /channel @канал — подключите Telegram-канал компании "
    "(бот должен быть в нём администратором).\n"
    "3. Отправьте клиенту ссылку-приглашение:\n{link}"
)
COMPANY_SWITCHED = (
    "Теперь вы работаете с компанией <b>{company}</b> (#{id}). "
    "Посты, профиль и диалог — только этой компании."
)
COMPANIES_HEADER = "<b>Компании</b>\n"
COMPANIES_FOOTER = "\n\nПереключиться — кнопкой ниже или /use N. Новая — /new_company Название"
USE_USAGE = "Укажите номер компании: /use 2. Список — /companies"
INVITE = (
    "🔗 Ссылка-приглашение для клиента компании <b>{company}</b>:\n{link}\n\n"
    "Клиент откроет её, нажмёт «Запустить» и получит доступ только к этой компании.\n"
    "Новая ссылка (старая перестанет работать): /invite new"
)
CHANNEL_CURRENT = (
    "Канал компании <b>{company}</b>: {channel}\n\n"
    "Сменить: /channel @username или /channel -100XXXXXXXXXX.\n"
    "Бот должен быть администратором канала с правом публиковать сообщения."
)
CHANNEL_NOT_SET = "не подключён"
CHANNEL_SAVED = "📣 Канал {channel} подключён к компании <b>{company}</b>."
CHANNEL_CHECK = "\nПроверка: {mark} {message}"


def start_for(
    name: str, profile_filled: bool, company: str, business_id: int, *, joined: bool, admin: bool
) -> str:
    text = start(name, profile_filled)
    if joined:
        text = START_JOINED.format(company=escape(company)) + text
    if admin:
        text += START_ADMIN.format(company=escape(company), id=business_id)
    return text


def company_line(
    business_id: int, name: str, clients: list[str], channel: str | None, current: bool
) -> str:
    mark = "👉 " if current else ""
    who = ", ".join(escape(c) for c in clients) if clients else "клиент не подключён"
    where = f"<code>{escape(channel)}</code>" if channel else "канал не задан"
    return f"{mark}<b>#{business_id}</b> {escape(name)} — {who}; {where}"


# --- поток B ---------------------------------------------------- меню и описание бота

# Показывается в пустом чате до «Запустить» (setMyDescription, до 512 символов)
BOT_DESCRIPTION = (
    "Я — AI-маркетолог для вашего бизнеса.\n\n"
    "✦ Изучу компанию по рассказу, документам и сайту\n"
    "✦ Напишу посты и нарисую картинки в стиле бренда\n"
    "✦ Пришлю черновик на согласование и опубликую только после вашего «Да»\n"
    "✦ Сам работаю по расписанию: черновики, публикации, отчёты\n\n"
    "Нажмите «Запустить», чтобы начать."
)
# Профиль бота и превью ссылки (setMyShortDescription, до 120 символов)
BOT_SHORT_DESCRIPTION = (
    "AI-маркетолог 24/7: посты и картинки для вашего канала — публикую только с вашего согласия."
)

ASK_POST_TOPIC = (
    "✍️ О чём пост? Опишите обычным языком, например:\n"
    "<i>«скидка 20% на выходных, с картинкой»</i> или <i>«новинка в меню — тыквенный латте»</i>."
)
POST_TOPIC_PREFIX = "Напиши пост: "
ASK_COMPANY_NAME = "🏢 Как называется новая компания? Например: <i>Цветочный салон «Роза»</i>."


# --- поток B ---------------------------------------------------- расходы на AI

COSTS_HEADER = "💰 <b>Расходы на AI</b> с {since}\n"
COSTS_ESTIMATED = "\n<i>~ — часть токенов оценена по длине текста.</i>"
COSTS_FOOTER_ADMIN = (
    "\n\nЛимит текущей компании: /budget 500 (₽ в месяц), снять — /budget off.\n"
    "Цены: LLM_PRICE_INPUT_PER_1M / LLM_PRICE_OUTPUT_PER_1M / IMAGE_PRICE в .env."
)
BUDGET_USAGE = "Укажите лимит в рублях на месяц: /budget 500. Снять лимит: /budget off"
BUDGET_SET = "💰 Лимит для <b>{company}</b>: {budget} ₽ в месяц. Потрачено: {spent:.2f} ₽."
BUDGET_OFF = "💰 Лимит для <b>{company}</b> снят."


def cost_line(
    business_id: int,
    name: str,
    *,
    calls: int,
    tokens: int,
    images: int,
    cost: float,
    budget: float | None,
    estimated: bool,
    current: bool = False,
) -> str:
    mark = "👉 " if current else ""
    approx = "~" if estimated else ""
    limit = f" из {budget:.0f} ₽" if budget is not None else ""
    warn = " ⛔" if budget is not None and cost >= budget else ""
    tokens_text = f"{tokens:,}".replace(",", " ")
    return (
        f"{mark}<b>#{business_id}</b> {escape(name)}: <b>{cost:.2f} ₽</b>{limit}{warn}\n"
        f"    запросов к AI: {calls}, токенов: {approx}{tokens_text}, картинок: {images}"
    )
