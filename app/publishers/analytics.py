"""Интерфейс чтения метрик соцсети."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AnalyticsError(RuntimeError):
    """Соцсеть не смогла вернуть метрики."""


@dataclass(frozen=True)
class ChannelMetrics:
    subscribers: int | None = None


@dataclass(frozen=True)
class PostMetrics:
    views: int | None = None
    forwards: int | None = None
    deleted: bool = False


class AnalyticsSource(Protocol):
    async def channel_metrics(self, channel_id: str) -> ChannelMetrics: ...

    async def post_metrics(
        self, channel_id: str, post_ids: list[str]
    ) -> dict[str, PostMetrics]: ...

    async def close(self) -> None: ...
