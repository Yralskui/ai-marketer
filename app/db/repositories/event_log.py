from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EventLog


class EventLogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log(
        self,
        type_: str,
        message: str = "",
        *,
        business_id: int | None = None,
        post_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EventLog:
        event = EventLog(
            type=str(type_),
            message=message,
            business_id=business_id,
            post_id=post_id,
            payload=payload,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_recent(self, business_id: int, limit: int = 15) -> list[EventLog]:
        result = await self.session.execute(
            select(EventLog)
            .where(EventLog.business_id == business_id)
            .order_by(EventLog.id.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def list_since(self, business_id: int, since_utc: datetime) -> list[EventLog]:
        result = await self.session.execute(
            select(EventLog)
            .where(EventLog.business_id == business_id, EventLog.created_at >= since_utc)
            .order_by(EventLog.id)
        )
        return list(result.scalars())
