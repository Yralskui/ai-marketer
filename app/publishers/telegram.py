"""Публикация в Telegram-канал (бот должен быть админом канала)."""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, Message

from app.db.models import Channel, Platform, Post
from app.publishers.base import ChannelCheck, PublishError, Publisher, PublishResult
from app.services.posts import PLATFORM_HARD_LIMITS, TELEGRAM_CAPTION_LIMIT

log = logging.getLogger(__name__)


class TelegramPublisher(Publisher):
    platform = Platform.TELEGRAM

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def publish(self, post: Post, channel: Channel) -> PublishResult:
        chat_id = channel.external_id
        text = post.text[: PLATFORM_HARD_LIMITS[Platform.TELEGRAM]]
        try:
            if post.image_path and Path(post.image_path).exists():
                photo = FSInputFile(post.image_path)
                if len(text) <= TELEGRAM_CAPTION_LIMIT:
                    msg = await self.bot.send_photo(chat_id, photo, caption=text, parse_mode=None)
                else:
                    # Подпись к фото ограничена 1024 символами — шлём фото и текст отдельно
                    await self.bot.send_photo(chat_id, photo)
                    msg = await self.bot.send_message(chat_id, text, parse_mode=None)
            else:
                if post.image_path:
                    log.warning("Картинка поста #%s не найдена: %s", post.id, post.image_path)
                msg = await self.bot.send_message(chat_id, text, parse_mode=None)
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
