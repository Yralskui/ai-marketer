"""Реализация Notifier через Telegram-бота."""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from app.bot import keyboards, texts
from app.config import Settings
from app.db.models import Post

log = logging.getLogger(__name__)


class BotNotifier:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings

    async def send_draft(self, chat_id: int, post: Post, revision_no: int | None = None) -> None:
        if post.image_path and Path(post.image_path).exists():
            await self.bot.send_photo(chat_id, FSInputFile(post.image_path))
        await self.bot.send_message(
            chat_id,
            texts.draft_card(post, revision_no),
            reply_markup=keyboards.draft_keyboard(post.id),
        )

    async def send_text(self, chat_id: int, text: str) -> None:
        await self.bot.send_message(chat_id, text)


CAPTION_LIMIT = 1024


async def send_report(bot: Bot, chat_id: int, report) -> None:  # type: ignore[no-untyped-def]
    """Отчёт: график с подписью, а если текст длиннее подписи — график и текст отдельно."""
    if report.chart_path and Path(report.chart_path).exists():
        photo = FSInputFile(report.chart_path)
        if len(report.text) <= CAPTION_LIMIT:
            await bot.send_photo(chat_id, photo, caption=report.text)
            return
        await bot.send_photo(chat_id, photo)
    await bot.send_message(chat_id, report.text)
