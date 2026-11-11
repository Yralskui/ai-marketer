"""Создание Bot и Dispatcher, подключение роутеров и middleware."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot.middlewares import AccessMiddleware, DbSessionMiddleware
from app.config import Settings
from app.container import AppContainer

BOT_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="onboarding", description="Рассказать о бизнесе"),
    BotCommand(command="profile", description="Профиль бизнеса"),
    BotCommand(command="posts", description="Посты и статусы"),
    BotCommand(command="status", description="Диагностика"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="cancel", description="Отменить действие"),
]


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def create_dispatcher(container: AppContainer) -> Dispatcher:
    # Импорт внутри функции: роутеры — модульные синглтоны, подключаются один раз
    from app.bot.handlers import admin, approval, chat, onboarding, start

    # TODO(поток B): RedisStorage для FSM, чтобы состояния переживали перезапуск
    dp = Dispatcher(storage=MemoryStorage())
    dp["container"] = container
    dp.update.outer_middleware(AccessMiddleware(container.settings.allowed_ids))
    dp.update.outer_middleware(DbSessionMiddleware(container))
    dp.include_routers(
        start.router,
        onboarding.router,
        approval.router,
        admin.router,
        chat.router,  # ПОСЛЕДНИМ: ловит весь оставшийся текст
    )
    return dp
