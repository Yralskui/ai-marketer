"""Middleware: доступ по белому списку и сессия БД на каждое обновление."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import TelegramObject, Update
from aiogram.types import User as TgUser

from app.bot import texts
from app.services.accounts import ensure_user_and_business

if TYPE_CHECKING:
    from app.container import AppContainer

log = logging.getLogger(__name__)
Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class AccessMiddleware(BaseMiddleware):
    """Пропускает только пользователей из ALLOWED_USER_IDS. Остальным сообщает их ID."""

    def __init__(self, allowed_ids: set[int]) -> None:
        self.allowed_ids = allowed_ids

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: TgUser | None = data.get("event_from_user")
        if user is None:
            return None  # посты в канале, служебные обновления — игнорируем
        if user.id in self.allowed_ids:
            return await handler(event, data)

        log.warning("Доступ запрещён: %s (@%s)", user.id, user.username)
        if isinstance(event, Update):
            if event.message and event.message.chat.type == ChatType.PRIVATE:
                await event.message.answer(texts.ACCESS_DENIED.format(user_id=user.id))
            elif event.callback_query:
                await event.callback_query.answer("Нет доступа", show_alert=True)
        return None


class DbSessionMiddleware(BaseMiddleware):
    """Открывает сессию, находит/создаёт пользователя и его бизнес.

    В хендлерах доступны: session, db_user, business, container.
    Коммит — после успешного хендлера, при исключении — откат.
    """

    def __init__(self, container: AppContainer) -> None:
        self.container = container

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        async with self.container.db.sessionmaker() as session:
            data["session"] = session
            if tg_user is not None:
                user, business = await ensure_user_and_business(
                    session, self.container.settings, tg_user.id, tg_user.full_name
                )
                data["db_user"] = user
                data["business"] = business
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
