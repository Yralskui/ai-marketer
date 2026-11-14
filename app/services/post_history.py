"""Версии содержимого и события согласования."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EventLog, Post, PostRevision
from app.db.publications import (
    PostAttachments,
    RevisionAttachments,
    RevisionDecision,
    RevisionProvenance,
)
from app.db.repositories import PostRepo


@dataclass(frozen=True)
class RevisionSource:
    kind: str
    request_id: int | None = None
    author_id: int | None = None
    request_text: str | None = None


async def latest_revision(session: AsyncSession, post: Post) -> PostRevision | None:
    return await session.scalar(
        select(PostRevision)
        .where(PostRevision.post_id == post.id)
        .order_by(PostRevision.id.desc())
        .limit(1)
    )


async def save_revision(
    session: AsyncSession,
    post: Post,
    source: RevisionSource,
    *,
    instruction: str | None = None,
    image_change: bool = False,
    image_source: str | None = None,
    image_prompt: str | None = None,
    image_model: str | None = None,
    media_id: int | None = None,
) -> None:
    parent = await latest_revision(session, post)
    previous = await session.get(RevisionProvenance, parent.id) if parent else None
    if previous is not None and not image_change:
        image_source = previous.image_source
        image_prompt = previous.image_prompt
        image_model = previous.image_model
        media_id = previous.media_id
    elif post.image_path and image_source is None:
        image_source = "legacy"
    revision = await PostRepo(session).add_revision(post, post.text, instruction)
    attachments = await session.get(PostAttachments, post.id)
    if attachments:
        session.add(RevisionAttachments(revision_id=revision.id, items=attachments.items))
    session.add(
        RevisionProvenance(
            revision_id=revision.id,
            parent_id=parent.id if parent else None,
            request_id=source.request_id,
            author_id=source.author_id,
            source=source.kind,
            request_text=source.request_text,
            image_path=post.image_path,
            image_source=image_source,
            image_prompt=image_prompt,
            image_model=image_model,
            media_id=media_id,
        )
    )


async def record_decision(
    session: AsyncSession, post: Post, event: EventLog, user_id: int | None = None
) -> None:
    revision = await latest_revision(session, post)
    if revision is not None:
        session.add(RevisionDecision(event_id=event.id, revision_id=revision.id, user_id=user_id))
