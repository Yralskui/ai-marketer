"""Надёжность публикации: без дублей и с повтором временных ошибок Telegram (п. 5 ТЗ)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message

from app.db.models import Channel, Platform, Post, PostStatus
from app.db.repositories import PostRepo
from app.publishers.base import PublishError, PublishResult
from app.publishers.telegram import TelegramPublisher
from app.scheduler.jobs import publish_due_posts
from app.services import approval, posts
from app.services.publishing import PublishingService
from app.utils.time import utcnow
from tests.conftest import FakePublisher

ADMIN_CHAT = 999


class SlowPublisher(FakePublisher):
    """Публикует с задержкой, чтобы два обработчика успели пересечься."""

    async def publish(self, post: Post, channel: Channel) -> PublishResult:
        await asyncio.sleep(0.05)
        return await super().publish(post, channel)


async def _pending_post(session, owner, llm, images, topic="акция"):
    _, business = owner
    post = await posts.create_draft(session, llm, images, business, topic)
    await approval.submit_for_approval(session, post, None, None)
    await session.commit()
    return post


# ---------------------------------------------------------------- без дублей


async def test_double_click_publishes_once(db, session, owner, llm, images):
    """Два одновременных «Опубликовать сейчас» → один пост в канале."""
    user, _ = owner
    post = await _pending_post(session, owner, llm, images)
    publisher = SlowPublisher()
    service = PublishingService({Platform.TELEGRAM: publisher})

    async def click():
        async with db.sessionmaker() as s:
            stale = await PostRepo(s).get(post.id)
            return await approval.approve_and_publish(s, stale, user, service)

    first, second = await asyncio.gather(click(), click())

    assert publisher.published == [post.id]
    assert sorted([first.ok, second.ok]) == [False, True]
    assert (first if not first.ok else second).skipped
    async with db.sessionmaker() as fresh:
        stored = await PostRepo(fresh).get(post.id)
        assert stored.status == PostStatus.PUBLISHED
        assert stored.publish_attempts == 1


async def test_stale_object_cannot_republish(db, session, owner, llm, images):
    """Объект, прочитанный до публикации в другой сессии, не опубликует пост повторно
    и не вернёт его из published в approved."""
    user, _ = owner
    post = await _pending_post(session, owner, llm, images)
    publisher = FakePublisher()
    service = PublishingService({Platform.TELEGRAM: publisher})

    async with db.sessionmaker() as other:
        stale = await PostRepo(other).get(post.id)  # статус pending_approval в памяти
        assert (await approval.approve_and_publish(session, post, user, service)).ok

        outcome = await approval.approve_and_publish(other, stale, user, service)
        assert not outcome.ok and outcome.skipped
        assert stale.status == PostStatus.PUBLISHED

    assert publisher.published == [post.id]


async def test_claim_is_atomic(session, owner, llm, images):
    """Захват под публикацию удаётся только тому, кто видел актуальный счётчик попыток."""
    user, _ = owner
    post = await _pending_post(session, owner, llm, images)
    await approval.approve(session, post, user)
    await session.commit()
    repo = PostRepo(session)
    statuses = [PostStatus.APPROVED, PostStatus.SCHEDULED]

    assert await repo.claim_for_publish(post.id, 0, statuses)
    assert not await repo.claim_for_publish(post.id, 0, statuses)  # второй — опоздал


async def test_scheduler_skips_post_published_by_button(db, session, owner, llm, images):
    user, _ = owner
    post = await _pending_post(session, owner, llm, images)
    when = (utcnow() + timedelta(minutes=1)).replace(tzinfo=UTC)
    await approval.schedule(session, post, when, user)
    await session.commit()

    publisher = FakePublisher()
    service = PublishingService({Platform.TELEGRAM: publisher})
    later = utcnow() + timedelta(minutes=5)
    assert await publish_due_posts(db.sessionmaker, service, now=later) == 1
    assert await publish_due_posts(db.sessionmaker, service, now=later) == 0
    assert publisher.published == [post.id]


# ---------------------------------------------------------------- уведомления


async def test_failure_notifies_owner_and_admins(db, session, owner, llm, images, notifier):
    user, _ = owner
    post = await _pending_post(session, owner, llm, images)
    when = (utcnow() + timedelta(minutes=1)).replace(tzinfo=UTC)
    await approval.schedule(session, post, when, user)
    await session.commit()

    service = PublishingService({Platform.TELEGRAM: FakePublisher(fail=True)})
    await publish_due_posts(
        db.sessionmaker,
        service,
        notifier,
        now=utcnow() + timedelta(minutes=5),
        admin_chat_ids=[ADMIN_CHAT, user.telegram_id],
    )

    recipients = [chat for chat, _ in notifier.texts]
    assert recipients == [user.telegram_id, ADMIN_CHAT]  # без повторов
    assert all("Не удалось" in text for _, text in notifier.texts)


# ---------------------------------------------------------------- повторы Telegram


def _message() -> Message:
    return Message(message_id=7, date=datetime.now(), chat=Chat(id=-1001, type="channel"))


class FlakyBot:
    """send_message сначала падает заданными ошибками, потом отвечает."""

    def __init__(self, errors: list[Exception]) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def send_message(self, *args, **kwargs) -> Message:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return _message()


METHOD = SendMessage(chat_id=1, text="x")


def _publisher(bot: FlakyBot, sleeps: list[float]) -> TelegramPublisher:
    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    return TelegramPublisher(bot, base_delay=1.0, sleep=fake_sleep)  # type: ignore[arg-type]


def _post_and_channel() -> tuple[Post, Channel]:
    post = Post(id=1, business_id=1, text="привет", status=PostStatus.APPROVED)
    return post, Channel(id=1, business_id=1, platform=Platform.TELEGRAM, external_id="@c")


async def test_retry_after_is_respected():
    bot = FlakyBot([TelegramRetryAfter(METHOD, "flood", 5), TelegramRetryAfter(METHOD, "f", 3)])
    sleeps: list[float] = []
    result = await _publisher(bot, sleeps).publish(*_post_and_channel())
    assert result.external_id == "7"
    assert sleeps == [5.0, 3.0]
    assert bot.calls == 3


async def test_server_errors_retried_then_fail():
    bot = FlakyBot([TelegramServerError(METHOD, "502")] * 4)
    sleeps: list[float] = []
    with pytest.raises(PublishError):
        await _publisher(bot, sleeps).publish(*_post_and_channel())
    assert bot.calls == 4  # первая попытка + 3 повтора
    assert sleeps == [1.0, 2.0, 4.0]


async def test_network_error_is_not_retried():
    """Сообщение могло дойти — повтор дал бы дубль в канале."""
    bot = FlakyBot([TelegramNetworkError(METHOD, "timeout")])
    sleeps: list[float] = []
    with pytest.raises(PublishError, match="могло уйти"):
        await _publisher(bot, sleeps).publish(*_post_and_channel())
    assert bot.calls == 1 and sleeps == []


async def test_too_long_retry_after_fails_fast():
    bot = FlakyBot([TelegramRetryAfter(METHOD, "flood", 3600)])
    sleeps: list[float] = []
    with pytest.raises(PublishError):
        await _publisher(bot, sleeps).publish(*_post_and_channel())
    assert sleeps == []
