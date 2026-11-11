"""Публикация в MAX — ЗАГЛУШКА (поток B).

TODO(поток B):
- изучить Bot API мессенджера MAX (dev.max.ru), токен бота в настройках;
- отправка сообщения в канал от имени бота, загрузка изображений;
- построение ссылки на сообщение;
- check_channel: проверка, что бот — админ канала.
"""

from __future__ import annotations

from app.db.models import Channel, Platform, Post
from app.publishers.base import Publisher, PublishResult


class MaxPublisher(Publisher):
    platform = Platform.MAX

    async def publish(self, post: Post, channel: Channel) -> PublishResult:
        raise NotImplementedError("Публикация в MAX пока в разработке")
