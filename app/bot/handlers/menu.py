"""Главное меню кнопками под полем ввода.

Кнопки — обычные текстовые сообщения, поэтому роутер стоит раньше онбординга и
согласования: нажатие кнопки прерывает любой незавершённый ввод. Сами действия не
дублируются — вызываются те же хендлеры, что и у команд.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.handlers import (
    admin,
    audit,
    chat,
    comments,
    companies,
    costs,
    onboarding,
    reports,
    start,
)
from app.bot.states import MenuStates
from app.container import AppContainer
from app.db.models import Business, User
from app.services import accounts

router = Router(name="menu")


def _command(name: str, args: str | None = None) -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args)


@router.message(F.text == kb.MENU_NEW_POST)
async def menu_new_post(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuStates.waiting_post_topic)
    await message.answer(texts.ASK_POST_TOPIC, reply_markup=kb.cancel_keyboard())


@router.message(F.text == kb.MENU_POSTS)
async def menu_posts(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
) -> None:
    await state.clear()
    await admin.cmd_posts(message, session, business, container)


@router.message(F.text == kb.MENU_PROFILE)
async def menu_profile(message: Message, state: FSMContext, business: Business) -> None:
    await state.clear()
    await onboarding.cmd_profile(message, business)


@router.message(F.text == kb.MENU_MATERIALS)
async def menu_materials(message: Message, state: FSMContext) -> None:
    await onboarding.cmd_onboarding(message, state)


@router.message(F.text == kb.MENU_CHANNEL)
async def menu_channel(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
) -> None:
    await state.clear()
    await companies.cmd_channel(message, _command("channel"), session, db_user, business, container)


@router.message(F.text == kb.MENU_STATUS)
async def menu_status(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
) -> None:
    await state.clear()
    await admin.cmd_status(message, session, business, container)


@router.message(F.text == kb.MENU_HELP)
async def menu_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await start.cmd_help(message)


# ---------------------------------------------------------------- администратор


@router.message(F.text == kb.MENU_COMPANIES)
async def menu_companies(
    message: Message, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    await state.clear()
    await companies.cmd_companies(message, session, db_user)


@router.message(F.text == kb.MENU_NEW_COMPANY)
async def menu_new_company(message: Message, state: FSMContext, db_user: User) -> None:
    if not accounts.is_admin(db_user):
        await message.answer(texts.ADMIN_ONLY)
        return
    await state.set_state(MenuStates.waiting_company_name)
    await message.answer(texts.ASK_COMPANY_NAME, reply_markup=kb.cancel_keyboard())


@router.message(F.text == kb.MENU_INVITE)
async def menu_invite(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    bot: Bot,
) -> None:
    await state.clear()
    await companies.cmd_invite(message, _command("invite"), session, db_user, business, bot)


# ---------------------------------------------------------------- ввод после кнопки


@router.message(MenuStates.waiting_post_topic, F.text, ~F.text.startswith("/"))
async def on_post_topic(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    await state.clear()
    task = texts.POST_TOPIC_PREFIX + (message.text or "")
    await chat.ask_agent(message, task, session, db_user, business, container, bot)


@router.message(MenuStates.waiting_company_name, F.text, ~F.text.startswith("/"))
async def on_company_name(
    message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot
) -> None:
    await state.clear()
    await companies.cmd_new_company(
        message, _command("new_company", message.text), session, db_user, bot
    )


@router.message(F.text == kb.MENU_COSTS)
async def menu_costs(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
) -> None:
    await state.clear()
    await costs.cmd_costs(message, session, db_user, business, container)


@router.message(F.text == kb.MENU_COMMENTS)
async def menu_comments(
    message: Message, state: FSMContext, session: AsyncSession, business: Business, bot: Bot
) -> None:
    await state.clear()
    await comments.cmd_comments(message, session, business, bot)


@router.message(F.text == kb.MENU_REPORT)
async def menu_report(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    await state.clear()
    await reports.cmd_report(
        message, CommandObject(prefix="/", command="report"), session, business, container, bot
    )


@router.message(F.text == kb.MENU_CLIENTS)
async def menu_clients(
    message: Message, state: FSMContext, session: AsyncSession, db_user: User, business: Business
) -> None:
    await state.clear()
    await audit.cmd_clients(message, session, db_user, business)


@router.message(F.text == kb.MENU_LOG)
async def menu_log(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
) -> None:
    await state.clear()
    await audit.cmd_log(message, _command("log"), session, business, container)
