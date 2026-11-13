from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Comment, CommentStatus


class CommentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, comment: Comment) -> Comment:
        self.session.add(comment)
        await self.session.flush()
        return comment

    async def get(self, comment_id: int) -> Comment | None:
        return await self.session.get(Comment, comment_id)

    async def find_by_message(self, chat_id: int, message_id: int) -> Comment | None:
        result = await self.session.execute(
            select(Comment).where(Comment.chat_id == chat_id, Comment.message_id == message_id)
        )
        return result.scalar_one_or_none()

    async def list_for_business(self, business_id: int, limit: int = 10) -> list[Comment]:
        result = await self.session.execute(
            select(Comment)
            .where(Comment.business_id == business_id)
            .order_by(Comment.id.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def count_by_status(self, business_id: int) -> dict[CommentStatus, int]:
        result = await self.session.execute(
            select(Comment.status, func.count())
            .where(Comment.business_id == business_id)
            .group_by(Comment.status)
        )
        counts = {s: 0 for s in CommentStatus}
        for status, n in result.all():
            counts[CommentStatus(status)] = n
        return counts
