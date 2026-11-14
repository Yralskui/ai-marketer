from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.knowledge import ChatHistoryClear
from app.db.models import AgentMessage, MessageRole
from app.db.publications import PlanOrigin, RevisionProvenance


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
            cleared = await self.session.get(ChatHistoryClear, (user_id, business_id))
            if cleared:
                query = query.where(AgentMessage.id > cleared.through_id)
        result = await self.session.execute(query.order_by(AgentMessage.id.desc()).limit(n))
        return list(reversed(list(result.scalars())))

    async def clear(self, user_id: int, business_id: int) -> None:
        scope = (AgentMessage.user_id == user_id, AgentMessage.business_id == business_id)
        through = await self.session.scalar(select(func.max(AgentMessage.id)).where(*scope))
        if through is None:
            return
        current = await self.session.get(ChatHistoryClear, (user_id, business_id))
        await self.session.merge(
            ChatHistoryClear(
                user_id=user_id,
                business_id=business_id,
                through_id=max(through, current.through_id if current else 0),
            )
        )
        await self.session.execute(
            delete(AgentMessage).where(
                *scope,
                AgentMessage.id <= through,
                ~select(RevisionProvenance.revision_id)
                .where(RevisionProvenance.request_id == AgentMessage.id)
                .exists(),
                ~select(PlanOrigin.plan_id)
                .where(PlanOrigin.request_id == AgentMessage.id)
                .exists(),
            )
        )
