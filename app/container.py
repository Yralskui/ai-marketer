"""Сборка зависимостей приложения в одном месте.

Хендлеры бота получают контейнер через aiogram DI (аргумент `container`),
задачи планировщика — аргументом.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agent.core import Agent
from app.agent.registry import ToolRegistry, default_registry
from app.config import Settings
from app.db.base import Database
from app.db.models import Platform
from app.images.base import ImageProvider
from app.images.factory import create_image_provider
from app.llm.base import LLMProvider
from app.llm.factory import create_llm
from app.publishers.base import Publisher
from app.publishers.max import MaxPublisher
from app.publishers.vk import VKPublisher
from app.services.notifier import Notifier
from app.services.publishing import PublishingService
from app.services.usage import MeteredImages, MeteredLLM, Prices, UsageRecorder

if TYPE_CHECKING:
    from aiogram import Bot
    from apscheduler.schedulers.asyncio import AsyncIOScheduler


@dataclass
class AppContainer:
    settings: Settings
    db: Database
    llm: LLMProvider
    images: ImageProvider
    publishing: PublishingService
    agent: Agent
    registry: ToolRegistry
    notifier: Notifier | None = None
    bot: Bot | None = None
    scheduler: AsyncIOScheduler | None = None


def build_container(settings: Settings, bot: Bot | None = None) -> AppContainer:
    import app.agent.tools  # noqa: F401  — регистрирует инструменты

    publishers: dict[Platform, Publisher] = {
        Platform.VK: VKPublisher(),
        Platform.MAX: MaxPublisher(),
    }
    notifier: Notifier | None = None
    if bot is not None:
        from app.bot.notifier import BotNotifier
        from app.publishers.telegram import TelegramPublisher

        publishers[Platform.TELEGRAM] = TelegramPublisher(bot)
        notifier = BotNotifier(bot, settings)

    async def alert_admins(text: str) -> None:
        if notifier is not None:
            for admin_id in settings.allowed_ids:
                await notifier.send_text(admin_id, text)

    # Учёт расходов: каждый вызов LLM и генерации картинок записывается на компанию
    recorder = UsageRecorder(Prices.from_settings(settings), settings.tz, alert=alert_admins)

    return AppContainer(
        settings=settings,
        db=Database(settings.effective_database_url),
        llm=MeteredLLM(create_llm(settings), recorder, settings.llm_model),
        images=MeteredImages(create_image_provider(settings), recorder),
        publishing=PublishingService(publishers, dry_run=settings.publish_dry_run),
        agent=Agent(
            default_registry,
            max_steps=settings.agent_max_steps,
            history_limit=settings.agent_history_limit,
            step_timeout=settings.llm_timeout * (settings.llm_max_retries + 1) + 5,
        ),
        registry=default_registry,
        notifier=notifier,
        bot=bot,
    )
