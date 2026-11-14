"""Краткая веб-справка и один разбор на опубликованный пост."""

import json
from datetime import timedelta
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.agent.prompts import render_prompt
from app.db.models import Post, PostStatus
from app.db.publications import PostCritique, PostSnapshot
from app.llm.base import LLMError
from app.services.business_profile import get_profile
from app.utils.time import to_local, utcnow


class Critique(BaseModel):
    facts: str = Field(min_length=1, max_length=600)
    hypothesis: str = Field(min_length=1, max_length=600)
    next: str = Field(min_length=1, max_length=600)


async def claim(session, model, keys):
    # Уникальный ключ и блокировка строки объединяют параллельные запросы.
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    await session.execute(insert(model).values(**keys).on_conflict_do_nothing())
    return await session.scalar(
        select(model).filter_by(**keys).with_for_update().execution_options(populate_existing=True)
    )


async def research(llm, tz, topic, question):
    if llm is None:
        return None
    today = to_local(utcnow(), tz).date()
    raw = await llm.complete(
        render_prompt("web_brief"),
        f"Дата: {today}\nТема: {topic[:350]}\nВопрос: {question[:400]}",
        task="web_brief",
    )
    try:
        data = json.loads(raw)
        summary = str(data["summary"]).strip()[:850]
        sources = []
        for url, title in data["sources"]:
            if len(url) <= 1500 and urlsplit(url).scheme in {"https", "http"}:
                sources.append({"url": url, "title": str(title)[:100]})
            if len(sources) == 3:
                break
    except (TypeError, ValueError, KeyError) as exc:
        raise LLMError("Поиск вернул некорректный ответ") from exc
    return {"summary": summary if sources else "", "sources": sources}


def brief_context(brief):
    if not brief or not brief["summary"]:
        return ""
    return json.dumps(
        {
            "summary": brief["summary"],
            "sources": [source["url"] for source in brief["sources"] if len(source["url"]) <= 320],
        },
        ensure_ascii=False,
    )


async def critique(session, llm, business, post, tz):
    if post.status != PostStatus.PUBLISHED:
        raise ValueError("Разбор доступен для опубликованных постов")
    cached = await session.get(PostCritique, post.id)
    if cached and cached.result is not None:
        return cached.result
    points = list(
        await session.scalars(
            select(PostSnapshot)
            .where(PostSnapshot.post_id == post.id, PostSnapshot.views.is_not(None))
            .order_by(PostSnapshot.hour.desc())
            .limit(168)
        )
    )
    if not points:
        raise ValueError("Для разбора дождитесь первых замеров просмотров")
    row = await claim(session, PostCritique, {"post_id": post.id})
    if row.result is not None:
        return row.result
    selected = points[:: max(1, len(points) // 12)][:13]
    if selected[-1] is not points[-1]:
        selected.append(points[-1])
    # Сравниваем с постами той же площадки по замерам в близком возрасте.
    target_age = points[0].hour - post.published_at if post.published_at else None
    comparable = []
    if target_age is not None:
        recent = list(
            await session.scalars(
                select(Post)
                .where(
                    Post.business_id == business.id,
                    Post.id != post.id,
                    Post.platform == post.platform,
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at.is_not(None),
                )
                .order_by(Post.published_at.desc())
                .limit(8)
            )
        )
        for other in recent:
            if other.channel_id != post.channel_id:
                continue
            target = other.published_at + target_age
            sample = await session.scalar(
                select(PostSnapshot)
                .where(
                    PostSnapshot.post_id == other.id,
                    PostSnapshot.views.is_not(None),
                    PostSnapshot.hour >= target - timedelta(hours=1),
                    PostSnapshot.hour <= target + timedelta(hours=1),
                )
                .order_by(PostSnapshot.hour.desc())
                .limit(1)
            )
            if sample:
                comparable.append(sample.views)
    payload = {
        "компания": business.name,
        "описание": get_profile(business).description[:600],
        "тема": post.topic,
        "текст": post.text[:4100],
        "публикация": to_local(post.published_at, tz).isoformat() if post.published_at else None,
        "замеры": [
            [to_local(p.hour, tz).isoformat(), p.views, p.forwards] for p in reversed(selected)
        ],
        "просмотры_сопоставимых_постов": comparable,
    }
    raw = await llm.complete(
        render_prompt("post_critique"),
        json.dumps(payload, ensure_ascii=False),
        json_mode=True,
        task="post_critique",
    )
    try:
        row.result = Critique.model_validate_json(raw).model_dump()
    except ValidationError as exc:
        raise LLMError("Не удалось прочитать разбор") from exc
    await session.flush()
    return row.result
