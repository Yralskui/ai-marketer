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
from app.services.usage import BudgetExceededError

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
    # Пока модель думает (с настоящей LLM — 10–30 с), видно, что бот работает
    progress = await message.answer(texts.WORKING)
    answer = texts.AGENT_ERROR
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            answer = escape((await container.agent.run(ctx, text)).text)
        except AgentError as exc:
            if isinstance(exc.__cause__, BudgetExceededError):
                answer = escape(str(exc.__cause__))
            else:
                await EventLogRepo(session).log(
                    EventType.AGENT_ERROR, str(exc)[:500], business_id=business.id
                )
    try:
        await progress.delete()
    except Exception:
        log.debug("Не удалось удалить сообщение «работаю»", exc_info=True)
    await message.answer(answer)


@router.message(StateFilter(None), F.document)
async def on_document_outside_onboarding(message: Message) -> None:
    await message.answer(texts.DOCUMENT_OUTSIDE_ONBOARDING)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(texts.UNSUPPORTED_MESSAGE)
