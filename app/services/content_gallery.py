"""Раздел «Контент»: картинки компании — сгенерированные ассистентом и добавленные людьми.

Это единственное, что агент может открыть и вложить в пост. Файлов профиля (PDF, документы,
брендбуки) агент не видит: из них он получает только текстовые выжимки через поиск по
материалам. Источники картинок:

* `g<N>` — сгенерированное изображение (версия поста N, см. generated_gallery);
* `m<N>` — картинка из фотобанка компании (`MediaFile`): её добавил человек в Mini App или
  прислал боту.

Удаление сгенерированной картинки скрывает её из раздела (файл остаётся: он может быть в посте),
удаление добавленной убирает запись и файл, если на него не ссылается ни один пост.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import IMAGES_DIR
from app.db.knowledge import HiddenGeneratedImage
from app.db.models import Business, EventType, MediaFile, Post, PostRevision, PostStatus
from app.db.publications import PostAttachments
from app.db.repositories import EventLogRepo, MediaRepo
from app.ingestion.profile_files import (
    IMAGE_EXTENSIONS,
    ProfileFileFormatError,
    fit_image_for_model,
)
from app.services import generated_gallery
from app.services.media import UPLOADS_DIR
from app.services.post_history import RevisionSource, save_revision
from app.services.posts import transition

LIMIT = generated_gallery.LIMIT  # общий размер раздела на компанию
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
USED_DIR = IMAGES_DIR / "content-used"
_ID = re.compile(r"^([gm])(\d+)$")


class ContentError(ValueError):
    """Ошибка раздела «Контент» — текст можно показывать пользователю."""


@dataclass(frozen=True)
class ContentImage:
    id: str
    kind: Literal["generated", "uploaded"]
    path: Path
    size: int
    label: str
    media_id: int | None = None


def parse_id(value: str | None) -> tuple[str, int] | None:
    match = _ID.match((value or "").strip().lower())
    return (match.group(1), int(match.group(2))) if match else None


async def _generated(session: AsyncSession, business_id: int) -> list[ContentImage]:
    entries = await generated_gallery.entries(session, business_id)
    if not entries:
        return []
    topics = dict(
        (
            await session.execute(
                select(PostRevision.id, Post.topic)
                .join(Post, Post.id == PostRevision.post_id)
                .where(PostRevision.id.in_([entry[0] for entry in entries]))
            )
        ).all()
    )
    return [
        ContentImage(
            f"g{revision_id}",
            "generated",
            path,
            size,
            (topics.get(revision_id) or "").strip() or f"Изображение №{revision_id}",
        )
        for revision_id, path, size in entries
    ]


async def _uploaded(session: AsyncSession, business_id: int) -> list[ContentImage]:
    result = []
    for media in reversed(await MediaRepo(session).list_for_business(business_id, limit=500)):
        path = Path(media.path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        label = (media.caption or media.description or "").strip() or f"Фото №{media.id}"
        result.append(ContentImage(f"m{media.id}", "uploaded", path, size, label[:120], media.id))
    return result


async def list_content(session: AsyncSession, business_id: int) -> list[ContentImage]:
    """Все картинки раздела: сначала добавленные людьми, затем сгенерированные."""
    return [*await _uploaded(session, business_id), *await _generated(session, business_id)]


async def get_content(
    session: AsyncSession, business_id: int, content_id: str | None
) -> ContentImage | None:
    parsed = parse_id(content_id)
    if parsed is None:
        return None
    wanted = f"{parsed[0]}{parsed[1]}"
    return next(
        (item for item in await list_content(session, business_id) if item.id == wanted), None
    )


async def available_bytes(session: AsyncSession, business_id: int) -> int:
    return LIMIT - sum(item.size for item in await list_content(session, business_id))


def _prepare_upload(filename: str, data: bytes) -> bytes:
    suffix = Path(filename).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        raise ContentError("В «Контент» добавляются только изображения.")
    folder = UPLOADS_DIR / ".tmp"
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / f"{secrets.token_hex(8)}{suffix}"
    try:
        temp.write_bytes(data)
        return fit_image_for_model(temp, suffix, quality=88)
    except ProfileFileFormatError as exc:
        raise ContentError(str(exc)) from exc
    except Exception as exc:  # битый файл под видом картинки
        raise ContentError("Не удалось прочитать изображение.") from exc
    finally:
        temp.unlink(missing_ok=True)


async def add_image(
    session: AsyncSession, business: Business, filename: str, data: bytes
) -> ContentImage:
    """Добавляет картинку: вписывается в Full HD без обрезки, сохраняется как JPEG."""
    if not 0 < len(data) <= MAX_UPLOAD_BYTES:
        raise ContentError("Изображение должно быть от 1 байта до 20 МБ.")
    jpeg = await asyncio.to_thread(_prepare_upload, filename, data)
    if len(jpeg) > await available_bytes(session, business.id):
        raise ContentError("В разделе «Контент» не осталось места: лимит 100 МБ.")
    folder = UPLOADS_DIR / str(business.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{secrets.token_hex(8)}.jpg"
    path.write_bytes(jpeg)
    caption = Path(filename).stem[:200]
    media = await MediaRepo(session).add(
        MediaFile(business_id=business.id, path=str(path), caption=caption)
    )
    await EventLogRepo(session).log(
        EventType.PHOTO_ADDED, caption or path.name, business_id=business.id
    )
    return ContentImage(
        f"m{media.id}", "uploaded", path, len(jpeg), caption or f"Фото №{media.id}", media.id
    )


async def delete_content(session: AsyncSession, business_id: int, content_id: str) -> bool:
    item = await get_content(session, business_id, content_id)
    if item is None:
        return False
    if item.kind == "generated":
        session.add(HiddenGeneratedImage(revision_id=int(item.id[1:])))
        return True
    media = await session.get(MediaFile, item.media_id)
    if media is None or media.business_id != business_id:
        return False
    await session.delete(media)
    await session.flush()
    in_use = await session.scalar(select(Post.id).where(Post.image_path == str(item.path)).limit(1))
    if in_use is None:
        item.path.unlink(missing_ok=True)
    return True


def copy_for_post(item: ContentImage, business_id: int) -> str:
    """Копия файла для поста: удаление картинки из раздела не ломает уже готовые посты."""
    folder = USED_DIR / str(business_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{secrets.token_hex(8)}{item.path.suffix.lower() or '.jpg'}"
    shutil.copyfile(item.path, target)
    return str(target)


async def replace_post_image(
    session: AsyncSession, post: Post, item: ContentImage, *, author_id: int | None = None
) -> Post:
    """Ставит картинку из раздела в готовый черновик; пост возвращается на согласование."""
    if post.status != PostStatus.DRAFT:
        transition(post, PostStatus.DRAFT)  # для опубликованных и отклонённых бросит ошибку
    attachments = await session.get(PostAttachments, post.id)
    if attachments is not None:
        await session.delete(attachments)
        await session.flush()
    post.image_path = await asyncio.to_thread(copy_for_post, item, post.business_id)
    post.scheduled_at = None
    post.error = None
    await save_revision(
        session,
        post,
        RevisionSource("photo", author_id=author_id),
        instruction="Замена изображения из раздела «Контент»",
        image_change=True,
        image_source="upload",
        media_id=item.media_id,
    )
    await EventLogRepo(session).log(
        EventType.POST_REVISED,
        "картинка из раздела «Контент»",
        business_id=post.business_id,
        post_id=post.id,
    )
    await session.flush()
    return post
