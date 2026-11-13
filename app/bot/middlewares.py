"""Middleware: сессия БД, доступ и текущая компания на каждое обновление."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import TelegramObject, Update
from aiogram.types import User as TgUser

from app.bot import texts
from app.services.accounts import resolve_account
from app.services.usage import usage_scope

if TYPE_CHECKING:
    from app.container import AppContainer

log = logging.getLogger(__name__)
Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def start_payload(event: TelegramObject) -> str | None:
    """Параметр из «/start <код>» (deep link с приглашением)."""
    if isinstance(event, Update) and event.message and event.message.text:
        parts = event.message.text.split(maxsplit=1)
        if len(parts) == 2 and parts[0].split("@")[0] == "/start":
            return parts[1].strip()
    return None


class DbSessionMiddleware(BaseMiddleware):
    """Открывает сессию, проверяет доступ и находит пользователя и его компанию.

    Доступ есть у администраторов (ALLOWED_USER_IDS) и у клиентов, подключённых по
    приглашению. Остальным бот сообщает их ID и дальше обновление не пускает.

    В хендлерах доступны: session, db_user, business, account, container.
    Коммит — после успешного хендлера, при исключении — откат.
    """

    def __init__(self, container: AppContainer) -> None:
        self.container = container

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return None  # посты в канале, служебные обновления — игнорируем

        if _is_group_update(event):
            # Группа обсуждения канала: пишут подписчики, доступа к боту у них нет.
            # Обрабатывает только роутер comments, ему нужна лишь сессия.
            async with self.container.db.sessionmaker() as session:
                data["session"] = session
                try:
                    result = await handler(event, data)
                except Exception:
                    await session.rollback()
                    raise
                await session.commit()
                return result

        async with self.container.db.sessionmaker() as session:
            account = await resolve_account(
                session,
                self.container.settings,
                tg_user.id,
                tg_user.full_name,
                start_payload=start_payload(event),
            )
            if account is None:
                await session.rollback()
                await _deny(event, tg_user)
                return None

            data["session"] = session
            data["account"] = account
            data["db_user"] = account.user
            data["business"] = account.business
            try:
                # Все AI-расходы внутри обновления — на текущую компанию
                async with usage_scope(session, account.business.id):
                    result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


def _is_group_update(event: TelegramObject) -> bool:
    return (
        isinstance(event, Update)
        and event.message is not None
        and event.message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    )


async def _deny(event: TelegramObject, user: TgUser) -> None:
    log.warning("Доступ запрещён: %s (@%s)", user.id, user.username)
    if not isinstance(event, Update):
        return
    if event.message and event.message.chat.type == ChatType.PRIVATE:
        await event.message.answer(texts.ACCESS_DENIED.format(user_id=user.id))
    elif event.callback_query:
        await event.callback_query.answer("Нет доступа", show_alert=True)
