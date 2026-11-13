from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Post, PostRevision, PostStatus


class PostRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, post: Post) -> Post:
        self.session.add(post)
        await self.session.flush()
        return post

    async def get(self, post_id: int) -> Post | None:
        return await self.session.get(Post, post_id)

    async def find_published(self, channel_id: int, external_id: str) -> Post | None:
        result = await self.session.execute(
            select(Post).where(Post.channel_id == channel_id, Post.external_id == external_id)
        )
        return result.scalars().first()

    async def get_for_business(self, post_id: int, business_id: int) -> Post | None:
        post = await self.get(post_id)
        if post is None or post.business_id != business_id:
            return None
        return post

    async def list_for_business(
        self, business_id: int, status: PostStatus | None = None, limit: int = 20
    ) -> list[Post]:
        stmt = select(Post).where(Post.business_id == business_id)
        if status is not None:
            stmt = stmt.where(Post.status == status)
        stmt = stmt.order_by(Post.id.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def list_due(self, now_utc: datetime, limit: int = 50) -> list[Post]:
        """Запланированные посты, время которых наступило."""
        result = await self.session.execute(
            select(Post)
            .where(
                Post.status == PostStatus.SCHEDULED,
                Post.scheduled_at.is_not(None),
                Post.scheduled_at <= now_utc,
            )
            .order_by(Post.scheduled_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def count_by_status(self, business_id: int | None = None) -> dict[PostStatus, int]:
        stmt = select(Post.status, func.count(Post.id)).group_by(Post.status)
        if business_id is not None:
            stmt = stmt.where(Post.business_id == business_id)
        result = await self.session.execute(stmt)
        counts = {status: 0 for status in PostStatus}
        for status, count in result.all():
            counts[PostStatus(status)] = count
        return counts

    async def add_revision(self, post: Post, text: str, instruction: str | None) -> PostRevision:
        revision = PostRevision(post_id=post.id, text=text, instruction=instruction)
        self.session.add(revision)
        await self.session.flush()
        return revision

    async def list_revisions(self, post_id: int) -> list[PostRevision]:
        result = await self.session.execute(
            select(PostRevision).where(PostRevision.post_id == post_id).order_by(PostRevision.id)
        )
        return list(result.scalars())
