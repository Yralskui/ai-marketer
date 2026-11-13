"""Создание Bot и Dispatcher, подключение роутеров и middleware."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot.middlewares import DbSessionMiddleware
from app.config import Settings
from app.container import AppContainer

BOT_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="onboarding", description="Рассказать о бизнесе"),
    BotCommand(command="profile", description="Профиль бизнеса"),
    BotCommand(command="posts", description="Посты и статусы"),
    BotCommand(command="channel", description="Канал для публикаций"),
    BotCommand(command="comments", description="Комментарии и ответы"),
    BotCommand(command="report", description="Отчёт за неделю"),
    BotCommand(command="status", description="Диагностика"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="cancel", description="Отменить действие"),
]

# Администраторам дополнительно показываем команды управления компаниями
ADMIN_COMMANDS = [
    *BOT_COMMANDS[:1],
    BotCommand(command="companies", description="Компании (админ)"),
    BotCommand(command="new_company", description="Добавить компанию (админ)"),
    BotCommand(command="invite", description="Ссылка-приглашение клиента (админ)"),
    BotCommand(command="costs", description="Расходы на AI (админ)"),
    BotCommand(command="budget", description="Лимит расходов компании (админ)"),
    BotCommand(command="autoreply", description="Автоответ на комментарии (админ)"),
    BotCommand(command="clients", description="Клиенты и доступ (админ)"),
    BotCommand(command="log", description="Журнал действий"),
    *BOT_COMMANDS[1:],
]


def create_bot(settings: Settings) -> Bot:
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def create_dispatcher(container: AppContainer) -> Dispatcher:
    # Импорт внутри функции: роутеры — модульные синглтоны, подключаются один раз
    from app.bot.handlers import (
        admin,
        approval,
        audit,
        chat,
        comments,
        companies,
        costs,
        menu,
        onboarding,
        reports,
        start,
    )

    # TODO(поток B): RedisStorage для FSM, чтобы состояния переживали перезапуск
    dp = Dispatcher(storage=MemoryStorage())
    dp["container"] = container
    dp.update.outer_middleware(DbSessionMiddleware(container))
    dp.include_routers(
        comments.router,  # ПЕРВЫМ: забирает все сообщения из групп обсуждения
        start.router,
        menu.router,  # кнопки меню — раньше онбординга: прерывают любой ввод
        onboarding.router,
        approval.router,
        admin.router,
        companies.router,
        costs.router,
        audit.router,
        reports.router,
        chat.router,  # ПОСЛЕДНИМ: ловит весь оставшийся текст
    )
    return dp
