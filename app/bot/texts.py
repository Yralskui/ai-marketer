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
    "• Пишите задачи обычным языком: <i>«напиши пост про скидки на выходных с картинкой»</i>, "
    "<i>«сделай пост #3 короче»</i>, <i>«покажи мои посты»</i>.\n"
    "• Каждый пост сначала приходит черновиком с кнопками — без вашего «Опубликовать» "
    "ничего не выйдет.\n\n"
    "<b>Команды</b>\n"
    "/onboarding — рассказать о бизнесе (текст, файлы PDF/DOCX/TXT, ссылка на сайт)\n"
    "/profile — профиль бизнеса\n"
    "/posts — последние посты и статусы\n"
    "/post N — открыть пост №N\n"
    "/status — диагностика\n"
    "/cancel — отменить текущее действие"
)
CANCELLED = "Отменено. Чем ещё помочь?"
NOTHING_TO_CANCEL = "Нечего отменять 🙂"
ACCESS_DENIED = (
    "⛔ У вас нет доступа к этому боту.\n"
    "Ваш Telegram ID: <code>{user_id}</code> — передайте его администратору "
    "(переменная ALLOWED_USER_IDS)."
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
        f"Telegram-канал: {channel}\n\n"
        f"<b>Посты</b>\n{counts_text}\n\n"
        f"<b>Планировщик</b>\n{jobs_text}"
    )
