"""Все клавиатуры и CallbackData-фабрики."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


class PostCb(CallbackData, prefix="post"):
    """Кнопки под черновиком. action: publish | schedule | edit | reject."""

    action: str
    post_id: int


class OnboardingCb(CallbackData, prefix="onb"):
    action: str  # done


class CancelCb(CallbackData, prefix="cancel"):
    pass


def draft_keyboard(post_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Опубликовать сейчас", callback_data=PostCb(action="publish", post_id=post_id)
    )
    kb.button(text="🕒 Запланировать", callback_data=PostCb(action="schedule", post_id=post_id))
    kb.button(text="✏️ Изменить", callback_data=PostCb(action="edit", post_id=post_id))
    kb.button(text="❌ Отклонить", callback_data=PostCb(action="reject", post_id=post_id))
    kb.adjust(1, 2, 1)
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
MENU_STATUS = "📊 Статус"
MENU_HELP = "❓ Помощь"
MENU_COMMENTS = "💬 Комментарии"
MENU_REPORT = "📈 Отчёт"
MENU_COMPANIES = "🗂 Компании"
MENU_NEW_COMPANY = "➕ Новая компания"
MENU_INVITE = "🔗 Приглашение"
MENU_COSTS = "💰 Расходы"

ADMIN_MENU_BUTTONS = (MENU_COMPANIES, MENU_NEW_COMPANY, MENU_INVITE, MENU_COSTS)


def main_menu(*, admin: bool) -> ReplyKeyboardMarkup:
    """Постоянное меню под полем ввода. У администратора — ещё ряд управления компаниями."""
    kb = ReplyKeyboardBuilder()
    for text in (MENU_NEW_POST, MENU_POSTS, MENU_COMMENTS, MENU_REPORT):
        kb.button(text=text)
    for text in (MENU_PROFILE, MENU_MATERIALS, MENU_CHANNEL, MENU_STATUS, MENU_HELP):
        kb.button(text=text)
    sizes = [2, 2, 2, 3]
    if admin:
        for text in ADMIN_MENU_BUTTONS:
            kb.button(text=text)
        sizes += [2, 2]
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
