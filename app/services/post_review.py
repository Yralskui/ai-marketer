"""Общий разбор поста: один и тот же пост, выложенный в Telegram и ВКонтакте, разбирается вместе.

Кнопка «Разбор» есть у поста любой соцсети. Если тот же пост вышел и туда и туда (копия через
`posts.clone_for_platform`), разбор строится по обеим публикациям сразу, с пометкой, где какая
цифра, — поэтому один пост нельзя разобрать двумя разными запросами с разных соцсетей. Разбор
считается по правилам, без вызова модели: так он одинаков при любом входе в него и бесплатен.

Связь «тот же пост» определяется без новых колонок: та же компания, та же тема и то же начало
текста. Копия для второй соцсети создаётся именно так (текст урезается только по лимиту).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Comment, Platform, Post, PostStatus
from app.db.repositories import PostMetricRepo
from app.publishers.analytics import PostMetrics
from app.services import social_metrics
from app.services.posts import PLATFORM_TITLES

_PREFIX = 80


def _key(post: Post) -> tuple[str, str]:
    return (post.topic or "").strip().lower(), post.text.strip()[:_PREFIX].lower()


async def post_group(session: AsyncSession, post: Post) -> list[Post]:
    """Все публикации этого поста: сам пост и его копии для других соцсетей (по одной на сеть)."""
    others = (
        await session.scalars(
            select(Post).where(
                Post.business_id == post.business_id,
                Post.platform != post.platform,
                Post.status == PostStatus.PUBLISHED,
            )
        )
    ).all()
    key = _key(post)
    group = {post.platform: post}
    for other in sorted(others, key=lambda item: item.id):
        if _key(other) == key and other.platform not in group:
            group[other.platform] = other
    return sorted(group.values(), key=lambda item: item.id)


def _rule_based_notes(rows: list[dict[str, Any]]) -> list[str]:
    notes = []
    with_views = [row for row in rows if row["views"]]
    if len(with_views) == 2:
        best = max(with_views, key=lambda row: row["views"])
        other = next(row for row in with_views if row is not best)
        notes.append(
            f"По просмотрам лучше сработал {best['platform_title']}: {best['views']} против "
            f"{other['views']} ({other['platform_title']})."
        )
    for row in rows:
        if row["subscribers"] and row["views"] is not None:
            share = round(100 * row["views"] / row["subscribers"])
            notes.append(
                f"{row['platform_title']}: пост увидели около {share}% подписчиков "
                f"({row['views']} из {row['subscribers']})."
            )
        if row["views"] is None:
            notes.append(f"{row['platform_title']}: счётчик просмотров пока недоступен.")
    commented = [row for row in rows if row["comments"]]
    if commented:
        notes.append(
            "Обсуждают: "
            + ", ".join(f"{row['platform_title']} — {row['comments']}" for row in commented)
            + "."
        )
    if not notes:
        notes.append("Данных пока мало: метрики собираются раз в час.")
    return notes


async def review(session: AsyncSession, post: Post) -> dict[str, Any]:
    """Разбор поста вместе с его копиями. Результат одинаков, с какой из них его ни запросили."""
    group = await post_group(session, post)
    ids = [item.id for item in group]
    subscribers, counters = await social_metrics.latest_metrics(session, post.business_id, ids)
    reactions = await PostMetricRepo(session).for_posts(ids)
    comments: dict[int, int] = {}
    for post_id in (
        await session.scalars(select(Comment.post_id).where(Comment.post_id.in_(ids)))
    ).all():
        comments[post_id] = comments.get(post_id, 0) + 1
    rows = []
    for item in group:
        platform = Platform(item.platform)
        social = counters.get(item.id, PostMetrics())
        metric = reactions.get(item.id)
        rows.append(
            {
                "post_id": item.id,
                "platform": platform.value,
                "platform_title": PLATFORM_TITLES.get(platform, platform.value),
                "published": item.status == PostStatus.PUBLISHED,
                "url": item.external_url,
                "views": social.views,
                "forwards": social.forwards,
                "reactions": metric.reactions_total if metric else 0,
                "comments": comments.get(item.id, 0),
                "subscribers": subscribers.get(platform.value),
            }
        )
    published = [row for row in rows if row["published"]]
    return {
        "post_ids": ids,
        "topic": group[0].topic or group[0].text[:60],
        "text": group[0].text,
        "platforms": rows,
        "notes": _rule_based_notes(published),
    }
