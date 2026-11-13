from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaFile


class MediaRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, media: MediaFile) -> MediaFile:
        self.session.add(media)
        await self.session.flush()
        return media

    async def get_for_business(self, media_id: int, business_id: int) -> MediaFile | None:
        media = await self.session.get(MediaFile, media_id)
        if media is None or media.business_id != business_id:
            return None
        return media

    async def find_by_unique_id(self, business_id: int, unique_id: str) -> MediaFile | None:
        result = await self.session.execute(
            select(MediaFile).where(
                MediaFile.business_id == business_id, MediaFile.telegram_unique_id == unique_id
            )
        )
        return result.scalars().first()

    async def list_for_business(self, business_id: int, limit: int = 10) -> list[MediaFile]:
        result = await self.session.execute(
            select(MediaFile)
            .where(MediaFile.business_id == business_id)
            .order_by(MediaFile.id.desc())
            .limit(limit)
        )
        return list(result.scalars())
