"""Все клавиатуры и CallbackData-фабрики."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


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
