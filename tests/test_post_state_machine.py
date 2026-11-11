"""Машина состояний поста и главное правило: публикация только после согласования."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.models import Platform, Post, PostStatus
from app.services import approval, posts
from app.services.posts import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    PublishNotAllowedError,
    transition,
)
from app.services.publishing import PublishingService
from app.utils.time import utcnow
from tests.conftest import FakePublisher


def make_post(status: PostStatus) -> Post:
    return Post(id=1, business_id=1, platform=Platform.TELEGRAM, text="t", status=status)


def test_every_status_has_transition_rules():
    assert set(ALLOWED_TRANSITIONS) == set(PostStatus)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (PostStatus.DRAFT, PostStatus.PENDING_APPROVAL),
        (PostStatus.PENDING_APPROVAL, PostStatus.APPROVED),
        (PostStatus.PENDING_APPROVAL, PostStatus.REJECTED),
        (PostStatus.APPROVED, PostStatus.SCHEDULED),
        (PostStatus.APPROVED, PostStatus.PUBLISHED),
        (PostStatus.SCHEDULED, PostStatus.PUBLISHED),
        (PostStatus.SCHEDULED, PostStatus.FAILED),
        (PostStatus.FAILED, PostStatus.APPROVED),
    ],
)
def test_allowed_transitions(old, new):
    post = make_post(old)
    transition(post, new)
    assert post.status == new


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (PostStatus.DRAFT, PostStatus.APPROVED),  # нельзя одобрить, минуя согласование
        (PostStatus.DRAFT, PostStatus.PUBLISHED),
        (PostStatus.PENDING_APPROVAL, PostStatus.PUBLISHED),
        (PostStatus.PENDING_APPROVAL, PostStatus.SCHEDULED),
        (PostStatus.REJECTED, PostStatus.APPROVED),
        (PostStatus.PUBLISHED, PostStatus.DRAFT),
    ],
)
def test_forbidden_transitions(old, new):
    with pytest.raises(InvalidTransitionError):
        transition(make_post(old), new)


@pytest.mark.parametrize(
    "status",
    [PostStatus.DRAFT, PostStatus.PENDING_APPROVAL, PostStatus.REJECTED, PostStatus.FAILED],
)
async def test_publish_without_approval_is_blocked(session, status):
    """Сервисный слой не даёт опубликовать неодобренный пост — даже в обход UI."""
    publisher = FakePublisher()
    service = PublishingService({Platform.TELEGRAM: publisher})
    post = make_post(status)
    with pytest.raises(PublishNotAllowedError):
        await service.publish(session, post)
    assert publisher.published == []
    assert post.status == status


async def test_full_flow_draft_to_published(session, owner, llm, images, notifier):
    user, business = owner
    publisher = FakePublisher()
    service = PublishingService({Platform.TELEGRAM: publisher})

    post = await posts.create_draft(session, llm, images, business, "осеннее меню")
    assert post.status == PostStatus.DRAFT
    assert post.channel_id is not None  # канал из TELEGRAM_CHANNEL_ID

    await approval.submit_for_approval(session, post, notifier, user.telegram_id)
    assert post.status == PostStatus.PENDING_APPROVAL
    assert notifier.drafts == [(user.telegram_id, post.id)]

    outcome = await approval.approve_and_publish(session, post, user, service)
    assert outcome.ok
    assert post.status == PostStatus.PUBLISHED
    assert post.approved_by_user_id == user.id
    assert post.external_url == f"https://t.me/test/{post.id}"
    assert publisher.published == [post.id]


async def test_publish_failure_sets_failed(session, owner, llm, images):
    user, business = owner
    service = PublishingService({Platform.TELEGRAM: FakePublisher(fail=True)})
    post = await posts.create_draft(session, llm, images, business, "акция")
    await approval.submit_for_approval(session, post, None, None)
    outcome = await approval.approve_and_publish(session, post, user, service)
    assert not outcome.ok
    assert post.status == PostStatus.FAILED
    assert "канал недоступен" in (post.error or "")


async def test_dry_run_does_not_call_publisher(session, owner, llm, images):
    user, business = owner
    publisher = FakePublisher()
    service = PublishingService({Platform.TELEGRAM: publisher}, dry_run=True)
    post = await posts.create_draft(session, llm, images, business, "новинки")
    await approval.submit_for_approval(session, post, None, None)
    outcome = await approval.approve_and_publish(session, post, user, service)
    assert outcome.ok and outcome.result and outcome.result.dry_run
    assert publisher.published == []
    assert post.status == PostStatus.PUBLISHED


async def test_revision_requires_new_approval(session, owner, llm, images):
    user, business = owner
    post = await posts.create_draft(session, llm, images, business, "скидки")
    await approval.submit_for_approval(session, post, None, None)
    await approval.schedule(session, post, utcnow_aware(hours=2), user)
    assert post.status == PostStatus.SCHEDULED

    await posts.revise(session, llm, business, post, "сделай короче")
    assert post.status == PostStatus.DRAFT
    assert post.scheduled_at is None
    with pytest.raises(PublishNotAllowedError):
        posts.ensure_publishable(post)


async def test_schedule_in_past_rejected(session, owner, llm, images):
    user, business = owner
    post = await posts.create_draft(session, llm, images, business, "скидки")
    await approval.submit_for_approval(session, post, None, None)
    with pytest.raises(ValueError):
        await approval.schedule(session, post, utcnow_aware(hours=-1), user)


def utcnow_aware(hours: int):
    from datetime import UTC

    return (utcnow() + timedelta(hours=hours)).replace(tzinfo=UTC)
