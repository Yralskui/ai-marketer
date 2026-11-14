"""Контекст выполнения агента: всё, что нужно инструментам."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Business, User
from app.images.base import ImageProvider
from app.llm.base import LLMProvider

if TYPE_CHECKING:
    from app.services.notifier import Notifier
    from app.services.object_storage import ObjectStorage


@dataclass
class AgentContext:
    session: AsyncSession
    user: User
    business: Business
    settings: Settings
    llm: LLMProvider
    images: ImageProvider
    # Через notifier инструменты показывают черновики пользователю (вместо прямой ссылки на бота)
    notifier: Notifier | None = None
    # Куда слать черновики; по умолчанию — личка пользователя
    chat_id: int | None = None
    # Что сделал агент за этот запуск (для ответа и тестов)
    created_post_ids: list[int] = field(default_factory=list)
    reference: str = ""
    request_id: int | None = None
    research_llm: LLMProvider | None = None
    web_context: str = ""
    progress: Callable[[str], None] | None = None
    interactive_drafts: bool = False
    storage: ObjectStorage | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None

    def update_progress(self, stage: str) -> None:
        if self.progress is not None:
            self.progress(stage)

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def business_id(self) -> int:
        return self.business.id

    @property
    def target_chat_id(self) -> int:
        return self.chat_id if self.chat_id is not None else self.user.telegram_id
