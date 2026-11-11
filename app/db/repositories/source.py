from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Source, SourceType


class SourceRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, business_id: int, type_: SourceType, origin: str, text: str) -> Source:
        source = Source(business_id=business_id, type=type_, origin=origin[:1024], text=text)
        self.session.add(source)
        await self.session.flush()
        return source

    async def list_for_business(self, business_id: int) -> list[Source]:
        result = await self.session.execute(
            select(Source).where(Source.business_id == business_id).order_by(Source.id)
        )
        return list(result.scalars())
