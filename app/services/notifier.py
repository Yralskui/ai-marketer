"""Интерфейс уведомлений пользователя.

Сервисы, агент и планировщик не знают про aiogram: они вызывают Notifier.
Реализация для Telegram — app/bot/notifier.py, в тестах — фейк.

⚠️ Стабильный интерфейс: меняется только по согласованию с напарником.
"""

from __future__ import annotations

from typing import Protocol

from app.db.models import Post


class Notifier(Protocol):
    async def send_draft(self, chat_id: int, post: Post) -> None:
        """Показать черновик с кнопками согласования."""

    async def send_text(self, chat_id: int, text: str) -> None:
        """Отправить простое сообщение (HTML уже экранирован вызывающим)."""
