"""Посты: создание и правка черновиков, машина состояний.

Машина состояний (ЕДИНСТВЕННОЕ место, где описаны переходы):

    draft -> pending_approval -> approved -> (scheduled) -> published
                     \\-> rejected          \\-> failed

Правка возвращает пост в draft — после неё нужно новое согласование.
⚠️ Менять переходы — только по согласованию с напарником.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import render_prompt
from app.db.models import Business, EventType, Platform, Post, PostStatus
from app.db.repositories import ChannelRepo, EventLogRepo, PostRepo
from app.images.base import ImageProvider
from app.llm.base import LLMProvider
from app.services.business_profile import profile_to_text

log = logging.getLogger(__name__)

S = PostStatus
ALLOWED_TRANSITIONS: dict[PostStatus, frozenset[PostStatus]] = {
    S.DRAFT: frozenset({S.PENDING_APPROVAL, S.REJECTED}),
    S.PENDING_APPROVAL: frozenset({S.APPROVED, S.REJECTED, S.DRAFT}),
    S.APPROVED: frozenset({S.SCHEDULED, S.PUBLISHED, S.FAILED, S.DRAFT}),
    S.SCHEDULED: frozenset({S.PUBLISHED, S.FAILED, S.APPROVED, S.DRAFT}),
    S.FAILED: frozenset({S.APPROVED, S.SCHEDULED, S.DRAFT, S.REJECTED}),
    S.PUBLISHED: frozenset(),
    S.REJECTED: frozenset(),
}
# Публиковать можно только из этих статусов — проверяется в services/publishing.py
PUBLISHABLE_STATUSES = frozenset({S.APPROVED, S.SCHEDULED})

STATUS_TITLES: dict[PostStatus, str] = {
    S.DRAFT: "📝 черновик",
    S.PENDING_APPROVAL: "⏳ на согласовании",
    S.APPROVED: "👍 одобрен",
    S.SCHEDULED: "🕒 запланирован",
    S.PUBLISHED: "✅ опубликован",
    S.REJECTED: "❌ отклонён",
    S.FAILED: "⚠️ ошибка публикации",
}
PLATFORM_TITLES: dict[Platform, str] = {
    Platform.TELEGRAM: "Telegram-канал",
    Platform.VK: "ВКонтакте",
    Platform.MAX: "MAX",
}

# Жёсткие лимиты платформ (символов в одном сообщении)
PLATFORM_HARD_LIMITS: dict[Platform, int] = {
    Platform.TELEGRAM: 4096,
    Platform.VK: 15_000,
    Platform.MAX: 4000,
}
TELEGRAM_CAPTION_LIMIT = 1024


class InvalidTransitionError(ValueError):
    """Недопустимый переход статуса поста."""


class PublishNotAllowedError(PermissionError):
    """Попытка опубликовать пост, который не прошёл согласование."""


def can_transition(old: PostStatus, new: PostStatus) -> bool:
    return new in ALLOWED_TRANSITIONS[old]


def transition(post: Post, new: PostStatus) -> None:
    old = PostStatus(post.status)
    if not can_transition(old, new):
        raise InvalidTransitionError(
            f"Пост #{post.id}: переход {old.value} -> {new.value} запрещён"
        )
    post.status = new


def ensure_publishable(post: Post) -> None:
    """Гарантия «только после согласования». Вызывается перед любой публикацией."""
    if post.status not in PUBLISHABLE_STATUSES:
        raise PublishNotAllowedError(
            f"Пост #{post.id} в статусе {PostStatus(post.status).value} нельзя публиковать: "
            "нужно одобрение клиента"
        )


def target_length(platform: Platform, with_image: bool) -> int:
    """Сколько символов просить у LLM. С картинкой — уложиться в подпись к фото."""
    if with_image and platform == Platform.TELEGRAM:
        return TELEGRAM_CAPTION_LIMIT - 24
    return min(2500, PLATFORM_HARD_LIMITS[platform])


async def create_draft(
    session: AsyncSession,
    llm: LLMProvider,
    images: ImageProvider,
    business: Business,
    topic: str,
    platform: Platform = Platform.TELEGRAM,
    with_image: bool = False,
    plan_item_id: int | None = None,
    image_path: str | None = None,
) -> Post:
    """Пишет текст (и картинку) через LLM и сохраняет пост в статусе draft.

    `image_path` — готовое фото клиента из фотобанка: тогда картинка не генерируется.
    """
    with_image = with_image or image_path is not None
    limit = target_length(platform, with_image)
    system = render_prompt(
        "write_post",
        profile=profile_to_text(business),
        platform=PLATFORM_TITLES[platform],
        limit=limit,
    )
    user = (
        f"Тема: {topic}\n"
        f"Название бизнеса: {business.name}\n"
        f"Платформа: {platform.value}\n"
        f"Лимит символов: {limit}"
    )
    text = clean_post_text(await llm.complete(system, user, task="write_post"), platform)

    if image_path is None and with_image:
        # TODO(поток A): отдельный промпт для картинки (визуальное описание сцены)
        image_path = str(await images.generate(f"{topic} — {business.name}"))

    channel = await ChannelRepo(session).get_active(business.id, platform)
    post = Post(
        business_id=business.id,
        channel_id=channel.id if channel else None,
        platform=platform,
        topic=topic[:512],
        text=text,
        image_path=image_path,
        status=PostStatus.DRAFT,
        plan_item_id=plan_item_id,
    )
    repo = PostRepo(session)
    await repo.add(post)
    await repo.add_revision(post, text, instruction=None)
    await EventLogRepo(session).log(
        EventType.POST_CREATED, topic, business_id=business.id, post_id=post.id
    )
    return post


async def revise(
    session: AsyncSession, llm: LLMProvider, business: Business, post: Post, instruction: str
) -> Post:
    """Переписывает текст по правке клиента. Пост возвращается в draft."""
    if post.status != PostStatus.DRAFT:
        transition(post, PostStatus.DRAFT)  # бросит ошибку для published/rejected
    post.scheduled_at = None

    platform = Platform(post.platform)
    limit = target_length(platform, bool(post.image_path))
    system = render_prompt("revise_post", profile=profile_to_text(business), limit=limit)
    user = (
        f"Текущий текст поста:\n<<<\n{post.text}\n>>>\n"
        f"Правка клиента: {instruction}\n"
        f"Лимит символов: {limit}"
    )
    post.text = clean_post_text(await llm.complete(system, user, task="revise_post"), platform)
    post.error = None
    await PostRepo(session).add_revision(post, post.text, instruction=instruction)
    await EventLogRepo(session).log(
        EventType.POST_REVISED, instruction, business_id=business.id, post_id=post.id
    )
    return post


def clean_post_text(text: str, platform: Platform) -> str:
    """Убирает кавычки-обёртки и обрезает до жёсткого лимита платформы."""
    text = text.strip().strip('"«»').strip()
    limit = PLATFORM_HARD_LIMITS[platform]
    if len(text) > limit:
        cut = text[: limit - 1]
        cut = cut[: cut.rfind(" ")] if " " in cut[-200:] else cut
        text = cut.rstrip() + "…"
    return text
