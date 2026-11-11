"""Задача планировщика publish_due_posts с фейковым публикатором."""

from __future__ import annotations

from datetime import UTC, timedelta

from app.db.models import Platform, PostStatus
from app.db.repositories import PostRepo
from app.scheduler.jobs import publish_due_posts
from app.services import approval, posts
from app.services.publishing import PublishingService
from app.utils.time import utcnow
from tests.conftest import FakePublisher


async def _scheduled_post(session, owner, llm, images, topic: str, in_minutes: int):
    user, business = owner
    post = await posts.create_draft(session, llm, images, business, topic)
    await approval.submit_for_approval(session, post, None, None)
    when = (utcnow() + timedelta(minutes=in_minutes)).replace(tzinfo=UTC)
    await approval.schedule(session, post, when, user)
    await session.commit()
    return post


async def test_publishes_only_due_posts(db, session, owner, llm, images, notifier):
    user, business = owner
    due = await _scheduled_post(session, owner, llm, images, "уже пора", in_minutes=5)
    later = await _scheduled_post(session, owner, llm, images, "ещё рано", in_minutes=120)
    pending = await posts.create_draft(session, llm, images, business, "не одобрен")
    await approval.submit_for_approval(session, pending, None, None)

    publisher = FakePublisher()
    service = PublishingService({Platform.TELEGRAM: publisher})
    # «Перематываем» время на 10 минут вперёд
    count = await publish_due_posts(
        db.sessionmaker, service, notifier, now=utcnow() + timedelta(minutes=10)
    )

    assert count == 1
    assert publisher.published == [due.id]
    async with db.sessionmaker() as fresh:
        repo = PostRepo(fresh)
        assert (await repo.get(due.id)).status == PostStatus.PUBLISHED
        assert (await repo.get(later.id)).status == PostStatus.SCHEDULED
        assert (await repo.get(pending.id)).status == PostStatus.PENDING_APPROVAL
    assert notifier.texts and notifier.texts[0][0] == user.telegram_id
    assert f"#{due.id}" in notifier.texts[0][1]


async def test_failed_publication_marks_failed_and_notifies(
    db, session, owner, llm, images, notifier
):
    post = await _scheduled_post(session, owner, llm, images, "упадёт", in_minutes=1)
    service = PublishingService({Platform.TELEGRAM: FakePublisher(fail=True)})

    count = await publish_due_posts(
        db.sessionmaker, service, notifier, now=utcnow() + timedelta(minutes=5)
    )

    assert count == 0
    async with db.sessionmaker() as fresh:
        stored = await PostRepo(fresh).get(post.id)
        assert stored.status == PostStatus.FAILED
        assert stored.publish_attempts == 1
    assert "Не удалось" in notifier.texts[0][1]


async def test_nothing_due(db, notifier):
    service = PublishingService({Platform.TELEGRAM: FakePublisher()})
    assert await publish_due_posts(db.sessionmaker, service, notifier) == 0
    assert notifier.texts == []
