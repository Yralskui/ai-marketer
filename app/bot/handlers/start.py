"""/start, /help, /cancel."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.keyboards import CancelCb
from app.db.models import Business, User
from app.services.accounts import Account
from app.services.business_profile import get_profile

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(
    message: Message, state: FSMContext, db_user: User, business: Business, account: Account
) -> None:
    await state.clear()
    await message.answer(
        texts.start_for(
            db_user.name,
            get_profile(business).is_filled(),
            business.name,
            business.id,
            joined=account.joined,
            admin=account.is_admin,
        )
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer(texts.NOTHING_TO_CANCEL)
        return
    await state.clear()
    await message.answer(texts.CANCELLED)


@router.callback_query(CancelCb.filter())
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(texts.CANCELLED)
    await callback.answer()
