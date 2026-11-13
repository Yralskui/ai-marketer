from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business


class BusinessRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, business_id: int) -> Business | None:
        return await self.session.get(Business, business_id)

    async def get_by_owner(self, owner_id: int) -> Business | None:
        result = await self.session.execute(
            select(Business).where(Business.owner_id == owner_id).order_by(Business.id).limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_invite(self, code: str) -> Business | None:
        result = await self.session.execute(select(Business).where(Business.invite_code == code))
        return result.scalar_one_or_none()

    async def create(
        self, owner_id: int, name: str = "Мой бизнес", invite_code: str | None = None
    ) -> Business:
        business = Business(owner_id=owner_id, name=name, profile={}, invite_code=invite_code)
        self.session.add(business)
        await self.session.flush()
        return business

    async def list_all(self) -> list[Business]:
        result = await self.session.execute(select(Business).order_by(Business.id))
        return list(result.scalars())

    async def set_profile(self, business: Business, profile: dict[str, Any]) -> None:
        # Новый dict, чтобы SQLAlchemy заметил изменение JSON-поля
        business.profile = dict(profile)
        if profile.get("name"):
            business.name = str(profile["name"])[:255]
        await self.session.flush()
