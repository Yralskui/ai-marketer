"""Нумерованные изображения профиля, которые пользователь указывает для генерации."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from botocore.exceptions import BotoCoreError, ClientError
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import IMAGES_DIR
from app.db.knowledge import ProfileDocument
from app.images import ImageGenerationError
from app.ingestion.profile_files import IMAGE_EXTENSIONS, fit_image_for_model

MAX_REFERENCES = 3


class ImageReference(BaseModel):
    number: int = Field(gt=0, description="Номер на картинке в интерфейсе")
    role: str = Field(
        "Визуальный референс",
        min_length=1,
        max_length=300,
        description="Что взять из картинки и где разместить в композиции",
    )


def document_image_number(document: ProfileDocument) -> int | None:
    if Path(document.filename).suffix.lower() in IMAGE_EXTENSIONS:
        return document.id * 3
    return None


async def profile_images(session, business_id):
    documents = await session.scalars(
        select(ProfileDocument).where(
            ProfileDocument.business_id == business_id, ProfileDocument.status != "deleted"
        )
    )
    return {number: doc for doc in documents if (number := document_image_number(doc))}


async def numbered_images(session, business_id, storage, *, originals_only=False):
    from app.services import content_gallery

    documents = await profile_images(session, business_id) if storage else {}
    gallery = {
        item.number: item
        for item in await content_gallery.list_content(session, business_id)
        if item.path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        and item.path.resolve().is_relative_to(IMAGES_DIR.resolve())
        and (not originals_only or item.kind == "uploaded")
    }
    return documents, gallery


@asynccontextmanager
async def prepare_references(session, business_id, storage, references, *, originals_only=False):
    if not references:
        yield []
        return
    if len(references) > MAX_REFERENCES:
        raise ImageGenerationError("Можно выбрать до трёх картинок.")
    documents, bank = await numbered_images(
        session, business_id, storage, originals_only=originals_only
    )
    with TemporaryDirectory(prefix="post-references-") as folder:
        prepared = []
        for ref in references:
            if ref.number in bank:
                item = bank[ref.number]
                path = item.path.resolve()
                if not path.is_relative_to(IMAGES_DIR.resolve()) or not path.is_file():
                    raise ImageGenerationError(f"Картинка №{ref.number} недоступна в этом профиле.")
                prepared.append((ref.role, path))
                continue
            doc = documents.get(ref.number)
            if not doc or storage is None:
                raise ImageGenerationError(f"Картинка №{ref.number} недоступна в этом профиле.")
            suffix = Path(doc.filename).suffix.lower()
            original = Path(folder) / f"{ref.number}{suffix}"
            try:
                async with asyncio.timeout(30):
                    await storage.download(doc.original_key, original)
                    data = await asyncio.to_thread(
                        fit_image_for_model, original, suffix, preserve_transparency=True
                    )
            except (OSError, ValueError, BotoCoreError, ClientError, DefusedXmlException) as exc:
                raise ImageGenerationError(f"Не удалось открыть картинку №{ref.number}.") from exc
            target = Path(folder) / f"{ref.number}-ref"
            target = target.with_suffix(".png" if data.startswith(b"\x89PNG") else ".jpg")
            await asyncio.to_thread(target.write_bytes, data)
            prepared.append((ref.role, target))
        yield prepared
