"""Все тексты бота в одном месте. Разметка — HTML; пользовательский/LLM-текст экранируем."""

from __future__ import annotations

from datetime import tzinfo
from html import escape

from app.db.models import Comment, CommentStatus, Post, PostStatus
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
    "/comments — комментарии под постами и ответы на них\n"
    "/report — отчёт: показатели, выводы и что сделать\n"
    "/photos — фотобанк: свои фото для постов (просто пришлите фото боту)\n"
    "/log — журнал действий по компании\n"
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
ONBOARDING_DOC_SAVED = "📄 Сохранил «{name}» ({size} байт)."
ONBOARDING_SITE_LOADING = "🌐 Читаю сайт {url} …"
ONBOARDING_SITE_SAVED = (
    "🌐 Прочитал сайт: {pages} стр., {chars} символов. Ещё что-нибудь или «Готово»?"
)
ONBOARDING_SITE_ERROR = "Не получилось прочитать сайт: {error}"
ONBOARDING_DOC_ERROR = "Не получилось прочитать файл: {error}"
UPLOAD_LINK = (
    'Для файла больше 20 МБ откройте <a href="{link}">форму загрузки</a>. '
    "Ссылка действует 10 минут и рассчитана на одну попытку. "
    "После загрузки вернитесь сюда. Новая ссылка — /upload."
)
UPLOAD_DISABLED = (
    "Telegram не скачивает файлы больше 20 МБ. "
    "Веб-загрузка пока не настроена. Пришлите документ частями до 20 МБ."
)
UPLOAD_QUOTA = "Документ не помещается в общий лимит профиля в 100 МБ."
UPLOAD_UNSUPPORTED = "Для визуалов используйте PDF, PNG, JPG или WebP. Презентации экспортируйте в PDF. Для текста подойдут DOCX, TXT, MD, CSV, JSON и RTF."
UPLOAD_EMPTY = "Файл пуст."
UPLOAD_SAVED = "📄 Документ из браузера добавлен к профилю: «{name}»."
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


def draft_card(
    post: Post,
    revision_no: int | None = None,
    *,
    channel: str | None = None,
    tz: tzinfo | None = None,
) -> str:
    """Карточка на согласование: материал, площадка и время публикации (п. 3.5 ТЗ)."""
    header = DRAFT_HEADER.format(
        id=post.id,
        platform=PLATFORM_TITLES.get(post.platform, str(post.platform)),
        revision=f" · версия {revision_no}" if revision_no and revision_no > 1 else "",
    )
    where = f"<code>{escape(channel)}</code>" if channel else "канал не подключён — /channel"
    if post.scheduled_at and tz is not None:
        when = f"запланировано на <b>{format_local(post.scheduled_at, tz)}</b>"
    else:
        when = "сразу после «Опубликовать» или в выбранное вами время"
    header += f"📣 Куда: {where}\n🕒 Когда: {when}\n\n"
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


# --- поток B ---------------------------------------------------- комментарии

COMMENT_NOT_FOUND = "Комментарий не найден."
COMMENT_SENT = "✅ Ответ опубликован под комментарием."
COMMENT_SKIPPED = "Пропустил — отвечать не будем."
COMMENT_ALREADY_DONE = "На этот комментарий уже ответили или его пропустили."
COMMENT_SEND_FAILED = (
    "⚠️ Не получилось ответить: проверьте, что бот — администратор группы обсуждения."
)
ASK_COMMENT_REPLY = "✏️ Напишите свой ответ — я опубликую его под комментарием как есть."
AUTOREPLY_USAGE = (
    "Автоответ на комментарии: /autoreply on — включить, /autoreply off — выключить.\n"
    "Даже с автоответом жалобы, негатив и вопросы о деньгах придут вам на согласование."
)
AUTOREPLY_ON = (
    "🤖 Автоответ для <b>{company}</b> включён: на обычные комментарии я отвечаю сам и "
    "сообщаю вам. Жалобы и спорные вопросы по-прежнему приходят на согласование."
)
AUTOREPLY_OFF = (
    "🤖 Автоответ для <b>{company}</b> выключен: каждый ответ — после вашего «Отправить»."
)
COMMENTS_SETUP = (
    "\n\n<b>Как подключить комментарии</b>\n"
    "1. Канал → Управление каналом → Обсуждение → привяжите группу.\n"
    "2. Добавьте бота в эту группу администратором.\n"
    "3. Как только под постом появится комментарий, я пришлю черновик ответа."
)
COMMENT_STATUS_ICONS = {
    CommentStatus.NEW: "🆕",
    CommentStatus.PENDING: "⏳",
    CommentStatus.SENT: "✅",
    CommentStatus.SKIPPED: "🙈",
}


def comment_card(comment: Comment, company: str, post: Post | None) -> str:
    where = f"к посту #{post.id}" if post else "в обсуждении канала"
    text = (
        f"💬 <b>Новый комментарий</b> · {escape(company)} · {where}\n\n"
        f"<b>{escape(comment.author_name or 'Подписчик')}:</b> {escape(comment.text[:1500])}\n\n"
        f"🤖 <b>Предлагаю ответить:</b>\n{escape(comment.reply_text or '')}"
    )
    if comment.needs_human_reason:
        text += f"\n\n⚠️ Нужен ваш взгляд: {escape(comment.needs_human_reason)}"
    return text


