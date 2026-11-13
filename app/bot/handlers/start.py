"""/start, /help, /cancel."""

from __future__ import annotations

from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from app.bot import texts
from app.bot.keyboards import CancelCb, main_menu
from app.db.models import Business, User
from app.services.accounts import Account
from app.services.business_profile import get_profile

router = Router(name="start")

# Приветственная картинка (рисует scripts/make_brand_assets.py)
WELCOME_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "welcome.png"


@router.message(CommandStart())
async def cmd_start(
    message: Message, state: FSMContext, db_user: User, business: Business, account: Account
) -> None:
    await state.clear()
    text = texts.start_for(
        db_user.name,
        get_profile(business).is_filled(),
        business.name,
        business.id,
        joined=account.joined,
        admin=account.is_admin,
    )
    menu = main_menu(admin=account.is_admin)
    if WELCOME_IMAGE.exists():
        await message.answer_photo(FSInputFile(WELCOME_IMAGE), caption=text, reply_markup=menu)
    else:
        await message.answer(text, reply_markup=menu)


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
