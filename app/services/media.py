"""Фотобанк компании: свои фото клиента для постов.

Свои фото обычно лучше сгенерированных: настоящий зал, товар, команда. Клиент присылает
фото боту, оно сохраняется в фотобанк компании и может стать картинкой поста — нового или
уже готового черновика. Замена картинки — существенное изменение, поэтому пост снова
уходит на согласование (п. 3.5 ТЗ).
"""

from __future__ import annotations

import secrets
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import IMAGES_DIR
from app.db.models import Business, EventType, MediaFile, Post, PostStatus
from app.db.publications import PostAttachments
from app.db.repositories import EventLogRepo, MediaRepo
from app.images.base import ImageProvider
from app.llm.base import LLMProvider
from app.services import posts
from app.services.post_history import RevisionSource, save_revision
from app.services.posts import transition

UPLOADS_DIR = IMAGES_DIR / "uploads"
MAX_PHOTO_BYTES = 20 * 1024 * 1024


class MediaError(ValueError):
    """Ошибка с фото — текст показывается пользователю."""


async def save_photo(
    session: AsyncSession,
    business: Business,
    data: bytes,
    *,
    caption: str = "",
    unique_id: str | None = None,
    root: Path | None = None,
) -> tuple[MediaFile, bool]:
    """Сохраняет фото в фотобанк компании. Возвращает (фото, создано_ли_новое)."""
    if not data:
        raise MediaError("Не получилось скачать фото, пришлите его ещё раз.")
    if len(data) > MAX_PHOTO_BYTES:
        raise MediaError("Фото больше 20 МБ — пришлите поменьше.")
    repo = MediaRepo(session)
    if unique_id:
        existing = await repo.find_by_unique_id(business.id, unique_id)
        if existing is not None and Path(existing.path).exists():
            if caption and not existing.caption:
                existing.caption = caption[:1000]
            return existing, False

    folder = (root or UPLOADS_DIR) / str(business.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{unique_id or secrets.token_hex(8)}.jpg"
    path.write_bytes(data)
    media = await repo.add(
        MediaFile(
            business_id=business.id,
            path=str(path),
            caption=caption[:1000],
            telegram_unique_id=unique_id,
        )
    )
    await EventLogRepo(session).log(
        EventType.PHOTO_ADDED, caption[:200] or path.name, business_id=business.id
    )
    return media, True


async def get_photo(session: AsyncSession, business: Business, media_id: int) -> MediaFile:
    media = await MediaRepo(session).get_for_business(media_id, business.id)
    if media is None or not Path(media.path).exists():
        raise MediaError("Фото не найдено в фотобанке.")
    return media


async def create_post_with_photo(
    session: AsyncSession,
    llm: LLMProvider,
    images: ImageProvider,
    business: Business,
    media: MediaFile,
    topic: str,
    *,
    source: RevisionSource | None = None,
) -> Post:
    """Новый черновик: текст пишет AI, картинка — фото клиента."""
    topic = topic.strip() or media.caption.strip() or "новый пост с нашим фото"
    if media.description:
        topic += f" (на фото: {media.description[:300]})"
    post = await posts.create_draft(
        session,
        llm,
        images,
        business,
        topic,
        image_path=media.path,
        media_id=media.id,
        source=source or RevisionSource("photo", request_text=topic),
    )
    media.used_count += 1
    await session.flush()
    return post


async def replace_post_photo(
    session: AsyncSession, post: Post, media: MediaFile, *, author_id: int | None = None
) -> Post:
    """Ставит фото клиента в готовый черновик. Пост возвращается в draft — на согласование."""
    if post.business_id != media.business_id:
        raise MediaError("Фото и пост из разных компаний.")
    if post.status not in (PostStatus.DRAFT,):
        transition(post, PostStatus.DRAFT)  # бросит ошибку для published/rejected
    attachments = await session.get(PostAttachments, post.id)
    if attachments is not None:
        await session.delete(attachments)
        await session.flush()
    post.image_path = media.path
    post.scheduled_at = None
    post.error = None
    media.used_count += 1
    await save_revision(
        session,
        post,
        RevisionSource("photo", author_id=author_id),
        instruction="Замена изображения",
        image_change=True,
        image_source="upload",
        media_id=media.id,
    )
    await EventLogRepo(session).log(
        EventType.POST_REVISED,
        "замена картинки на фото клиента",
        business_id=post.business_id,
        post_id=post.id,
    )
    await session.flush()
    return post
