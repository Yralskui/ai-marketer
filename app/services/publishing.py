"""Публикация: выбор публикатора по платформе, dry-run, обработка ошибок."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel, EventType, Platform, Post, PostStatus
from app.db.repositories import ChannelRepo, EventLogRepo
from app.publishers.base import PublishError, Publisher, PublishResult
from app.services.posts import ensure_publishable, transition
from app.utils.time import utcnow

log = logging.getLogger(__name__)


@dataclass
class PublishOutcome:
    ok: bool
    post: Post
    result: PublishResult | None = None
    error: str | None = None


class PublishingService:
    def __init__(self, publishers: dict[Platform, Publisher], *, dry_run: bool = False) -> None:
        self.publishers = publishers
        self.dry_run = dry_run

    def get_publisher(self, platform: Platform) -> Publisher | None:
        return self.publishers.get(platform)

    async def publish(self, session: AsyncSession, post: Post) -> PublishOutcome:
        """Публикует пост. Ошибки соцсети -> статус failed (не исключение).

        PublishNotAllowedError (пост не одобрен) пробрасывается: это нарушение правил,
        а не сбой сети.
        """
        ensure_publishable(post)
        events = EventLogRepo(session)
        post.publish_attempts = (post.publish_attempts or 0) + 1
        try:
            result = await self._do_publish(session, post)
        except (PublishError, NotImplementedError, OSError) as exc:
            return await self._fail(session, post, str(exc) or type(exc).__name__)
        except Exception as exc:
            log.exception("Неожиданная ошибка публикации поста #%s", post.id)
            return await self._fail(session, post, f"{type(exc).__name__}: {exc}")

        post.external_id = result.external_id
        post.external_url = result.url
        post.published_at = utcnow()
        post.error = None
        transition(post, PostStatus.PUBLISHED)
        await events.log(
            EventType.POST_PUBLISHED,
            result.url or "",
            business_id=post.business_id,
            post_id=post.id,
            payload={"dry_run": result.dry_run, "platform": Platform(post.platform).value},
        )
        await session.commit()
        log.info("Пост #%s опубликован (dry_run=%s) %s", post.id, result.dry_run, result.url or "")
        return PublishOutcome(ok=True, post=post, result=result)

    async def _do_publish(self, session: AsyncSession, post: Post) -> PublishResult:
        if self.dry_run:
            log.info("[DRY-RUN] Публикация поста #%s:\n%s", post.id, post.text)
            return PublishResult(external_id=f"dry-run-{post.id}", url=None, dry_run=True)
        platform = Platform(post.platform)
        publisher = self.get_publisher(platform)
        if publisher is None:
            raise PublishError(f"Нет публикатора для платформы {platform.value}")
        channel = await self._resolve_channel(session, post)
        if channel is None:
            raise PublishError("Канал для публикации не настроен (TELEGRAM_CHANNEL_ID в .env)")
        return await publisher.publish(post, channel)

    async def _resolve_channel(self, session: AsyncSession, post: Post) -> Channel | None:
        repo = ChannelRepo(session)
        if post.channel_id:
            channel = await repo.get(post.channel_id)
            if channel and channel.is_active:
                return channel
        channel = await repo.get_active(post.business_id, Platform(post.platform))
        if channel:
            post.channel_id = channel.id
        return channel

    async def _fail(self, session: AsyncSession, post: Post, error: str) -> PublishOutcome:
        # TODO(поток B): автоматический повтор с паузой для временных ошибок
        log.warning("Пост #%s не опубликован: %s", post.id, error)
        post.error = error[:2000]
        transition(post, PostStatus.FAILED)
        await EventLogRepo(session).log(
            EventType.POST_FAILED, error[:500], business_id=post.business_id, post_id=post.id
        )
        await session.commit()
        return PublishOutcome(ok=False, post=post, error=error)
