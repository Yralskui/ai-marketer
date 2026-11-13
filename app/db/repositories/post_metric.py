from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PostMetric


class PostMetricRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_post(self, post_id: int) -> PostMetric | None:
        result = await self.session.execute(select(PostMetric).where(PostMetric.post_id == post_id))
        return result.scalar_one_or_none()

    async def set_reactions(
        self, post_id: int, business_id: int, reactions: dict[str, Any]
    ) -> PostMetric:
        metric = await self.get_for_post(post_id)
        if metric is None:
            metric = PostMetric(post_id=post_id, business_id=business_id)
            self.session.add(metric)
        metric.reactions = dict(reactions)
        metric.reactions_total = sum(int(v) for v in reactions.values())
        await self.session.flush()
        return metric

    async def for_posts(self, post_ids: list[int]) -> dict[int, PostMetric]:
        if not post_ids:
            return {}
        result = await self.session.execute(
            select(PostMetric).where(PostMetric.post_id.in_(post_ids))
        )
        return {m.post_id: m for m in result.scalars()}
