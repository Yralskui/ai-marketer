"""Создание Bot и Dispatcher, подключение роутеров и middleware."""

from __future__ import annotations

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot.middlewares import DbSessionMiddleware
from app.config import Settings
from app.container import AppContainer

BOT_COMMANDS = [BotCommand(command="start", description="Начать заново")]


def create_bot(settings: Settings) -> Bot:
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def create_dispatcher(container: AppContainer) -> Dispatcher:
    from app.bot.handlers import chat, comments, reports, start

    dp = Dispatcher(storage=MemoryStorage())
    dp["container"] = container
    dp.update.outer_middleware(DbSessionMiddleware(container))
    dp.message.register(comments.on_group_message, F.chat.type.in_(comments.GROUP_TYPES))
    dp.message_reaction_count.register(reports.on_reaction_count)
    dp.include_routers(start.router, chat.router)
    return dp
