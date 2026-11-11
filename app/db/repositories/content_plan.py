from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ContentPlan, ContentPlanItem


class ContentPlanRepo:
    """Заготовка для потока A (контент-план)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, plan: ContentPlan) -> ContentPlan:
        self.session.add(plan)
        await self.session.flush()
        return plan

    async def get(self, plan_id: int) -> ContentPlan | None:
        return await self.session.get(ContentPlan, plan_id)

    async def add_item(self, item: ContentPlanItem) -> ContentPlanItem:
        self.session.add(item)
        await self.session.flush()
        return item

    async def list_items(self, plan_id: int) -> list[ContentPlanItem]:
        result = await self.session.execute(
            select(ContentPlanItem)
            .where(ContentPlanItem.plan_id == plan_id)
            .order_by(ContentPlanItem.planned_at)
        )
        return list(result.scalars())
