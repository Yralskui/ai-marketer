"""Публикация: выбор публикатора по платформе, dry-run, обработка ошибок.

Защита от двойной публикации (п. 5 ТЗ):
- внутри процесса — блокировка на пост (`post_lock`), её берёт «Опубликовать сейчас»;
- между процессами и для устаревших объектов — атомарный захват поста в БД
  (`PostRepo.claim_for_publish`) с коммитом до обращения к соцсети.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Channel,
    ContentPlanItem,
    ContentPlanItemStatus,
    EventType,
    Platform,
    Post,
    PostStatus,
)
from app.db.publications import PostAttachments
from app.db.repositories import ChannelRepo, EventLogRepo, PostRepo
from app.publishers.base import Publisher, PublishError, PublishResult
from app.services.post_history import record_decision
from app.services.posts import PUBLISHABLE_STATUSES, ensure_publishable, transition
from app.utils.time import utcnow

log = logging.getLogger(__name__)


@dataclass
class PublishOutcome:
    ok: bool
    post: Post
    result: PublishResult | None = None
    error: str | None = None
    # True — пост уже публикуется или опубликован другим обработчиком, ничего не делали
    skipped: bool = False


class PublishingService:
    def __init__(self, publishers: dict[Platform, Publisher], *, dry_run: bool = False) -> None:
        self.publishers = publishers
        self.dry_run = dry_run
        self._locks: dict[int, asyncio.Lock] = {}

    @asynccontextmanager
    async def post_lock(self, post_id: int) -> AsyncIterator[None]:
        """Последовательная обработка одного поста внутри процесса (двойной клик)."""
        # Замок на пост живёт до перезапуска: их столько, сколько постов публиковали
        lock = self._locks.setdefault(post_id, asyncio.Lock())
        async with lock:
            yield

    def get_publisher(self, platform: Platform) -> Publisher | None:
        return self.publishers.get(platform)

    async def publish(self, session: AsyncSession, post: Post) -> PublishOutcome:
        """Публикует пост. Ошибки соцсети -> статус failed (не исключение).

        PublishNotAllowedError (пост не одобрен) пробрасывается: это нарушение правил,
        а не сбой сети.
        """
        ensure_publishable(post)
        events = EventLogRepo(session)
        seen = post.publish_attempts or 0
        claimed = await PostRepo(session).claim_for_publish(post.id, seen, PUBLISHABLE_STATUSES)
        # Коммит ДО обращения к соцсети: другой обработчик должен увидеть захват
        await session.commit()
        await session.refresh(post)
        if not claimed:
            log.warning("Пост #%s уже публикуется или опубликован — пропускаю", post.id)
            return PublishOutcome(
                ok=False, post=post, error="пост уже публикуется или опубликован", skipped=True
            )
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
        if post.plan_item_id is not None:
            item = await session.get(ContentPlanItem, post.plan_item_id)
            if item is not None:
                item.status = ContentPlanItemStatus.PUBLISHED
        event = await events.log(
            EventType.POST_PUBLISHED,
            result.url or "",
            business_id=post.business_id,
            post_id=post.id,
            payload={"dry_run": result.dry_run, "platform": Platform(post.platform).value},
        )
        await record_decision(session, post, event)
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
            raise PublishError(
                "Канал для публикации не настроен — подключите его командой /channel"
            )
        attachments = await session.get(PostAttachments, post.id)
        post.delivery_media = attachments.items if attachments else None
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
        # Временные ошибки (429, 5xx) повторяет сам публикатор; сюда доходят окончательные
        log.warning("Пост #%s не опубликован: %s", post.id, error)
        post.error = error[:2000]
        transition(post, PostStatus.FAILED)
        await EventLogRepo(session).log(
            EventType.POST_FAILED, error[:500], business_id=post.business_id, post_id=post.id
        )
        await session.commit()
        return PublishOutcome(ok=False, post=post, error=error)
