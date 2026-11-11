"""Согласование постов.

ЕДИНСТВЕННОЕ место, которое переводит пост в approved/scheduled — то есть разрешает
публикацию. Сама публикация (services/publishing.py) дополнительно проверяет статус.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EventType, Post, PostStatus, User
from app.db.repositories import EventLogRepo
from app.services.notifier import Notifier
from app.services.posts import transition
from app.services.publishing import PublishingService, PublishOutcome
from app.utils.time import to_utc_naive, utcnow

log = logging.getLogger(__name__)


async def submit_for_approval(
    session: AsyncSession, post: Post, notifier: Notifier | None, chat_id: int | None
) -> Post:
    """draft -> pending_approval и показ черновика клиенту."""
    transition(post, PostStatus.PENDING_APPROVAL)
    await EventLogRepo(session).log(
        EventType.POST_SUBMITTED, "", business_id=post.business_id, post_id=post.id
    )
    # Фиксируем до отправки: кнопки под сообщением должны ссылаться на сохранённый пост
    await session.commit()
    if notifier is not None and chat_id is not None:
        await notifier.send_draft(chat_id, post)
    return post


async def approve(session: AsyncSession, post: Post, user: User) -> Post:
    transition(post, PostStatus.APPROVED)
    post.approved_at = utcnow()
    post.approved_by_user_id = user.id
    await EventLogRepo(session).log(
        EventType.POST_APPROVED, "", business_id=post.business_id, post_id=post.id
    )
    return post


async def reject(session: AsyncSession, post: Post, user: User) -> Post:
    transition(post, PostStatus.REJECTED)
    await EventLogRepo(session).log(
        EventType.POST_REJECTED,
        f"Отклонил пользователь {user.telegram_id}",
        business_id=post.business_id,
        post_id=post.id,
    )
    return post


async def schedule(session: AsyncSession, post: Post, when: datetime, user: User) -> Post:
    """Одобряет (если ещё не одобрен) и ставит пост в расписание на `when` (aware)."""
    when_utc = to_utc_naive(when)
    if when_utc <= utcnow():
        raise ValueError("Время публикации уже прошло")
    if post.status == PostStatus.PENDING_APPROVAL:
        await approve(session, post, user)
    if post.status != PostStatus.SCHEDULED:
        transition(post, PostStatus.SCHEDULED)
    post.scheduled_at = when_utc
    await EventLogRepo(session).log(
        EventType.POST_SCHEDULED,
        when_utc.isoformat(),
        business_id=post.business_id,
        post_id=post.id,
    )
    return post


async def approve_and_publish(
    session: AsyncSession, post: Post, user: User, publishing: PublishingService
) -> PublishOutcome:
    """Кнопка «Опубликовать сейчас»."""
    await approve(session, post, user)
    await session.commit()
    return await publishing.publish(session, post)
