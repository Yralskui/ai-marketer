"""Инструменты: посты. Публикации здесь НЕТ и быть не должно — только черновики."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.agent.registry import tool
from app.db.models import Platform, PostStatus
from app.db.repositories import PostRepo
from app.services import approval, posts
from app.services.post_history import RevisionSource
from app.utils.time import format_local


class CreatePostArgs(BaseModel):
    topic: str = Field(description="Тема поста, своими словами клиента", min_length=2)
    platform: Literal["telegram", "vk", "max"] = Field(
        "telegram", description="Площадка; сейчас публикация работает только в telegram"
    )
    with_image: bool = Field(False, description="Нужна ли картинка к посту")


@tool(
    "create_post_draft",
    "Подготовить черновик поста на заданную тему и отправить его клиенту на согласование "
    "(клиент увидит текст с кнопками «Опубликовать / Запланировать / Изменить / Отклонить»).",
)
async def create_post_draft(ctx: AgentContext, args: CreatePostArgs) -> dict:
    post = await posts.create_draft(
        ctx.session,
        ctx.llm,
        ctx.images,
        ctx.business,
        topic=args.topic,
        platform=Platform(args.platform),
        with_image=args.with_image,
        reference=ctx.reference,
        web_context=ctx.web_context,
        source=RevisionSource("chat", request_id=ctx.request_id, author_id=ctx.user_id),
        image_model=ctx.settings.image_model,
        progress=ctx.update_progress,
    )
    await approval.submit_for_approval(ctx.session, post, ctx.notifier, ctx.target_chat_id)
    ctx.created_post_ids.append(post.id)
    result = {
        "post_id": post.id,
        "status": post.status.value,
        "has_image": bool(post.image_path),
        "preview": post.text[:200],
    }
    if args.with_image and not post.image_path:
        result["warning"] = "Картинку не удалось сгенерировать. Черновик отправлен без неё."
    return result


class RevisePostArgs(BaseModel):
    post_id: int = Field(description="Номер поста")
    instruction: str = Field(description="Что изменить, словами клиента", min_length=2)
    research_question: str = Field(
        "",
        max_length=400,
        description="Точный вопрос для поиска, только если нужны актуальные внешние факты",
    )


@tool(
    "revise_post",
    "Переписать существующий пост по правке клиента и снова отправить на согласование.",
)
async def revise_post(ctx: AgentContext, args: RevisePostArgs) -> dict:
    post = await PostRepo(ctx.session).get_for_business(args.post_id, ctx.business_id)
    if post is None:
        return {"error": f"Пост #{args.post_id} не найден"}
    await posts.revise(
        ctx.session,
        ctx.llm,
        ctx.business,
        post,
        args.instruction,
        web_context=ctx.web_context,
        source=RevisionSource("chat", request_id=ctx.request_id, author_id=ctx.user_id),
    )
    await approval.submit_for_approval(ctx.session, post, ctx.notifier, ctx.target_chat_id)
    ctx.created_post_ids.append(post.id)
    return {"post_id": post.id, "status": post.status.value, "preview": post.text[:200]}


class ListPostsArgs(BaseModel):
    status: PostStatus | None = Field(None, description="Фильтр по статусу")
    limit: int = Field(10, ge=1, le=50)


@tool("list_posts", "Показать последние посты бизнеса с их статусами.")
async def list_posts(ctx: AgentContext, args: ListPostsArgs) -> dict:
    items = await PostRepo(ctx.session).list_for_business(ctx.business_id, args.status, args.limit)
    tz = ctx.settings.tz
    return {
        "posts": [
            {
                "id": p.id,
                "status": p.status.value,
                "topic": p.topic,
                "scheduled_at": format_local(p.scheduled_at, tz) if p.scheduled_at else None,
                "published_at": format_local(p.published_at, tz) if p.published_at else None,
                "url": p.external_url,
            }
            for p in items
        ]
    }
