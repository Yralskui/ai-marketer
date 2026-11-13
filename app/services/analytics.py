"""Аналитика публикаций: реакции, комментарии, согласования, расходы за период.

Источники данных:
- реакции на посты — Telegram присылает апдейт `message_reaction_count`, если бот
  администратор канала (`record_reactions`);
- комментарии — таблица `comments` (services/comments.py);
- согласования и публикации — статусы постов и `event_log`;
- расходы — `usage_records` (services/usage.py).

Просмотры постов Bot API не отдаёт (нужен MTProto-клиент от имени пользователя) —
это ограничение указано в отчёте.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta, tzinfo
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business, Comment, CommentStatus, EventLog, EventType
from app.db.repositories import ChannelRepo, PostMetricRepo, PostRepo
from app.services import usage
from app.utils.time import to_local, utcnow

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


@dataclass
class PostStat:
    id: int
    topic: str
    reactions: int
    comments: int
    has_image: bool
    url: str | None
    weekday: str
    hour: int


@dataclass
class AnalyticsSummary:
    period_days: int
    company: str = ""
    posts_published: int = 0
    drafts_created: int = 0
    approved: int = 0
    rejected: int = 0
    total_reactions: int = 0
    avg_reactions: float = 0.0
    comments_total: int = 0
    comments_replied: int = 0
    with_image_avg: float | None = None
    without_image_avg: float | None = None
    best_weekday: str | None = None
    costs_rub: float = 0.0
    top_posts: list[dict[str, Any]] = field(default_factory=list)
    posts: list[PostStat] = field(default_factory=list)
    limitations: str = "Просмотры через Bot API недоступны — считаем реакции и комментарии."


async def record_reactions(
    session: AsyncSession,
    channel_chat_id: int,
    channel_username: str | None,
    message_id: int,
    reactions: dict[str, int],
) -> bool:
    """Сохраняет реакции на пост канала. False — пост не наш."""
    for channel in await ChannelRepo(session).find_telegram(channel_chat_id, channel_username):
        post = await PostRepo(session).find_published(channel.id, str(message_id))
        if post is not None:
            await PostMetricRepo(session).set_reactions(post.id, post.business_id, reactions)
            return True
    return False


async def collect_metrics(session: AsyncSession, business: Business) -> int:
    """Реакции приходят от Telegram сами; возвращает число постов с метриками."""
    posts = await PostRepo(session).list_for_business(business.id, limit=1000)
    return len(await PostMetricRepo(session).for_posts([p.id for p in posts]))


async def _event_count(session: AsyncSession, business_id: int, type_: EventType, since) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                EventLog.business_id == business_id,
                EventLog.type == type_.value,
                EventLog.created_at >= since,
            )
        )
        or 0
    )


def _avg(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


async def get_summary(
    session: AsyncSession, business: Business, days: int = 7, tz: tzinfo | None = None
) -> AnalyticsSummary:
    """Сводка за последние `days` дней."""
    since = utcnow() - timedelta(days=days)
    posts = await PostRepo(session).list_published_since(business.id, since)
    metrics = await PostMetricRepo(session).for_posts([p.id for p in posts])

    comment_rows = (
        await session.execute(
            select(Comment.post_id, Comment.status).where(
                Comment.business_id == business.id, Comment.created_at >= since
            )
        )
    ).all()
    comments_per_post = Counter(pid for pid, _ in comment_rows if pid is not None)

    stats: list[PostStat] = []
    for post in posts:
        local = to_local(post.published_at, tz) if tz and post.published_at else post.published_at
        stats.append(
            PostStat(
                id=post.id,
                topic=(post.topic or post.text[:60]).strip(),
                reactions=metrics[post.id].reactions_total if post.id in metrics else 0,
                comments=comments_per_post.get(post.id, 0),
                has_image=bool(post.image_path),
                url=post.external_url,
                weekday=WEEKDAYS[local.weekday()] if local else "",
                hour=local.hour if local else 0,
            )
        )

    summary = AnalyticsSummary(period_days=days, company=business.name, posts=stats)
    summary.posts_published = len(stats)
    summary.total_reactions = sum(s.reactions for s in stats)
    summary.avg_reactions = round(summary.total_reactions / len(stats), 1) if stats else 0.0
    summary.comments_total = len(comment_rows)
    summary.comments_replied = sum(1 for _, st in comment_rows if st == CommentStatus.SENT)
    summary.with_image_avg = _avg([s.reactions for s in stats if s.has_image])
    summary.without_image_avg = _avg([s.reactions for s in stats if not s.has_image])
    by_day: dict[str, list[int]] = {}
    for s in stats:
        by_day.setdefault(s.weekday, []).append(s.reactions + s.comments)
    if by_day and summary.total_reactions + summary.comments_total > 0:
        summary.best_weekday = max(by_day, key=lambda d: sum(by_day[d]) / len(by_day[d]))
    summary.top_posts = [
        {"id": s.id, "topic": s.topic, "reactions": s.reactions, "comments": s.comments}
        for s in sorted(stats, key=lambda s: (s.reactions, s.comments), reverse=True)[:3]
    ]
    summary.drafts_created = await _event_count(session, business.id, EventType.POST_CREATED, since)
    summary.approved = await _event_count(session, business.id, EventType.POST_APPROVED, since)
    summary.rejected = await _event_count(session, business.id, EventType.POST_REJECTED, since)
    summary.costs_rub = round((await usage.usage_summary(session, business.id, since)).cost_rub, 2)
    return summary


def summary_for_llm(summary: AnalyticsSummary) -> dict[str, Any]:
    """Компактные цифры для промпта отчёта."""
    data = {
        k: v
        for k, v in summary.__dict__.items()
        if k not in ("posts", "limitations") and v not in (None, [], "")
    }
    data["posts"] = [
        {
            "id": s.id,
            "topic": s.topic,
            "reactions": s.reactions,
            "comments": s.comments,
            "image": s.has_image,
            "weekday": s.weekday,
            "hour": s.hour,
        }
        for s in summary.posts
    ]
    return data
