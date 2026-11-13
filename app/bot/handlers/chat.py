"""Свободный текст -> AI-агент. Роутер подключается ПОСЛЕДНИМ."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import AgentContext
from app.agent.core import AgentError
from app.bot import texts
from app.container import AppContainer
from app.db.models import Business, EventType, User
from app.db.repositories import EventLogRepo

log = logging.getLogger(__name__)
router = Router(name="chat")


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer(texts.UNKNOWN_COMMAND)


@router.message(StateFilter(None), F.text)
async def on_text(
    message: Message,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    await ask_agent(message, message.text or "", session, db_user, business, container, bot)


async def ask_agent(
    message: Message,
    text: str,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    """Передаёт задачу агенту и отвечает его итоговым текстом."""
    ctx = AgentContext(
        session=session,
        user=db_user,
        business=business,
        settings=container.settings,
        llm=container.llm,
        images=container.images,
        notifier=container.notifier,
        chat_id=message.chat.id,
    )
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            result = await container.agent.run(ctx, text)
        except AgentError as exc:
            await EventLogRepo(session).log(
                EventType.AGENT_ERROR, str(exc)[:500], business_id=business.id
            )
            await message.answer(texts.AGENT_ERROR)
            return
    await message.answer(escape(result.text))


@router.message(StateFilter(None), F.document)
async def on_document_outside_onboarding(message: Message) -> None:
    await message.answer(texts.DOCUMENT_OUTSIDE_ONBOARDING)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(texts.UNSUPPORTED_MESSAGE)
