"""Вход в Mini App и сброс незавершённого диалога."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from app.bot.keyboards import miniapp_keyboard
from app.container import AppContainer

router = Router(name="start")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, container: AppContainer) -> None:
    await state.clear()
    await state.update_data(turns_since_reset=0)
    await message.answer(
        "Напишите задачу обычным текстом. Профили, черновики и публикации доступны в приложении.",
        reply_markup=ReplyKeyboardRemove(),
    )
    if url := container.settings.telegram_miniapp_url:
        await message.answer("Откройте рабочее пространство.", reply_markup=miniapp_keyboard(url))


@router.callback_query()
async def old_button(callback: CallbackQuery) -> None:
    await callback.answer("Откройте Mini App через /start.", show_alert=True)
