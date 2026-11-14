"""Метрики сообщества ВКонтакте: число подписчиков и просмотры записей.

Подключается к почасовому сбору (services/social_metrics.py) так же, как Telegram: источник
отдаёт числа, а в базу и в графики Mini App они попадают общим кодом.
"""

from __future__ import annotations

from typing import Any

from app.publishers.analytics import AnalyticsError, ChannelMetrics, PostMetrics
from app.publishers.vk_api import VKClients, VKError

BATCH = 100  # wall.getById принимает до 100 записей за вызов


def _group_id(channel_id: str) -> int:
    try:
        return abs(int(channel_id))
    except ValueError as exc:
        raise AnalyticsError(f"Некорректный id сообщества ВКонтакте: {channel_id!r}") from exc


def _count(value: Any) -> int | None:
    count = value.get("count") if isinstance(value, dict) else None
    return count if isinstance(count, int) else None


class VKAnalytics:
    def __init__(self, clients: VKClients) -> None:
        self.clients = clients

    async def channel_metrics(self, channel_id: str) -> ChannelMetrics:
        group_id = _group_id(channel_id)
        try:
            response = await self.clients.get(group_id).call(
                "groups.getById",
                {"group_id": group_id, "fields": "members_count"},
                idempotent=True,
            )
        except VKError as exc:
            raise AnalyticsError(f"ВКонтакте не вернул число подписчиков: {exc}") from exc
        groups = response.get("groups") if isinstance(response, dict) else response
        members = groups[0].get("members_count") if groups else None
        return ChannelMetrics(members if isinstance(members, int) else None)

    async def post_metrics(self, channel_id: str, post_ids: list[str]) -> dict[str, PostMetrics]:
        group_id = _group_id(channel_id)
        if not self.clients.business_account:
            return {}  # просмотры недоступны без бизнес-аккаунта: не спрашиваем и не выдумываем
        ids = [value for value in post_ids if value.isascii() and value.isdecimal()]
        result: dict[str, PostMetrics] = {}
        for start in range(0, len(ids), BATCH):
            chunk = ids[start : start + BATCH]
            try:
                response = await self.clients.get(group_id).call(
                    "wall.getById",
                    {"posts": ",".join(f"-{group_id}_{post_id}" for post_id in chunk)},
                    idempotent=True,
                )
            except VKError as exc:
                raise AnalyticsError(f"ВКонтакте не вернул просмотры: {exc}") from exc
            items = response.get("items") if isinstance(response, dict) else response
            found = {str(item["id"]): item for item in items or [] if "id" in item}
            for post_id in chunk:
                item = found.get(post_id)
                if item is None:
                    # Вызов удался, а записи в ответе нет — её удалили со стены
                    result[post_id] = PostMetrics(deleted=True)
                else:
                    result[post_id] = PostMetrics(
                        views=_count(item.get("views")), forwards=_count(item.get("reposts"))
                    )
        return result

    async def close(self) -> None:
        await self.clients.close()
