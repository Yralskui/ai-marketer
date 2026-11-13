"""Журнал действий (/log) и управление доступом клиентов (/clients)."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import ClientCb, clients_keyboard
from app.container import AppContainer
from app.db.models import Business, User
from app.db.repositories import EventLogRepo
from app.services import accounts
from app.services.accounts import CompanyError
from app.utils.time import format_local

router = Router(name="audit")


@router.message(Command("log"))
async def cmd_log(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
) -> None:
    """Последние события компании: создание, согласование, публикации, доступы, ошибки."""
    arg = (command.args or "").strip()
    limit = min(int(arg), 50) if arg.isdigit() and int(arg) > 0 else 15
    events = await EventLogRepo(session).list_recent(business.id, limit)
    tz = container.settings.tz
    lines = [
        texts.log_line(format_local(e.created_at, tz), e.type, e.message or "", e.post_id)
        for e in events
    ]
    await message.answer(
        texts.LOG_HEADER.format(company=escape(business.name))
        + ("\n".join(lines) if lines else texts.LOG_EMPTY)
    )


async def _clients_view(
    session: AsyncSession, db_user: User, business: Business
) -> tuple[str, object]:
    clients = await accounts.list_clients(session, db_user, business)
    header = texts.CLIENTS_HEADER.format(company=escape(business.name))
    if not clients:
        return header + texts.CLIENTS_EMPTY, None
    lines = [texts.client_line(c.name, c.telegram_id, c.is_active) for c in clients]
    keyboard = clients_keyboard(
        [(c.id, c.name or str(c.telegram_id), c.is_active) for c in clients]
    )
    return header + "\n".join(lines), keyboard


@router.message(Command("clients"))
async def cmd_clients(
    message: Message, session: AsyncSession, db_user: User, business: Business
) -> None:
    try:
        text, keyboard = await _clients_view(session, db_user, business)
    except CompanyError as exc:
        await message.answer(escape(str(exc)))
        return
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(ClientCb.filter(F.action.in_({"revoke", "restore"})))
async def on_client_access(
    callback: CallbackQuery,
    callback_data: ClientCb,
    session: AsyncSession,
    db_user: User,
    business: Business,
) -> None:
    active = callback_data.action == "restore"
    try:
        client = await accounts.set_client_access(
            session, db_user, business, callback_data.user_id, active
        )
    except CompanyError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    name = escape(client.name or str(client.telegram_id))
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            (texts.CLIENT_RESTORED if active else texts.CLIENT_REVOKED).format(name=name)
        )
