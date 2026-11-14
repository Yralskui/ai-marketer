"""Применяет подтверждённое вложение к черновику."""

import asyncio
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError

from app.config import IMAGES_DIR
from app.db.knowledge import ProfileDocument
from app.db.models import Platform, PostStatus
from app.db.publications import PostAttachments
from app.ingestion.profile_files import ProfileFileFormatError, render_profile_image
from app.publishers.telegram_text import format_text
from app.services import approval, posts
from app.services.post_history import RevisionSource, save_revision
from app.utils.time import utcnow


async def attach_company_file(session, company, post, pending, *, storage, business_account):
    document = await session.get(ProfileDocument, pending.document_id, with_for_update=True)
    if (
        not storage
        or document is None
        or document.business_id != company.id
        or document.status == "deleted"
    ):
        raise ValueError("Файл компании недоступен")
    if post.status != PostStatus.DRAFT and not posts.can_transition(post.status, PostStatus.DRAFT):
        raise ValueError("Файл можно добавить к черновику на согласовании")
    suffix = Path(document.filename).suffix.lower()
    is_pdf = suffix == ".pdf"
    if post.platform == Platform.VK and (is_pdf or not business_account):
        raise ValueError("Это вложение доступно для Telegram")
    album = await session.get(PostAttachments, post.id)
    items = (
        list(album.items)
        if album
        else (
            [{"path": post.image_path, "type": "image", "name": "Фото"}] if post.image_path else []
        )
    )
    if any(item.get("document_id") == document.id for item in items):
        await session.delete(pending)
        return
    if len(items) >= 10:
        raise ValueError("В посте уже 10 вложений")
    if any((item["type"] == "document") != is_pdf for item in items):
        raise ValueError("Telegram принимает PDF и фотографии в отдельных публикациях")
    if is_pdf and document.size > 50_000_000:
        raise ValueError("Telegram принимает PDF размером до 50 МБ")
    if (
        post.platform == Platform.TELEGRAM
        and format_text(post.text)[1] > posts.TELEGRAM_CAPTION_LIMIT
    ):
        raise ValueError("Сократите текст до 1024 символов и снова попросите приложить файл")
    folder = IMAGES_DIR / "company-attachments" / str(company.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uuid4().hex + (".pdf" if is_pdf else ".jpg"))
    try:
        with TemporaryDirectory(prefix="marketer-attachment-") as tmp:
            original = Path(tmp) / "original"
            await storage.download(document.original_key, original)
            if is_pdf:
                await asyncio.to_thread(copyfile, original, path)
            else:
                rendered = await asyncio.to_thread(
                    render_profile_image, original, suffix, 2048, "JPEG"
                )
                await asyncio.to_thread(path.write_bytes, rendered)
        items.append(
            {
                "path": str(path),
                "name": document.filename,
                "type": "document" if is_pdf else "image",
                "document_id": document.id,
            }
        )
        await session.merge(PostAttachments(post_id=post.id, items=items))
        post.image_path = None
        post.scheduled_at = None
        post.error = None
        post.updated_at = utcnow()
        if post.status != PostStatus.DRAFT:
            posts.transition(post, PostStatus.DRAFT)
        await save_revision(
            session,
            post,
            RevisionSource("chat", request_id=pending.request_id, author_id=pending.user_id),
            instruction=f"Подтверждено вложение: {document.filename}",
            image_change=True,
            image_source="upload",
        )
        await session.delete(pending)
        await approval.submit_for_approval(session, post, None, None)
    except (OSError, BotoCoreError, ClientError, ProfileFileFormatError) as exc:
        path.unlink(missing_ok=True)
        raise ValueError("Не удалось загрузить файл компании") from exc
