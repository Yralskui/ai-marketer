"""Публикация во ВКонтакте — ЗАГЛУШКА (поток B).

TODO(поток B):
- токен сообщества (VK_GROUP_TOKEN) и ID группы в настройках;
- wall.post(owner_id=-group_id, message=text, from_group=1);
- картинка: photos.getWallUploadServer -> загрузка -> photos.saveWallPhoto -> attachments;
- URL: https://vk.com/wall-{group_id}_{post_id};
- check_channel: groups.getById + проверка прав токена.
"""

from __future__ import annotations

from app.db.models import Channel, Platform, Post
from app.publishers.base import Publisher, PublishResult


class VKPublisher(Publisher):
    platform = Platform.VK

    async def publish(self, post: Post, channel: Channel) -> PublishResult:
        raise NotImplementedError("Публикация во ВКонтакте пока в разработке")
