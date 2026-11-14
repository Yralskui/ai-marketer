"""Все клавиатуры и CallbackData-фабрики."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    ChatAdministratorRights,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


class PostCb(CallbackData, prefix="post"):
    """Кнопки под черновиком. action: publish | schedule | edit | photo | reject."""

    action: str
    post_id: int


class OnboardingCb(CallbackData, prefix="onb"):
    action: str  # done


class CancelCb(CallbackData, prefix="cancel"):
    pass


def draft_keyboard(post_id: int, *, brand_fix: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if brand_fix:
        kb.button(
            text="🛠 Исправить по бренд-гиду",
            callback_data=PostCb(action="brandfix", post_id=post_id),
        )
    kb.button(
        text="✅ Опубликовать сейчас", callback_data=PostCb(action="publish", post_id=post_id)
    )
    kb.button(text="🕒 Запланировать", callback_data=PostCb(action="schedule", post_id=post_id))
    kb.button(text="✏️ Изменить", callback_data=PostCb(action="edit", post_id=post_id))
    kb.button(text="🖼 Своё фото", callback_data=PostCb(action="photo", post_id=post_id))
    kb.button(text="❌ Отклонить", callback_data=PostCb(action="reject", post_id=post_id))
    kb.adjust(*([1] if brand_fix else []), 1, 2, 2)
    return kb.as_markup()


def onboarding_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=OnboardingCb(action="done"))
    kb.button(text="Отмена", callback_data=CancelCb())
    kb.adjust(2)
    return kb.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data=CancelCb())
    return kb.as_markup()


# --- поток B ---------------------------------------------------- компании


class CompanyCb(CallbackData, prefix="company"):
    """Кнопки списка компаний. action: use."""

    action: str
    business_id: int


def companies_keyboard(
    items: list[tuple[int, str]], current_id: int | None
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for business_id, name in items:
        if business_id == current_id:
            continue
        kb.button(
            text=f"➡️ #{business_id} {name[:40]}",
            callback_data=CompanyCb(action="use", business_id=business_id),
        )
    kb.adjust(1)
    return kb.as_markup()


# --- поток B ---------------------------------------------------- главное меню (кнопки снизу)

MENU_NEW_POST = "✍️ Новый пост"
MENU_POSTS = "📋 Мои посты"
MENU_PROFILE = "🏢 О компании"
MENU_MATERIALS = "📚 Материалы"
MENU_CHANNEL = "📣 Канал"
MENU_STATUS = "📊 Состояние"
MENU_HELP = "❓ Помощь"
MENU_COMMENTS = "💬 Комментарии"
MENU_REPORT = "📈 Отчёт"
MENU_PHOTOS = "🖼 Фотобанк"
MENU_COMPANIES = "🗂 Компании"
MENU_NEW_COMPANY = "➕ Новая компания"
MENU_INVITE = "🔗 Приглашение"
MENU_COSTS = "💰 Расходы"
MENU_CLIENTS = "👥 Клиенты"
MENU_LOG = "📜 Журнал"
MENU_BRAND = "🎨 Бренд-гид"

ADMIN_MENU_BUTTONS = (
    MENU_COMPANIES,
    MENU_NEW_COMPANY,
    MENU_INVITE,
    MENU_CLIENTS,
    MENU_COSTS,
    MENU_LOG,
    MENU_BRAND,
)


def main_menu(*, admin: bool) -> ReplyKeyboardMarkup:
    """Постоянное меню под полем ввода. У администратора — ещё ряд управления компаниями."""
    kb = ReplyKeyboardBuilder()
    for text in (MENU_NEW_POST, MENU_POSTS, MENU_COMMENTS, MENU_REPORT):
        kb.button(text=text)
    for text in (MENU_PROFILE, MENU_MATERIALS, MENU_PHOTOS, MENU_CHANNEL, MENU_STATUS, MENU_HELP):
        kb.button(text=text)
    sizes = [2, 2, 3, 3]
    if admin:
        for text in ADMIN_MENU_BUTTONS:
            kb.button(text=text)
        sizes += [3, 3, 1]
    kb.adjust(*sizes)
    return kb.as_markup(
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напишите задачу или выберите кнопку",
    )


# --- поток B ---------------------------------------------------- комментарии


class CommentCb(CallbackData, prefix="cmt"):
    """Кнопки под карточкой комментария. action: send | edit | skip."""

    action: str
    comment_id: int


def comment_keyboard(comment_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Отправить ответ", callback_data=CommentCb(action="send", comment_id=comment_id)
    )
    kb.button(text="✏️ Свой ответ", callback_data=CommentCb(action="edit", comment_id=comment_id))
    kb.button(text="🙈 Пропустить", callback_data=CommentCb(action="skip", comment_id=comment_id))
    kb.adjust(1, 2)
    return kb.as_markup()


# --- поток B ---------------------------------------------------- клиенты компании


class ClientCb(CallbackData, prefix="client"):
    """action: revoke | restore."""

    action: str
    user_id: int


def clients_keyboard(clients: list[tuple[int, str, bool]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for user_id, name, active in clients:
        if active:
            kb.button(
                text=f"🚫 Отключить {name[:30]}",
                callback_data=ClientCb(action="revoke", user_id=user_id),
            )
        else:
            kb.button(
                text=f"✅ Вернуть {name[:30]}",
                callback_data=ClientCb(action="restore", user_id=user_id),
            )
    kb.adjust(1)
    return kb.as_markup()


# --- поток B ---------------------------------------------------- фотобанк


class MediaCb(CallbackData, prefix="media"):
    """action: post (сделать пост с этим фото) | keep (только сохранить)."""

    action: str
    media_id: int


def photo_keyboard(media_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Пост с этим фото", callback_data=MediaCb(action="post", media_id=media_id))
    kb.button(text="🗂 Только сохранить", callback_data=MediaCb(action="keep", media_id=media_id))
    kb.adjust(1, 1)
    return kb.as_markup()


def photos_keyboard(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for media_id, caption in items:
        label = f"📝 Пост с фото #{media_id}" + (f" · {caption[:20]}" if caption else "")
        kb.button(text=label, callback_data=MediaCb(action="post", media_id=media_id))
    kb.adjust(1)
    return kb.as_markup()


def miniapp_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть Маркетолог", web_app=WebAppInfo(url=url))
    return builder.as_markup()


# --- поток B ---------------------------------------------------- быстрые действия с постом

# Быстрые правки: код -> (кнопка, инструкция для LLM)
QUICK_EDITS: dict[str, tuple[str, str]] = {
    "short": ("✂️ Короче", "Сделай текст заметно короче, оставь главное."),
    "long": ("➕ Подробнее", "Сделай текст подробнее: добавь деталей и пользы для читателя."),
    "emoji": ("😊 Эмодзи", "Добавь уместные эмодзи, не перебарщивай."),
    "formal": ("👔 Официальнее", "Сделай тон более деловым и официальным."),
    "warm": ("🤗 Теплее", "Сделай тон проще, теплее и дружелюбнее."),
    "cta": ("📣 Призыв", "Добавь в конце понятный призыв к действию."),
    "new": ("🔄 Другой вариант", "Напиши другой вариант на ту же тему, с другим заходом."),
}


class QuickEditCb(CallbackData, prefix="qe"):
    post_id: int
    code: str


def quick_edit_keyboard(post_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, (label, _) in QUICK_EDITS.items():
        kb.button(text=label, callback_data=QuickEditCb(post_id=post_id, code=code))
    kb.button(text="Отмена", callback_data=CancelCb())
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


class TimeCb(CallbackData, prefix="tm"):
    """Запланировать на время ts (unix, секунды)."""

    post_id: int
    ts: int


def schedule_keyboard(post_id: int, options: list[tuple[str, int]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, ts in options:
        kb.button(text=f"🕒 {label}", callback_data=TimeCb(post_id=post_id, ts=ts))
    kb.button(text="Отмена", callback_data=CancelCb())
    kb.adjust(2)
    return kb.as_markup()


class PublishedCb(CallbackData, prefix="pub"):
    """action: delete (спросить) | delete_yes | delete_no."""

    action: str
    post_id: int


def published_keyboard(post_id: int, url: str | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if url:
        kb.button(text="🔗 Открыть в канале", url=url)
    kb.button(
        text="🗑 Удалить из канала", callback_data=PublishedCb(action="delete", post_id=post_id)
    )
    kb.adjust(1)
    return kb.as_markup()


def delete_confirm_keyboard(post_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=PublishedCb(action="delete_yes", post_id=post_id))
    kb.button(text="Нет", callback_data=PublishedCb(action="delete_no", post_id=post_id))
    kb.adjust(2)
    return kb.as_markup()


def retry_keyboard(post_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Повторить", callback_data=PostCb(action="publish", post_id=post_id))
    kb.button(text="❌ Отклонить", callback_data=PostCb(action="reject", post_id=post_id))
    kb.adjust(2)
    return kb.as_markup()


# --- поток B ---------------------------------------------------- список постов


class PostViewCb(CallbackData, prefix="pv"):
    post_id: int


class PostsFilterCb(CallbackData, prefix="pf"):
    """status: all | pending | scheduled | published."""

    status: str


POST_FILTERS: dict[str, str] = {
    "all": "Все",
    "pending": "⏳ Ждут",
    "scheduled": "🕒 В плане",
    "published": "✅ Вышли",
}


class MenuCb(CallbackData, prefix="menu"):
    """Кнопки-переходы из пустых состояний. action: new_post."""

    action: str


def posts_keyboard(items: list[tuple[int, str]], current: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, label in POST_FILTERS.items():
        mark = "• " if code == current else ""
        kb.button(text=f"{mark}{label}", callback_data=PostsFilterCb(status=code))
    for post_id, label in items:
        kb.button(text=label[:60], callback_data=PostViewCb(post_id=post_id))
    kb.button(text="✍️ Новый пост", callback_data=MenuCb(action="new_post"))
    kb.adjust(4, *([1] * len(items)), 1)
    return kb.as_markup()


def new_post_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Создать пост", callback_data=MenuCb(action="new_post"))
    return kb.as_markup()


# --- поток B ---------------------------------------------------- выбор канала

CHANNEL_PICK_REQUEST_ID = 1
MENU_BACK = "↩️ В меню"
CHANNEL_PICK_BUTTON = "📣 Выбрать канал"


def channel_picker_keyboard() -> ReplyKeyboardMarkup:
    """Нативный выбор канала Telegram: бот сразу получает права админа на публикацию."""
    rights = ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_send_welcome_messages=False,
        can_post_messages=True,
        can_edit_messages=True,
    )
    pick = KeyboardButton(
        text=CHANNEL_PICK_BUTTON,
        request_chat=KeyboardButtonRequestChat(
            request_id=CHANNEL_PICK_REQUEST_ID,
            chat_is_channel=True,
            bot_administrator_rights=rights,
            request_title=True,
            request_username=True,
        ),
    )
    return ReplyKeyboardMarkup(
        keyboard=[[pick], [KeyboardButton(text=MENU_BACK)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# --- поток B ---------------------------------------------------- бренд-гид


class BrandCb(CallbackData, prefix="brand"):
    """action: upload | preset (value — код шаблона) | clear."""

    action: str
    value: str = ""


def brand_keyboard(
    presets: list[tuple[str, str]], *, admin: bool, filled: bool
) -> InlineKeyboardMarkup | None:
    if not admin:
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Загрузить брендбук или правила", callback_data=BrandCb(action="upload"))
    for code, title in presets:
        kb.button(text=f"✨ Шаблон: {title}", callback_data=BrandCb(action="preset", value=code))
    if filled:
        kb.button(text="🧹 Очистить", callback_data=BrandCb(action="clear"))
    kb.adjust(1)
    return kb.as_markup()