def comment_auto_notice(comment: Comment) -> str:
    return (
        f"🤖 Ответил на комментарий <b>{escape(comment.author_name or 'подписчика')}</b>:\n"
        f"<i>{escape(comment.text[:300])}</i>\n→ {escape(comment.reply_text or '')}"
    )


def comments_overview(
    company: str, *, auto: bool, group_connected: bool, items: list[Comment]
) -> str:
    text = (
        f"💬 <b>Комментарии</b> · {escape(company)}\n"
        f"Группа обсуждения: {'подключена ✅' if group_connected else 'не найдена ❌'}\n"
        f"Автоответ: {'включён 🤖' if auto else 'выключен — отвечаю после согласования'}"
        " (/autoreply on|off — администратор)\n"
    )
    if items:
        lines = []
        for c in items:
            icon = COMMENT_STATUS_ICONS.get(CommentStatus(c.status), "")
            line = f"{icon} <b>{escape(c.author_name or 'подписчик')}:</b> {escape(c.text[:60])}"
            if c.status == CommentStatus.SENT and c.reply_text:
                line += f"\n    → {escape(c.reply_text[:80])}"
            lines.append(line)
        text += "\n" + "\n".join(lines)
    else:
        text += "\nКомментариев пока нет."
    if not group_connected:
        text += COMMENTS_SETUP
    return text


# --- поток B ---------------------------------------------------- отчёты

REPORT_BUILDING = "📈 Собираю отчёт за {days} дн.…"
REPORT_USAGE = "Отчёт за период: /report 7 (дней, от 1 до 90)."


# --- поток B ---------------------------------------------------- журнал и клиенты

LOG_HEADER = "📜 <b>Журнал действий</b> · {company}\n"
LOG_EMPTY = "Событий пока нет."
CLIENTS_HEADER = "👥 <b>Клиенты компании</b> {company}\n"
CLIENTS_EMPTY = "Клиентов пока нет. Ссылка-приглашение — /invite"
CLIENT_REVOKED = "🚫 Доступ {name} отключён. Старая ссылка-приглашение больше не работает."
CLIENT_RESTORED = "✅ Доступ {name} возвращён."

EVENT_ICONS = {
    "post_created": "📝",
    "post_revised": "✏️",
    "post_submitted": "📨",
    "post_approved": "👍",
    "post_rejected": "❌",
    "post_scheduled": "🕒",
    "post_published": "✅",
    "post_failed": "⚠️",
    "profile_updated": "🏢",
    "source_added": "📚",
    "agent_error": "🤖",
    "budget_exceeded": "💰",
    "comment_received": "💬",
    "comment_replied": "↩️",
    "company_created": "🏢",
    "client_joined": "🤝",
    "client_revoked": "🚫",
    "client_restored": "✅",
    "channel_changed": "📣",
}


def log_line(created_local: str, type_: str, message: str, post_id: int | None) -> str:
    icon = EVENT_ICONS.get(type_, "•")
    post = f" #{post_id}" if post_id else ""
    tail = f" — {escape(message[:80])}" if message else ""
    return f"{icon} <code>{created_local}</code> {escape(type_)}{post}{tail}"


def client_line(name: str, telegram_id: int, active: bool) -> str:
    state = "активен" if active else "отключён"
    return f"{'🟢' if active else '⚪️'} {escape(name or str(telegram_id))} — {state}"


# --- поток B ---------------------------------------------------- фотобанк

PHOTO_SAVED = (
    "🖼 Сохранил фото в фотобанк компании. Сделать с ним пост?\n"
    "<i>Совет: пришлите фото с подписью — сразу напишу пост по ней.</i>"
)
PHOTO_SAVED_MAKING_POST = "🖼 Сохранил фото и пишу пост по вашей подписи…"
PHOTO_KEPT = "Сохранено в фотобанк."
ASK_PHOTO_TOPIC = "📝 О чём пост с этим фото? Например: <i>«новинка недели, скидка 15%»</i>."
ASK_REPLACEMENT_PHOTO = (
    "🖼 Пришлите фото для поста #{id} — поставлю его вместо картинки и пришлю черновик "
    "на согласование ещё раз."
)
PHOTO_REPLACED = "🖼 Фото в посте #{id} заменено. Проверьте черновик ещё раз 👇"
PHOTO_WRONG_STATUS = "Пост #{id} уже опубликован или отклонён — фото не заменить."
PHOTOS_EMPTY = (
    "🖼 Фотобанк пуст. Пришлите боту фото зала, товаров, команды — я сохраню их и буду "
    "использовать в постах вместо сгенерированных картинок."
)
PHOTOS_HEADER = (
    "🖼 <b>Фотобанк</b> · {company}: последние {count} фото выше.\n"
    "Выберите фото для нового поста или пришлите новое."
)


MINIAPP_OPEN = "В Маркетологе можно выбрать компанию, подготовить пост и управлять расписанием."
MINIAPP_UNAVAILABLE = "Мини-приложение пока не подключено. Команды бота доступны в /help."
