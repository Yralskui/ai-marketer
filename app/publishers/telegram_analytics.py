"""Метрики канала из Bot API и счётчики постов из MTProto."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import MemorySession

from app.publishers.analytics import AnalyticsError, ChannelMetrics, PostMetrics


class TelegramAnalytics:
    def __init__(self, bot: Bot, *, api_id: int = 0, api_hash: str = "") -> None:
        self.bot = bot
        self.client = (
            TelegramClient(
                MemorySession(),
                api_id,
                api_hash,
                receive_updates=False,
                timeout=5,
                connection_retries=0,
                request_retries=0,
                flood_sleep_threshold=0,
            )
            if api_id and api_hash
            else None
        )
        self._connect_lock = asyncio.Lock()
        self._authorized = False

    async def channel_metrics(self, channel_id: str) -> ChannelMetrics:
        try:
            chat_id = channel_id if channel_id.startswith("@") else int(channel_id)
            return ChannelMetrics(await self.bot.get_chat_member_count(chat_id, request_timeout=5))
        except (TelegramAPIError, TimeoutError, ValueError) as exc:
            raise AnalyticsError("Telegram не вернул число подписчиков") from exc

    async def post_metrics(self, channel_id: str, post_ids: list[str]) -> dict[str, PostMetrics]:
        client = self.client
        ids = [
            int(value)
            for value in post_ids
            if len(value) <= 10
            and value.isascii()
            and value.isdecimal()
            and 0 < int(value) <= 2**31 - 1
        ]
        if client is None or not ids:
            return {}

        try:
            async with asyncio.timeout(10):
                async with self._connect_lock:
                    if not self._authorized or not client.is_connected():
                        self._authorized = False
                        await client.start(bot_token=self.bot.token)
                        self._authorized = True
                if channel_id.startswith("@"):
                    channel = utils.get_input_channel(await client.get_input_entity(channel_id))
                else:
                    peer_id, peer_type = utils.resolve_id(int(channel_id))
                    if peer_type is not types.PeerChannel:
                        raise ValueError("Ожидался ID Telegram-канала")
                    # Для бота Telegram допускает нулевой access hash, если он ещё не известен.
                    channel = types.InputChannel(peer_id, 0)

                result = {}
                for start in range(0, len(ids), 100):
                    response = await client(
                        functions.channels.GetMessagesRequest(
                            channel, [types.InputMessageID(id_) for id_ in ids[start : start + 100]]
                        )
                    )
                    for message in response.messages:
                        if isinstance(message, types.Message):
                            result[str(message.id)] = PostMetrics(message.views, message.forwards)
                        elif isinstance(message, types.MessageEmpty):
                            result[str(message.id)] = PostMetrics(deleted=True)
                return result
        except (errors.RPCError, OSError, TimeoutError, TypeError, ValueError) as exc:
            raise AnalyticsError("Telegram не вернул счётчики публикаций") from exc

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
