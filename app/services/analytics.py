"""Аналитика — ЗАГЛУШКА с интерфейсом (поток B).

Задумка:
- collect_metrics: периодически собирать просмотры/реакции/пересылки опубликованных
  постов (Telegram: статистика канала доступна ограниченно — проверить
  getMessageViews через MTProto или вести собственные метрики; VK: stats.getPostReach).
- хранить снимки метрик (новая модель PostMetric — согласовать с напарником);
- get_summary: сводка за период для агента и отчёта.

Уже сейчас доступны события в EventLog (создание, согласование, публикация, ошибки).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business


@dataclass
class AnalyticsSummary:
    period_days: int
    posts_published: int = 0
    total_views: int = 0
    top_posts: list[dict] = field(default_factory=list)


async def collect_metrics(session: AsyncSession, business: Business) -> int:
    """Собирает свежие метрики по опубликованным постам. Возвращает число обновлённых."""
    # TODO(поток B)
    raise NotImplementedError("Сбор метрик пока в разработке")


async def get_summary(session: AsyncSession, business: Business, days: int = 7) -> AnalyticsSummary:
    """Сводка за последние `days` дней."""
    # TODO(поток B)
    raise NotImplementedError("Аналитика пока в разработке — скоро покажу охваты и реакции!")
