"""Интерфейс публикатора соцсети.

⚠️ Стабильный интерфейс: меняется только по согласованию с напарником.
Публикатор НЕ проверяет согласование — это делает services/publishing.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.db.models import Channel, Platform, Post


class PublishError(RuntimeError):
    """Соцсеть отказала в публикации (нет прав, сеть, лимиты...)."""


@dataclass
class PublishResult:
    external_id: str
    url: str | None = None
    dry_run: bool = False


@dataclass
class ChannelCheck:
    ok: bool
    message: str


class Publisher(ABC):
    platform: Platform

    @abstractmethod
    async def publish(self, post: Post, channel: Channel) -> PublishResult:
        """Публикует пост в канал и возвращает его внешний ID и ссылку."""

    async def check_channel(self, channel: Channel) -> ChannelCheck:
        """Диагностика для /status: доступен ли канал и есть ли права."""
        return ChannelCheck(False, "Проверка канала для этой платформы не реализована")
