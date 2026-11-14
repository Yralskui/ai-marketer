"""Общая квота документов и одноразовые ссылки на загрузку."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import LargeBinary, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.db.knowledge import ProfileDocument
from app.db.models import Business, EventLog, EventType, Source, SourceType
from app.ingestion.profile_files import IMAGE_EXTENSIONS, MAX_PROFILE_IMAGES
from app.services.business_profile import add_source

if TYPE_CHECKING:
    from app.services.object_storage import ObjectStorage


PROFILE_DOCUMENT_BYTES = 100_000_000
LINK_TTL_SECONDS = 10 * 60
MAX_PENDING_LINKS = 500


class DocumentQuotaError(ValueError):
    """Файл не помещается в квоту компании."""


class ImageLimitError(DocumentQuotaError):
    """В профиле уже максимум отдельных картинок: каждую смотрит тяжёлая модель."""

    def __init__(self) -> None:
        super().__init__(
            f"В профиле может быть не больше {MAX_PROFILE_IMAGES} отдельных картинок. "
            "Удалите лишнюю или добавьте картинки в PDF."
        )


async def image_count(session: AsyncSession, business_id: int) -> int:
    names = await session.scalars(
        select(ProfileDocument.filename).where(
            ProfileDocument.business_id == business_id, ProfileDocument.status != "deleted"
        )
    )
    return sum(1 for name in names if Path(name).suffix.lower() in IMAGE_EXTENSIONS)


async def remaining_bytes(session: AsyncSession, business_id: int) -> int:
    sources = (
        await session.execute(
            select(Source.id, func.length(cast(Source.text, LargeBinary))).where(
                Source.business_id == business_id, Source.type == SourceType.DOCUMENT
            )
        )
    ).all()
    events = await session.scalars(
        select(EventLog.payload).where(
            EventLog.business_id == business_id, EventLog.type == EventType.SOURCE_ADDED
        )
    )
    deleted = set(
        await session.scalars(
            select(ProfileDocument.source_id).where(
                ProfileDocument.business_id == business_id, ProfileDocument.status == "deleted"
            )
        )
    )
    sizes = {event["source_id"]: event["bytes"] for event in events if event and "bytes" in event}
    used = sum(
        sizes.get(source_id, text_bytes)
        for source_id, text_bytes in sources
        if source_id not in deleted
    )
    return max(0, PROFILE_DOCUMENT_BYTES - used)


@contextmanager
def stage_document(business_id: int) -> Iterator[Path]:
    directory = DATA_DIR / "documents" / str(business_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with TemporaryDirectory(prefix=".upload-", dir=directory) as tmp:
        yield Path(tmp) / "document"


async def save_document(
    session: AsyncSession,
    business: Business,
    filename: str,
    staged: Path,
    *,
    storage: ObjectStorage | None = None,
) -> None:
    size = staged.stat().st_size
    if not 0 < size <= PROFILE_DOCUMENT_BYTES:
        raise DocumentQuotaError
    # Блокировка записи перед повторной проверкой общей квоты.
    await session.execute(
        update(Business).where(Business.id == business.id).values(updated_at=Business.updated_at)
    )
    await session.refresh(business, ["settings"])
    if (business.settings or {}).get("miniapp_archived"):
        raise DocumentQuotaError("Профиль удалён")
    if storage is not None:
        await save_to_storage(session, business, filename, staged, size, storage)
        return
    if size > await remaining_bytes(session, business.id):
        raise DocumentQuotaError
    target = staged.parent.parent / f"{uuid4().hex}{Path(filename).suffix.lower()}"
    staged.chmod(0o600)
    staged.replace(target)
    try:
        await add_source(
            session,
            business,
            SourceType.DOCUMENT,
            filename,
            "",
            document_bytes=size,
            document_path=str(target.relative_to(DATA_DIR)),
        )
        await session.commit()
    except Exception:
        target.unlink(missing_ok=True)
        raise


async def save_to_storage(
    session: AsyncSession,
    business: Business,
    filename: str,
    staged: Path,
    size: int,
    storage: ObjectStorage,
) -> None:
    def digest_file() -> str:
        with staged.open("rb") as file:
            return file_digest(file, "sha256").hexdigest()

    digest = await asyncio.to_thread(digest_file)
    previous = await session.scalar(
        select(ProfileDocument).where(
            ProfileDocument.business_id == business.id,
            ProfileDocument.digest == digest,
        )
    )
    if previous and previous.status != "deleted":
        await session.commit()
        return
    if (
        Path(filename).suffix.lower() in IMAGE_EXTENSIONS
        and await image_count(session, business.id) >= MAX_PROFILE_IMAGES
    ):
        raise ImageLimitError
    if size > await remaining_bytes(session, business.id):
        raise DocumentQuotaError
    if previous is not None:
        raise DocumentQuotaError("Файл удаляется. Повторите через несколько секунд.")
    key = f"profiles/{business.id}/originals/{uuid4().hex}"
    await storage.upload(staged, key)
    try:
        source = await add_source(
            session, business, SourceType.DOCUMENT, filename, "", document_bytes=size
        )
        session.add(
            ProfileDocument(
                business_id=business.id,
                source_id=source.id,
                filename=filename[:255],
                digest=digest,
                original_key=key,
                size=size,
            )
        )
        await session.commit()
    except Exception:
        await session.rollback()
        await storage.delete(key)
        raise


@dataclass(frozen=True)
class UploadGrant:
    user_id: int
    business_id: int
    expires_at: float


class UploadLinks:
    def __init__(self) -> None:
        self.pending: dict[bytes, UploadGrant] = {}

    def issue(self, user_id: int, business_id: int) -> str:
        now = monotonic()
        self.pending = {
            key: grant
            for key, grant in self.pending.items()
            if grant.expires_at > now and grant.user_id != user_id
        }
        if len(self.pending) >= MAX_PENDING_LINKS:
            self.pending.pop(next(iter(self.pending)))
        token = secrets.token_urlsafe(32)
        self.pending[sha256(token.encode()).digest()] = UploadGrant(
            user_id, business_id, now + LINK_TTL_SECONDS
        )
        return token

    def consume(self, token: str) -> UploadGrant | None:
        if len(token) != 43 or not token.isascii():
            return None
        grant = self.pending.pop(sha256(token.encode()).digest(), None)
        return grant if grant and grant.expires_at > monotonic() else None
