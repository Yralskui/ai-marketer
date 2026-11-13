from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentMessage, MessageRole


class AgentMessageRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        user_id: int,
        role: MessageRole,
        content: str | None = None,
        *,
        business_id: int | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
    ) -> AgentMessage:
        msg = AgentMessage(
            user_id=user_id,
            business_id=business_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            name=name,
        )
        self.session.add(msg)
        await self.session.flush()
        return msg

    async def last_n(
        self, user_id: int, n: int, *, business_id: int | None = None
    ) -> list[AgentMessage]:
        """Последние n сообщений в хронологическом порядке.

        С business_id — только диалог по этой компании: администратор, переключаясь
        между компаниями, не должен тащить в контекст чужую историю.
        """
        query = select(AgentMessage).where(AgentMessage.user_id == user_id)
        if business_id is not None:
            query = query.where(AgentMessage.business_id == business_id)
        result = await self.session.execute(query.order_by(AgentMessage.id.desc()).limit(n))
        return list(reversed(list(result.scalars())))
