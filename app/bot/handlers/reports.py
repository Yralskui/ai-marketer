"""Отчёты: /report и приём счётчиков реакций на посты канала."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, MessageReactionCountUpdated, ReactionTypeEmoji
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.notifier import send_report
from app.container import AppContainer
from app.db.models import Business
from app.services import analytics, reports

log = logging.getLogger(__name__)
router = Router(name="reports")


@router.message(Command("report"))
async def cmd_report(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    arg = (command.args or "7").strip()
    if not arg.isdigit() or not 1 <= int(arg) <= 90:
        await message.answer(texts.REPORT_USAGE)
        return
    days = int(arg)
    await message.answer(texts.REPORT_BUILDING.format(days=days))
    async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
        report = await reports.build_report(
            session, container.llm, business, days=days, tz=container.settings.tz
        )
    await send_report(bot, message.chat.id, report)


@router.message_reaction_count()
async def on_reaction_count(event: MessageReactionCountUpdated, session: AsyncSession) -> None:
    """Telegram присылает новые итоги реакций на пост канала (бот — админ канала)."""
    counts: dict[str, int] = {}
    for item in event.reactions:
        reaction = item.type
        key = reaction.emoji if isinstance(reaction, ReactionTypeEmoji) else reaction.type
        counts[key] = counts.get(key, 0) + item.total_count
    saved = await analytics.record_reactions(
        session, event.chat.id, event.chat.username, event.message_id, counts
    )
    log.info("Реакции на %s/%s: %s (наш пост: %s)", event.chat.id, event.message_id, counts, saved)
