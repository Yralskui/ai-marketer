"""Реализация Notifier через Telegram-бота."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from app.bot import keyboards, texts
from app.config import Settings
from app.db.models import Post

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024


ChannelLookup = Callable[[Post], Awaitable[str | None]]
# Нарушения бренд-гида в тексте поста (подставляет container.py)
BrandCheck = Callable[[Post], Awaitable[list[str]]]


class BotNotifier:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        channel_lookup: ChannelLookup | None = None,
        brand_check: BrandCheck | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        # Куда уйдёт пост — для карточки согласования (подставляет container.py)
        self.channel_lookup = channel_lookup
        self.brand_check = brand_check

    async def send_draft(self, chat_id: int, post: Post, revision_no: int | None = None) -> None:
        url = self.settings.telegram_miniapp_url
        issues: list[str] = []
        if self.brand_check is not None:
            try:
                issues = await self.brand_check(post)
            except Exception:
                log.debug("Не удалось проверить пост #%s по бренд-гиду", post.id, exc_info=True)
        await self.bot.send_message(
            chat_id,
            f"Черновик №{post.id} готов к согласованию. Откройте его в Mini App."
            + texts.brand_issues_block(issues),
            reply_markup=keyboards.miniapp_keyboard(url) if url else None,
        )

    async def send_text(self, chat_id: int, text: str) -> None:
        await self.bot.send_message(chat_id, text)


async def send_report(bot: Bot, chat_id: int, report) -> None:  # type: ignore[no-untyped-def]
    """Отчёт: график с подписью, а если текст длиннее подписи — график и текст отдельно."""
    if report.chart_path and Path(report.chart_path).exists():
        photo = FSInputFile(report.chart_path)
        if len(report.text) <= CAPTION_LIMIT:
            await bot.send_photo(chat_id, photo, caption=report.text)
            return
        await bot.send_photo(chat_id, photo)
    await bot.send_message(chat_id, report.text)
