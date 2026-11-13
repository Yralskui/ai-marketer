"""Компании и каналы: /new_company, /companies, /use, /invite, /channel."""

from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import (
    CHANNEL_PICK_REQUEST_ID,
    CompanyCb,
    channel_picker_keyboard,
    companies_keyboard,
    main_menu,
)
from app.container import AppContainer
from app.db.models import Business, Channel, Platform, User
from app.db.repositories import ChannelRepo
from app.services import accounts
from app.services.accounts import Account, CompanyError

router = Router(name="companies")


async def _invite_text(bot: Bot, business: Business) -> str:
    me = await bot.me()
    link = accounts.invite_link(me.username or "bot", business)
    return texts.INVITE.format(company=escape(business.name), link=escape(link))


# ---------------------------------------------------------------- администратор


@router.message(Command("new_company"))
async def cmd_new_company(
    message: Message, command: CommandObject, session: AsyncSession, db_user: User, bot: Bot
) -> None:
    try:
        business = await accounts.create_company(session, db_user, command.args or "")
    except CompanyError as exc:
        await message.answer(escape(str(exc)))
        return
    me = await bot.me()
    link = accounts.invite_link(me.username or "bot", business)
    await message.answer(
        texts.COMPANY_CREATED.format(
            company=escape(business.name), id=business.id, link=escape(link)
        )
    )


@router.message(Command("companies"))
async def cmd_companies(message: Message, session: AsyncSession, db_user: User) -> None:
    try:
        items = await accounts.list_companies(session, db_user)
    except CompanyError as exc:
        await message.answer(escape(str(exc)))
        return
    lines = [
        texts.company_line(
            i.business.id,
            i.business.name,
            [c.name or str(c.telegram_id) for c in i.clients],
            i.channel.external_id if i.channel else None,
            i.is_current,
        )
        for i in items
    ]
    keyboard = companies_keyboard(
        [(i.business.id, i.business.name) for i in items], db_user.business_id
    )
    await message.answer(
        texts.COMPANIES_HEADER + "\n".join(lines) + texts.COMPANIES_FOOTER,
        reply_markup=keyboard if keyboard.inline_keyboard else None,
    )


async def _switch(session: AsyncSession, db_user: User, business_id: int) -> str:
    try:
        business = await accounts.switch_company(session, db_user, business_id)
    except CompanyError as exc:
        return escape(str(exc))
    return texts.COMPANY_SWITCHED.format(company=escape(business.name), id=business.id)


@router.message(Command("use"))
async def cmd_use(
    message: Message, command: CommandObject, session: AsyncSession, db_user: User
) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer(texts.USE_USAGE)
        return
    await message.answer(await _switch(session, db_user, int(arg)))


@router.callback_query(CompanyCb.filter())
async def on_company(
    callback: CallbackQuery, callback_data: CompanyCb, session: AsyncSession, db_user: User
) -> None:
    text = await _switch(session, db_user, callback_data.business_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(text)


@router.message(Command("invite"))
async def cmd_invite(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    db_user: User,
    business: Business,
    bot: Bot,
) -> None:
    if not accounts.is_admin(db_user):
        await message.answer(texts.ADMIN_ONLY)
        return
    if (command.args or "").strip().lower() == "new" or not business.invite_code:
        await accounts.regenerate_invite(session, db_user, business)
    await message.answer(await _invite_text(bot, business))


# ---------------------------------------------------------------- канал компании


@router.message(Command("channel"))
async def cmd_channel(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
) -> None:
    company = escape(business.name)
    raw = (command.args or "").strip()
    if not raw:
        # Без аргумента — кнопка нативного выбора канала: ID искать не нужно
        channel = await ChannelRepo(session).get_active(business.id, Platform.TELEGRAM)
        shown = (
            f"<code>{escape(channel.title or channel.external_id)}</code>"
            if channel
            else texts.CHANNEL_NOT_SET
        )
        await message.answer(
            texts.CHANNEL_CURRENT.format(company=company, channel=shown)
            + "\n\n"
            + texts.CHANNEL_PICK_HINT,
            reply_markup=channel_picker_keyboard(),
        )
        return
    try:
        channel = await accounts.set_channel(session, business, raw, db_user)
    except CompanyError as exc:
        await message.answer(escape(str(exc)))
        return
    await message.answer(await _channel_saved_text(channel, company, container))


async def _channel_saved_text(channel: Channel, company: str, container: AppContainer) -> str:
    text = texts.CHANNEL_SAVED.format(
        channel=f"<code>{escape(channel.title or channel.external_id)}</code>", company=company
    )
    publisher = container.publishing.get_publisher(Platform.TELEGRAM)
    if publisher is not None:
        check = await publisher.check_channel(channel)
        text += texts.CHANNEL_CHECK.format(
            mark="✅" if check.ok else "❌", message=escape(check.message)
        )
    return text


@router.message(F.chat_shared)
async def on_channel_shared(
    message: Message,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    account: Account,
) -> None:
    """Клиент выбрал канал нативной кнопкой Telegram — бот уже добавлен админом."""
    shared = message.chat_shared
    if shared is None or shared.request_id != CHANNEL_PICK_REQUEST_ID:
        return
    menu = main_menu(admin=account.is_admin)
    try:
        channel = await accounts.set_channel(session, business, str(shared.chat_id), db_user)
    except CompanyError as exc:
        await message.answer(
            texts.CHANNEL_PICKED_FAILED.format(error=escape(str(exc))), reply_markup=menu
        )
        return
    if shared.title:
        channel.title = shared.title[:255]
    await message.answer(
        await _channel_saved_text(channel, escape(business.name), container), reply_markup=menu
    )
