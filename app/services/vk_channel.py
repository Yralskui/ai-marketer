"""Подключение сообщества ВКонтакте к компании.

Сообщество находится по ссылке или id, проверяется (есть ключ, стена не закрыта) и
сохраняется как канал компании `platform=vk`. Ключи доступа остаются в настройках (.env):
в базе хранится только id и название сообщества.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business, Channel, EventType, Platform
from app.db.repositories import ChannelRepo, EventLogRepo
from app.publishers.base import ChannelCheck, PublishError
from app.publishers.vk import VKCommunity, VKPublisher


class VKConnectError(ValueError):
    """Сообщество нельзя подключить — текст показывается пользователю."""


async def connect(
    session: AsyncSession, publisher: VKPublisher | None, business: Business, raw: str
) -> tuple[Channel, ChannelCheck]:
    if publisher is None or not publisher.clients:
        raise VKConnectError(
            "ВКонтакте на сервере не настроен: администратору нужно задать VK_COMMUNITIES"
        )
    try:
        community: VKCommunity = await publisher.resolve(raw)
    except PublishError as exc:
        raise VKConnectError(str(exc)) from exc
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('vk-channel'), hashtext(:group_id))"),
            {"group_id": str(community.id)},
        )
    existing = await session.scalar(
        select(Channel.id)
        .where(
            Channel.platform == Platform.VK,
            Channel.external_id == str(community.id),
            Channel.business_id != business.id,
            Channel.is_active.is_(True),
        )
        .limit(1)
    )
    if existing is not None:
        raise VKConnectError("Сообщество уже подключено к другому профилю")
    probe = Channel(business_id=business.id, platform=Platform.VK, external_id=str(community.id))
    check = await publisher.check_channel(probe)
    if not check.ok:
        raise VKConnectError(check.message)
    channel = await ChannelRepo(session).upsert(
        business.id, Platform.VK, str(community.id), title=community.name
    )
    await EventLogRepo(session).log(
        EventType.CHANNEL_CHANGED, f"ВКонтакте: {community.name}", business_id=business.id
    )
    return channel, check


async def disconnect(session: AsyncSession, business: Business) -> None:
    channel = await ChannelRepo(session).get_active(business.id, Platform.VK)
    if channel is not None:
        channel.is_active = False
        await EventLogRepo(session).log(
            EventType.CHANNEL_CHANGED, "ВКонтакте отключён", business_id=business.id
        )
