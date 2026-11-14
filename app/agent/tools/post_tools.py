"""Инструменты: посты. Публикации здесь НЕТ и быть не должно — только черновики."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.agent.registry import tool
from app.db.models import Platform, PostStatus
from app.db.repositories import ChannelRepo, PostRepo
from app.services import approval, posts
from app.services.post_history import RevisionSource
from app.utils.time import format_local


class CreatePostArgs(BaseModel):
    topic: str = Field(description="Тема поста, своими словами клиента", min_length=2)
    platforms: list[Literal["telegram", "vk"]] = Field(
        default_factory=list,
        description=(
            "Площадки, которые назвал клиент: telegram и/или vk («и туда и туда», «везде» — "
            "обе). Если клиент площадку не назвал — пустой список, не угадывай"
        ),
    )
    with_image: bool = Field(False, description="Нужна ли картинка к посту")


@tool(
    "create_post_draft",
    "Подготовить черновик поста на заданную тему и отправить его клиенту на согласование "
    "(клиент увидит текст с кнопками «Опубликовать / Запланировать / Изменить / Отклонить»).",
)
async def create_post_draft(ctx: AgentContext, args: CreatePostArgs) -> dict:
    targets, question = await choose_platforms(ctx, args.platforms)
    if question:
        # Черновик не создаём: сначала узнаём площадку. Вопрос агент передаёт клиенту как есть.
        return {"needs_platform_choice": True, "message": question}
    post = await posts.create_draft(
        ctx.session,
        ctx.llm,
        ctx.images,
        ctx.business,
        topic=args.topic,
        platform=targets[0],
        with_image=args.with_image,
        reference=ctx.reference,
        source=RevisionSource("chat", request_id=ctx.request_id, author_id=ctx.user_id),
        image_model=ctx.settings.image_model,
    )
    created = [post]
    for platform in targets[1:]:
        # Для второй соцсети — копия того же поста (текст по лимиту площадки, та же картинка)
        created.append(await posts.clone_for_platform(ctx.session, post, platform))
    for item in created:
        await approval.submit_for_approval(ctx.session, item, ctx.notifier, ctx.target_chat_id)
        ctx.created_post_ids.append(item.id)
    result: dict = {
        "post_id": post.id,
        "status": post.status.value,
        "has_image": bool(post.image_path),
        "preview": post.text[:200],
    }
    if len(created) > 1:
        result["posts"] = [
            {"post_id": item.id, "platform": Platform(item.platform).value} for item in created
        ]
    if args.with_image and not post.image_path:
        result["warning"] = "Картинку не удалось сгенерировать. Черновик отправлен без неё."
    return result


async def choose_platforms(
    ctx: AgentContext, requested: list[str]
) -> tuple[list[Platform], str | None]:
    """Куда готовить пост: площадки из запроса, единственная подключённая или вопрос клиенту.

    Подключены обе соцсети, а клиент не сказал куда — спрашиваем. Подключена одна — вопроса
    нет. Не подключено ничего — готовим для Telegram: пост можно написать заранее, а при
    публикации человек увидит понятную ошибку и сможет подключить канал.
    """
    asked = list(dict.fromkeys(Platform(name) for name in requested))
    if asked:
        return asked, None
    channels = ChannelRepo(ctx.session)
    connected = [
        platform
        for platform in (Platform.TELEGRAM, Platform.VK)
        if await channels.get_active(ctx.business_id, platform) is not None
    ]
    if len(connected) == 1:
        return connected, None
    if len(connected) == 2:
        return [], (
            "Подключены Telegram и ВКонтакте. Куда готовить пост: в Telegram, во ВКонтакте "
            "или в обе соцсети?"
        )
    return [Platform.TELEGRAM], None


class RevisePostArgs(BaseModel):
    post_id: int = Field(description="Номер поста")
    instruction: str = Field(description="Что изменить, словами клиента", min_length=2)


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
