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
    # Необязательное пояснение человеку: пост вышел, но не полностью (например, без картинки)
    note: str | None = None


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

    async def edit(self, post: Post, channel: Channel, text: str) -> None:
        """Необязательная возможность: заменить текст уже опубликованного поста.

        Публикатор платформы, где это можно, переопределяет метод; по умолчанию — отказ.
        Решение «менять ли живой пост» принимает человек в Mini App, не агент.
        """
        raise PublishError("Эта платформа не поддерживает правку опубликованных постов")
