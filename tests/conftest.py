"""Общие фикстуры: in-memory БД, mock-провайдеры, фейковые notifier и publisher."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import AgentContext
from app.config import Settings
from app.db.base import Database
from app.db.models import Business, Channel, Platform, Post, User
from app.images.mock_provider import MockImageProvider
from app.llm.mock_provider import MockLLMProvider
from app.publishers.base import Publisher, PublishError, PublishResult
from app.services.accounts import ensure_user_and_business


class FakeNotifier:
    def __init__(self) -> None:
        self.drafts: list[tuple[int, int]] = []
        self.texts: list[tuple[int, str]] = []

    async def send_draft(self, chat_id: int, post: Post) -> None:
        self.drafts.append((chat_id, post.id))

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append((chat_id, text))


class FakePublisher(Publisher):
    platform = Platform.TELEGRAM

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[int] = []

    async def publish(self, post: Post, channel: Channel) -> PublishResult:
        if self.fail:
            raise PublishError("канал недоступен")
        self.published.append(post.id)
        return PublishResult(external_id=f"msg-{post.id}", url=f"https://t.me/test/{post.id}")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        bot_token="",
        allowed_user_ids="111",
        telegram_channel_id="@test_channel",
        llm_provider="mock",
        image_provider="mock",
        database_url="sqlite+aiosqlite:///:memory:",
        publish_dry_run=False,
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.effective_database_url)
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
async def session(db: Database) -> AsyncIterator[AsyncSession]:
    async with db.sessionmaker() as s:
        yield s


@pytest.fixture
async def owner(session: AsyncSession, settings: Settings) -> tuple[User, Business]:
    user, business = await ensure_user_and_business(session, settings, 111, "Тестовый Владелец")
    business.name = "Кофейня «Зерно»"
    await session.commit()
    return user, business


@pytest.fixture
def llm() -> MockLLMProvider:
    return MockLLMProvider()


@pytest.fixture
def images(tmp_path: Path) -> MockImageProvider:
    return MockImageProvider(tmp_path / "images")


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def agent_ctx(session, owner, settings, llm, images, notifier) -> AgentContext:
    user, business = owner
    return AgentContext(
        session=session,
        user=user,
        business=business,
        settings=settings,
        llm=llm,
        images=images,
        notifier=notifier,
    )
