"""Публикация в Telegram-канал (бот должен быть админом канала).

Временные ошибки повторяются автоматически, но только те, после которых Telegram
гарантированно НЕ принял сообщение: 429 (подождать retry_after) и 5xx. Сетевой сбой
не повторяем: сообщение могло уйти, а повтор дал бы дубль в канале.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile, InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message

from app.db.models import Channel, Platform, Post
from app.publishers.base import ChannelCheck, Publisher, PublishError, PublishResult
from app.publishers.telegram_text import format_text
from app.services.posts import PLATFORM_HARD_LIMITS, TELEGRAM_CAPTION_LIMIT

log = logging.getLogger(__name__)
T = TypeVar("T")

MAX_RETRIES = 3
# Дольше ждать не будем: пусть пост уйдёт в failed, а владелец получит уведомление
MAX_RETRY_WAIT = 60.0


class TelegramPublisher(Publisher):
    platform = Platform.TELEGRAM

    def __init__(
        self,
        bot: Bot,
        *,
        max_retries: int = MAX_RETRIES,
        base_delay: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.bot = bot
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._sleep = sleep

    async def _call(self, make: Callable[[], Awaitable[T]]) -> T:
        """Один вызов Bot API с повтором 429 и 5xx."""
        attempt = 0
        while True:
            try:
                return await make()
            except TelegramRetryAfter as exc:
                if attempt >= self.max_retries or exc.retry_after > MAX_RETRY_WAIT:
                    raise
                delay = float(exc.retry_after)
            except TelegramServerError:
                if attempt >= self.max_retries:
                    raise
                delay = min(MAX_RETRY_WAIT, self.base_delay * (2**attempt))
            attempt += 1
            log.warning("Telegram временно недоступен, повтор %d через %.0f с", attempt, delay)
            await self._sleep(delay)

    async def publish(self, post: Post, channel: Channel) -> PublishResult:
        chat_id = channel.external_id
        raw = post.text[: PLATFORM_HARD_LIMITS[Platform.TELEGRAM]]
        text, length, mode = format_text(raw)
        try:
            items = getattr(post, "delivery_media", None)
            if items:
                if any(not Path(item["path"]).is_file() for item in items):
                    raise PublishError("Вложение поста недоступно")
                if length > TELEGRAM_CAPTION_LIMIT:
                    raise PublishError("Подпись к медиа должна быть не длиннее 1024 символов")
                if len(items) == 1:
                    item = items[0]
                    send = getattr(
                        self.bot,
                        "send_"
                        + {
                            "video": "video",
                            "document": "document",
                        }.get(item["type"], "photo"),
                    )
                    msg = await self._call(
                        lambda: send(
                            chat_id,
                            FSInputFile(
                                item["path"],
                                filename=item["name"] if item["type"] == "document" else None,
                            ),
                            caption=text,
                            parse_mode=mode,
                        )
                    )
                else:
                    album = []
                    for index, item in enumerate(items):
                        kind = {
                            "video": InputMediaVideo,
                            "document": InputMediaDocument,
                        }.get(item["type"], InputMediaPhoto)
                        album.append(
                            kind(
                                media=FSInputFile(
                                    item["path"],
                                    filename=item["name"] if item["type"] == "document" else None,
                                ),
                                caption=text if index == 0 else None,
                                parse_mode=mode,
                            )
                        )
                    sent = await self._call(lambda: self.bot.send_media_group(chat_id, album))
                    msg = sent[0]
            elif post.image_path and Path(post.image_path).exists():
                photo = FSInputFile(post.image_path)
                if length <= TELEGRAM_CAPTION_LIMIT:
                    msg = await self._call(
                        lambda: self.bot.send_photo(chat_id, photo, caption=text, parse_mode=mode)
                    )
                else:
                    # Подпись к фото ограничена 1024 символами — шлём фото и текст отдельно.
                    # Каждый вызов повторяется сам по себе, фото не уйдёт дважды.
                    await self._call(lambda: self.bot.send_photo(chat_id, photo))
                    msg = await self._call(
                        lambda: self.bot.send_message(chat_id, text, parse_mode=mode)
                    )
            else:
                if post.image_path:
                    log.warning("Картинка поста #%s не найдена: %s", post.id, post.image_path)
                msg = await self._call(
                    lambda: self.bot.send_message(chat_id, text, parse_mode=mode)
                )
        except TelegramNetworkError as exc:
            raise PublishError(
                f"нет связи с Telegram ({exc}). Сообщение могло уйти — проверьте канал, "
                "прежде чем публиковать повторно"
            ) from exc
        except TelegramAPIError as exc:
            raise PublishError(f"Telegram: {exc}") from exc
        return PublishResult(external_id=str(msg.message_id), url=message_url(msg))

    async def check_channel(self, channel: Channel) -> ChannelCheck:
        try:
            chat = await self.bot.get_chat(channel.external_id)
            me = await self.bot.me()
            member = await self.bot.get_chat_member(channel.external_id, me.id)
        except TelegramAPIError as exc:
            return ChannelCheck(False, f"канал недоступен: {exc}")
        title = chat.title or channel.external_id
        if member.status == ChatMemberStatus.CREATOR:
            return ChannelCheck(True, f"«{title}», бот — владелец")
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            if getattr(member, "can_post_messages", True) is False:
                return ChannelCheck(False, f"«{title}»: бот админ, но без права публиковать")
            return ChannelCheck(True, f"«{title}», бот — администратор")
        return ChannelCheck(False, f"«{title}»: бот не администратор канала")


def message_url(msg: Message) -> str | None:
    """Ссылка на сообщение в канале (публичном или приватном)."""
    if msg.chat.username:
        return f"https://t.me/{msg.chat.username}/{msg.message_id}"
    chat_id = str(msg.chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{msg.message_id}"
    return None
