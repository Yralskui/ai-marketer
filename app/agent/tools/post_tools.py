"""Инструменты: посты. Публикации здесь НЕТ и быть не должно — только черновики."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.agent.registry import tool
from app.db.models import Platform, PostStatus
from app.db.repositories import ChannelRepo, PostRepo
from app.images import ImageGenerationError
from app.publishers.telegram_text import plain_text
from app.services import approval, content_gallery, posts
from app.services.image_references import (
    MAX_REFERENCES,
    ImageReference,
    numbered_images,
    prepare_references,
)
from app.services.post_history import RevisionSource
from app.services.post_images import layout_requirement
from app.utils.time import format_local


class CreatePostArgs(BaseModel):
    topic: str = Field(description="Тема поста, своими словами клиента", min_length=2)
    platform: Literal["telegram", "vk", "both", "max"] | None = Field(
        None, description="Площадка, явно указанная пользователем: Telegram, VK или обе"
    )
    with_image: bool = Field(False, description="Нужна ли картинка к посту")
    style: posts.PostStyle = Field(posts.PostStyle.NEUTRAL, description="Стиль поста")
    image_direction: str = Field("", max_length=500, description="Конкретный замысел иллюстрации")
    content_id: str | None = Field(
        None,
        pattern=r"^[gm]\d+$",
        description="ID картинки из раздела «Контент» (например g12 или m5), если её просят использовать",
    )
    existing_image_number: int | None = Field(
        None, gt=0, description="Номер готового изображения, выбранного пользователем"
    )
    references: list[ImageReference] = Field(default_factory=list, max_length=MAX_REFERENCES)
    use_logo: bool | None = Field(None, description="Явно просят добавить логотип или убрать его")
    branded_layout: bool = Field(
        False, description="Нужна готовая карточка с вёрсткой и заголовком"
    )
    logo_number: int | None = Field(
        None, gt=0, description="Номер картинки, которую клиент назвал оригиналом логотипа"
    )


@tool(
    "create_post_draft",
    "Подготовить черновик поста на заданную тему и отправить его клиенту на согласование "
    "(клиент увидит текст с кнопками «Опубликовать / Запланировать / Изменить / Отклонить»).",
)
async def create_post_draft(ctx: AgentContext, args: CreatePostArgs) -> dict:
    if args.platform is None:
        args.platform = await sole_or_default_platform(ctx)
        if args.platform is None:
            return {"error": "Куда готовить пост: в Telegram, во ВКонтакте или в обе соцсети?"}
    targets = (
        [Platform.TELEGRAM, Platform.VK]
        if args.platform == "both"
        else [Platform(args.platform or "telegram")]
    )
    content = None
    image_path = None
    if args.content_id or args.existing_image_number:
        if targets == [Platform.VK] and not ctx.settings.vk_is_business_account:
            return {"error": "Во ВКонтакте картинки пока недоступны: нужен бизнес-аккаунт."}
        try:
            if number := args.existing_image_number:
                documents, gallery = await numbered_images(
                    ctx.session, ctx.business_id, ctx.storage
                )
                content = gallery.get(number)
                if content is None:
                    if number not in documents:
                        return {"error": f"Картинка №{number} недоступна в этом профиле."}
                    async with prepare_references(
                        ctx.session, ctx.business_id, ctx.storage, [ImageReference(number=number)]
                    ) as prepared:
                        image_path = await asyncio.to_thread(
                            content_gallery.copy_path_for_post, prepared[0][1], ctx.business_id
                        )
            else:
                content = await content_gallery.get_content(
                    ctx.session, ctx.business_id, args.content_id
                )
                if content is None:
                    return {"error": "Картинка не найдена в разделе «Контент»."}
            if content:
                image_path = await asyncio.to_thread(
                    content_gallery.copy_for_post, content, ctx.business_id
                )
        except (ImageGenerationError, OSError) as exc:
            return {
                "error": str(exc)
                if isinstance(exc, ImageGenerationError)
                else "Картинку не удалось открыть."
            }
    with_image = (
        args.with_image
        and not image_path
        and (targets[0] != Platform.VK or ctx.settings.vk_is_business_account)
    )
    if with_image and args.branded_layout:
        missing = await layout_requirement(
            ctx.session,
            ctx.business,
            storage=ctx.storage,
            logo_number=args.logo_number,
            use_logo=args.use_logo,
        )
        if missing:
            return {"error": missing}
    post = await posts.create_draft(
        ctx.session,
        ctx.llm,
        ctx.images,
        ctx.business,
        topic=args.topic,
        platform=targets[0],
        with_image=with_image,
        reference=ctx.reference,
        web_context=ctx.web_context,
        source=RevisionSource("chat", request_id=ctx.request_id, author_id=ctx.user_id),
        image_model=ctx.settings.image_model,
        progress=ctx.update_progress,
        style=args.style,
        image_direction=args.image_direction,
        visual_references=args.references,
        storage=ctx.storage,
        use_logo=args.use_logo,
        branded_layout=args.branded_layout,
        logo_number=args.logo_number,
        reserve_caption=bool(image_path),
        image_path=image_path,
        media_id=content.media_id if content else None,
    )
    created = [post]
    if len(targets) > 1:
        created.append(
            await posts.clone_for_platform(ctx.session, post, targets[1], author_id=ctx.user_id)
        )
    for item in created:
        await approval.submit_for_approval(ctx.session, item, ctx.notifier, ctx.target_chat_id)
        ctx.created_post_ids.append(item.id)
    result = {
        "post_id": post.id,
        "status": post.status.value,
        "has_image": bool(post.image_path),
        "preview": plain_text(post.text)[:200],
    }
    if args.with_image and not with_image and not image_path:
        result["warning"] = "С текущим ключом ВКонтакте пост будет текстовым."
    elif with_image and not post.image_path:
        result["warning"] = (
            getattr(post, "image_warning", None)
            or "Картинку не удалось сгенерировать. Черновик отправлен без неё."
        )
    if len(created) > 1:
        result["post_ids"] = [item.id for item in created]
    return result


async def sole_or_default_platform(ctx: AgentContext) -> str | None:
    channels = ChannelRepo(ctx.session)
    connected = [
        platform.value
        for platform in (Platform.TELEGRAM, Platform.VK)
        if await channels.get_active(ctx.business_id, platform) is not None
    ]
    return None if len(connected) > 1 else (connected[0] if connected else "telegram")


class RevisePostArgs(BaseModel):
    post_id: int | None = Field(None, gt=0, description="Номер поста, если его можно определить")
    instruction: str = Field(
        "", description="Что изменить в тексте, если клиент просил правку текста"
    )
    clarification: str = Field(
        "", max_length=350, description="Какой номер картинки или поста ещё нужен"
    )
    style: posts.PostStyle | None = Field(None, description="Новый стиль при явной просьбе")
    content_id: str | None = Field(
        None,
        pattern=r"^[gm]\d+$",
        description="ID картинки из раздела «Контент» (например g12 или m5), если её просят вложить",
    )
    attachment_only: bool = Field(False, description="Клиент просит только поставить эту картинку")
    image_direction: str = Field(
        "", max_length=500, description="Что сгенерировать или изменить на иллюстрации"
    )
    references: list[ImageReference] = Field(default_factory=list, max_length=MAX_REFERENCES)
    use_logo: bool | None = Field(None, description="Явно просят добавить логотип или убрать его")
    branded_layout: bool = Field(
        False, description="Собрать фирменную карточку с заголовком и фото"
    )
    logo_number: int | None = Field(
        None, gt=0, description="Номер картинки, которую клиент назвал оригиналом логотипа"
    )
    start_fresh: bool = Field(
        False, description="Создать новую композицию, не используя текущий кадр"
    )
    edit_current_image: bool = Field(
        False, description="Пользователь просит изменить именно текущее изображение поста"
    )
    keep_text: bool = Field(False, description="Пользователь не просил менять текст")
    copy_as_draft: bool = Field(
        False, description="Подготовить отдельную копию существующего поста для согласования"
    )

    @property
    def changes_text(self) -> bool:
        return self.style is not None or (
            not self.attachment_only and not self.keep_text and bool(self.instruction.strip())
        )

    @property
    def has_changes(self) -> bool:
        return self.changes_text or bool(self.image_direction or self.content_id)


@tool(
    "revise_post",
    "Переписать существующий пост по правке клиента и снова отправить на согласование.",
)
async def revise_post(
    ctx: AgentContext, args: RevisePostArgs, *, copy_source: bool = False
) -> dict:
    if args.post_id is None:
        return {"error": "Укажите номер поста."}
    post = await PostRepo(ctx.session).get_for_business(args.post_id, ctx.business_id)
    if post is None:
        return {"error": f"Пост #{args.post_id} не найден"}
    if args.clarification:
        return {"error": args.clarification}
    if not copy_source and not args.has_changes:
        return {"error": "Уточните, что изменить в посте."}
    content = None
    if args.content_id:
        content = await content_gallery.get_content(ctx.session, ctx.business_id, args.content_id)
        if content is None:
            return {"error": "Картинка не найдена в разделе «Контент»."}
    if (
        args.image_direction
        and post.platform == Platform.VK
        and not ctx.settings.vk_is_business_account
    ):
        return {"error": "В текущем режиме ВКонтакте доступны текстовые посты."}
    source = RevisionSource("chat", request_id=ctx.request_id, author_id=ctx.user_id)
    try:
        if copy_source:
            async with ctx.session.begin_nested():
                if post.status == PostStatus.REJECTED:
                    raise ValueError("Исходный пост недоступен.")
                post = await posts.copy_post_as_draft(
                    ctx.session, post, author_id=ctx.user_id, request_id=ctx.request_id
                )
                await apply_revision(ctx, post, args, source, content)
        else:
            await apply_revision(ctx, post, args, source, content)
    except (ImageGenerationError, ValueError) as exc:
        return {"error": str(exc)}
    await approval.submit_for_approval(ctx.session, post, ctx.notifier, ctx.target_chat_id)
    ctx.created_post_ids.append(post.id)
    return {"post_id": post.id, "status": post.status.value, "preview": plain_text(post.text)[:200]}


async def apply_revision(ctx, post, args, source, content):
    if args.image_direction:
        ctx.update_progress("image")
        await posts.revise_image(
            ctx.session,
            ctx.llm,
            ctx.images,
            ctx.business,
            post,
            direction=args.image_direction,
            source=source,
            image_model=ctx.settings.image_model,
            reference=ctx.reference,
            visual_references=args.references,
            storage=ctx.storage,
            use_logo=args.use_logo,
            start_fresh=args.start_fresh and not args.edit_current_image,
            edit_current_image=args.edit_current_image,
            branded_layout=args.branded_layout and not args.edit_current_image,
            logo_number=args.logo_number,
        )
    if args.changes_text:
        await posts.revise(
            ctx.session,
            ctx.llm,
            ctx.business,
            post,
            args.instruction.strip() or "Перепиши пост в выбранном стиле. Сохрани факты.",
            web_context=ctx.web_context,
            style=args.style,
            reserve_caption=content is not None,
            source=source,
        )
    if content:
        await content_gallery.replace_post_image(ctx.session, post, content, author_id=ctx.user_id)


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
