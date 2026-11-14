"""Сборка зависимостей приложения в одном месте.

Хендлеры бота получают контейнер через aiogram DI (аргумент `container`),
задачи планировщика — аргументом.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.agent.core import Agent
from app.agent.registry import ToolRegistry, default_registry
from app.config import Settings
from app.db.base import Database
from app.db.models import Platform, Post
from app.db.repositories import BusinessRepo, ChannelRepo
from app.images.base import ImageProvider
from app.images.factory import create_image_provider
from app.llm.base import LLMProvider
from app.llm.factory import create_llm
from app.publishers.analytics import AnalyticsSource
from app.publishers.base import Publisher
from app.publishers.max import MaxPublisher
from app.publishers.vk import VKPublisher
from app.publishers.vk_analytics import VKAnalytics
from app.publishers.vk_api import VKClients
from app.services.document_uploads import UploadLinks
from app.services.notifier import Notifier
from app.services.object_storage import ObjectStorage
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
    upload_links: UploadLinks = field(default_factory=UploadLinks)
    storage: ObjectStorage | None = None
    analytics_sources: dict[Platform, AnalyticsSource] = field(default_factory=dict)


def build_container(settings: Settings, bot: Bot | None = None) -> AppContainer:
    import app.agent.tools  # noqa: F401  — регистрирует инструменты

    # ВКонтакте не зависит от бота: публикация и метрики работают, как только заданы ключи
    vk_clients = VKClients(settings.vk_tokens, version=settings.vk_api_version)
    publishers: dict[Platform, Publisher] = {
        Platform.VK: VKPublisher(
            vk_clients, public_url=settings.miniapp_public_url, secret=settings.bot_token
        ),
        Platform.MAX: MaxPublisher(),
    }
    analytics_sources: dict[Platform, AnalyticsSource] = {}
    if vk_clients:
        analytics_sources[Platform.VK] = VKAnalytics(vk_clients)
    db = Database(settings.effective_database_url)
    notifier: Notifier | None = None
    if bot is not None:
        from app.bot.notifier import BotNotifier
        from app.publishers.telegram import TelegramPublisher
        from app.publishers.telegram_analytics import TelegramAnalytics

        async def channel_of(post: Post) -> str | None:
            async with db.sessionmaker() as session:
                repo = ChannelRepo(session)
                channel = await repo.get(post.channel_id) if post.channel_id else None
                channel = channel or await repo.get_active(
                    post.business_id, Platform(post.platform)
                )
                return channel.title or channel.external_id if channel else None

        async def brand_issues(post: Post) -> list[str]:
            from app.services.brand import check_text, get_guide

            async with db.sessionmaker() as session:
                business = await BusinessRepo(session).get(post.business_id)
                return check_text(post.text, get_guide(business)) if business else []

        publishers[Platform.TELEGRAM] = TelegramPublisher(bot)
        analytics_sources[Platform.TELEGRAM] = TelegramAnalytics(
            bot, api_id=settings.telegram_api_id, api_hash=settings.telegram_api_hash
        )
        notifier = BotNotifier(bot, settings, channel_lookup=channel_of, brand_check=brand_issues)

    async def alert_admins(text: str) -> None:
        if notifier is not None:
            for admin_id in settings.allowed_ids:
                await notifier.send_text(admin_id, text)

    # Учёт расходов: каждый вызов LLM и генерации картинок записывается на компанию
    recorder = UsageRecorder(Prices.from_settings(settings), settings.tz, alert=alert_admins)

    return AppContainer(
        settings=settings,
        db=db,
        llm=MeteredLLM(
            create_llm(settings), recorder, settings.llm_model_light or settings.llm_model
        ),
        images=MeteredImages(create_image_provider(settings), recorder),
        publishing=PublishingService(publishers, dry_run=settings.publish_dry_run),
        agent=Agent(
            default_registry,
            max_steps=settings.agent_max_steps,
            history_limit=settings.agent_history_limit,
            # Шаг агента ждёт все повторы LLM: бюджет + последняя попытка + запас
            step_timeout=settings.llm_retry_budget_seconds + settings.llm_timeout + 10,
        ),
        registry=default_registry,
        notifier=notifier,
        bot=bot,
        analytics_sources=analytics_sources,
        storage=ObjectStorage(settings) if settings.s3_endpoint else None,
    )
