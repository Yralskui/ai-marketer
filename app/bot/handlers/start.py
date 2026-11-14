"""Вход в Mini App и сброс незавершённого диалога."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import miniapp_keyboard
from app.container import AppContainer
from app.db.knowledge import PendingDraftOptions
from app.db.models import User

router = Router(name="start")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    container: AppContainer,
    session: AsyncSession,
    db_user: User,
) -> None:
    await state.clear()
    await session.execute(
        delete(PendingDraftOptions).where(PendingDraftOptions.user_id == db_user.id)
    )
    await state.update_data(turns_since_reset=0)
    await message.answer(
        "Напишите задачу обычным текстом. Профили, черновики и публикации доступны в приложении.",
        reply_markup=ReplyKeyboardRemove(),
    )
    if url := container.settings.telegram_miniapp_url:
        await message.answer("Откройте рабочее пространство.", reply_markup=miniapp_keyboard(url))


@router.callback_query(~F.data.startswith("draftopt:"))
async def old_button(callback: CallbackQuery) -> None:
    await callback.answer("Откройте Mini App через /start.", show_alert=True)
