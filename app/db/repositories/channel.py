from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel, Platform


class ChannelRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, channel_id: int) -> Channel | None:
        return await self.session.get(Channel, channel_id)

    async def get_active(self, business_id: int, platform: Platform) -> Channel | None:
        result = await self.session.execute(
            select(Channel)
            .where(
                Channel.business_id == business_id,
                Channel.platform == platform,
                Channel.is_active.is_(True),
            )
            .order_by(Channel.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self, business_id: int, platform: Platform, external_id: str, title: str = ""
    ) -> Channel:
        """Один активный канал на платформу: обновляет external_id, если он сменился в .env."""
        channel = await self.get_active(business_id, platform)
        if channel is None:
            channel = Channel(
                business_id=business_id, platform=platform, external_id=external_id, title=title
            )
            self.session.add(channel)
        else:
            channel.external_id = external_id
            if title:
                channel.title = title
        await self.session.flush()
        return channel
