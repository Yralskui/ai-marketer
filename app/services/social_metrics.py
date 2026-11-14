"""Почасовой сбор и чтение истории метрик без вызовов модели."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Business, Channel, Platform, Post, PostStatus
from app.db.publications import ChannelSnapshot, PostSnapshot
from app.publishers.analytics import AnalyticsError, AnalyticsSource, PostMetrics
from app.utils.time import utcnow

log = logging.getLogger(__name__)


async def collect_hour(
    sessions: async_sessionmaker[AsyncSession],
    sources: Mapping[Platform, AnalyticsSource],
    now: datetime | None = None,
) -> None:
    hour = (now or utcnow()).replace(minute=0, second=0, microsecond=0)
    if not sources:
        return
    async with sessions() as session:
        channels = list(
            (
                await session.execute(
                    select(Channel, Business.settings)
                    .join(Business, Business.id == Channel.business_id)
                    .where(Channel.platform.in_(sources))
                    .order_by(Channel.id)
                )
            ).all()
        )

    # SQLite в тестах может делить одно соединение между сессиями.
    parallel = 1 if sessions.kw["bind"].dialect.name == "sqlite" else 4
    for offset in range(0, len(channels), parallel):
        await asyncio.gather(
            *(
                _collect_channel(sessions, sources[channel.platform], channel, hour)
                for channel, settings in channels[offset : offset + parallel]
                if not (settings or {}).get("miniapp_archived")
            )
        )


async def _collect_channel(
    sessions: async_sessionmaker[AsyncSession],
    source: AnalyticsSource,
    channel: Channel,
    hour: datetime,
) -> None:
    try:
        async with sessions() as session:
            posts = (
                await session.execute(
                    select(Post.id, Post.external_id)
                    .where(
                        Post.channel_id == channel.id,
                        Post.business_id == channel.business_id,
                        Post.platform == channel.platform,
                        Post.status == PostStatus.PUBLISHED,
                        Post.external_id.is_not(None),
                        ~Post.external_id.like("dry-run-%"),
                        ~exists().where(
                            PostSnapshot.post_id == Post.id,
                            (PostSnapshot.hour == hour) | PostSnapshot.deleted.is_(True),
                        ),
                    )
                    .order_by(Post.id)
                )
            ).all()
            has_channel_snapshot = await session.get(ChannelSnapshot, (channel.id, hour))
        subscribers = None
        if channel.is_active and has_channel_snapshot is None:
            try:
                subscribers = (await source.channel_metrics(channel.external_id)).subscribers
            except AnalyticsError as exc:
                log.warning("Канал #%s: %s", channel.id, exc)

        counters: dict[int, PostMetrics] = {}
        # Адаптер получает ограниченный пакет, чтобы сбой не отменял весь канал.
        for offset in range(0, len(posts), 100):
            ids = {external_id: post_id for post_id, external_id in posts[offset : offset + 100]}
            try:
                batch = await source.post_metrics(channel.external_id, list(ids))
                counters.update({ids[key]: value for key, value in batch.items() if key in ids})
            except AnalyticsError as exc:
                log.warning("Канал #%s: %s", channel.id, exc)
                break

        async with sessions.begin() as session:
            insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
            if subscribers is not None:
                await session.execute(
                    insert(ChannelSnapshot)
                    .values(channel_id=channel.id, hour=hour, subscribers=subscribers)
                    .on_conflict_do_nothing()
                )
            rows = [
                {
                    "post_id": post_id,
                    "hour": hour,
                    "views": value.views,
                    "forwards": value.forwards,
                    "deleted": value.deleted,
                }
                for post_id, value in counters.items()
                if value.deleted or value.views is not None or value.forwards is not None
            ]
            for offset in range(0, len(rows), 100):
                await session.execute(
                    insert(PostSnapshot)
                    .values(rows[offset : offset + 100])
                    .on_conflict_do_nothing()
                )
    except Exception:
        log.exception("Сбор метрик для канала #%s завершился ошибкой", channel.id)


async def bounds(session: AsyncSession, business_id: int) -> datetime | None:
    channel_start = await session.scalar(
        select(func.min(ChannelSnapshot.hour))
        .join(Channel)
        .where(Channel.business_id == business_id)
    )
    post_start = await session.scalar(
        select(func.min(PostSnapshot.hour)).join(Post).where(Post.business_id == business_id)
    )
    return min((item for item in (channel_start, post_start) if item is not None), default=None)


def timestamp(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat()


async def series(
    session: AsyncSession, business_id: int, start: datetime, end: datetime, post_id: int | None
) -> dict:
    channels: dict[int, dict] = {}
    for channel, point in (
        await session.execute(
            select(Channel, ChannelSnapshot)
            .join(ChannelSnapshot)
            .where(Channel.business_id == business_id, ChannelSnapshot.hour.between(start, end))
            .order_by(ChannelSnapshot.hour, Channel.id)
        )
    ).all():
        group = channels.setdefault(
            channel.id, {"name": channel.title or channel.external_id, "points": []}
        )
        group["points"].append([timestamp(point.hour), point.subscribers])

    post_points = []
    if post_id is not None:
        post_points = [
            {
                "at": timestamp(point.hour),
                "views": point.views,
                "forwards": point.forwards,
                "deleted": point.deleted,
            }
            for point in (
                await session.scalars(
                    select(PostSnapshot)
                    .join(Post)
                    .where(
                        Post.business_id == business_id,
                        Post.id == post_id,
                        PostSnapshot.hour.between(start, end),
                    )
                    .order_by(PostSnapshot.hour)
                )
            ).all()
        ]
    available_posts = (
        await session.execute(
            select(Post.id, Post.topic)
            .where(
                Post.business_id == business_id,
                exists().where(PostSnapshot.post_id == Post.id),
            )
            .order_by(Post.id.desc())
        )
    ).all()
    return {
        "channels": list(channels.values()),
        "posts": [{"id": id_, "topic": topic} for id_, topic in available_posts],
        "points": post_points,
    }


async def latest_metrics(
    session: AsyncSession, business_id: int, post_ids: list[int]
) -> tuple[dict[str, int], dict[int, PostMetrics]]:
    channel_hours = (
        select(ChannelSnapshot.channel_id, func.max(ChannelSnapshot.hour).label("hour"))
        .join(Channel)
        .where(Channel.business_id == business_id, Channel.is_active.is_(True))
        .group_by(ChannelSnapshot.channel_id)
        .subquery()
    )
    subscribers = {
        platform.value: count
        for platform, count in (
            await session.execute(
                select(Channel.platform, ChannelSnapshot.subscribers)
                .join(Channel)
                .join(
                    channel_hours,
                    (channel_hours.c.channel_id == ChannelSnapshot.channel_id)
                    & (channel_hours.c.hour == ChannelSnapshot.hour),
                )
            )
        ).all()
    }
    if not post_ids:
        return subscribers, {}
    ranked = (
        select(
            PostSnapshot.post_id,
            PostSnapshot.views,
            PostSnapshot.forwards,
            PostSnapshot.deleted,
            func.row_number()
            .over(partition_by=PostSnapshot.post_id, order_by=PostSnapshot.hour.desc())
            .label("rank"),
        )
        .join(Post)
        .where(
            Post.business_id == business_id, Post.id.in_(post_ids), PostSnapshot.deleted.is_(False)
        )
        .subquery()
    )
    counters = {
        id_: PostMetrics(views, forwards, deleted)
        for id_, views, forwards, deleted in (
            await session.execute(
                select(ranked.c.post_id, ranked.c.views, ranked.c.forwards, ranked.c.deleted).where(
                    ranked.c.rank == 1
                )
            )
        ).all()
    }
    return subscribers, counters
