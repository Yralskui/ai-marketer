"""Фоновая обработка материалов. У каждой записи свой набор Markdown и векторов."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import Database
from app.db.knowledge import KnowledgeChunk, ProfileDocument
from app.db.models import Business, Source
from app.ingestion.knowledge import KnowledgeModels
from app.services import default_profile
from app.services.business_profile import profile_to_text
from app.services.object_storage import ObjectStorage
from app.utils.time import utcnow

log = logging.getLogger(__name__)


async def claim(session: AsyncSession) -> ProfileDocument | None:
    cutoff = utcnow() - timedelta(minutes=45)
    stmt = (
        select(ProfileDocument)
        .where(
            or_(
                ProfileDocument.status == "queued",
                (ProfileDocument.status == "processing") & (ProfileDocument.updated_at < cutoff),
            )
        )
        .order_by(ProfileDocument.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    document = await session.scalar(stmt)
    if document is not None:
        document.status = "processing"
        document.updated_at = utcnow()
        await session.commit()
    return document


async def process(
    database: Database, storage: ObjectStorage, models: KnowledgeModels, document: ProfileDocument
) -> None:
    async with database.sessionmaker() as session:
        business = await session.get(Business, document.business_id)
        profile = profile_to_text(business) if business is not None else ""
    keys = []
    completed = False
    try:
        if default_profile.is_preset_document(document):
            # Встроенная база знаний уже разбита на фрагменты — модель не нужна
            chunks = list(default_profile.knowledge_chunks())
            async with asyncio.timeout(600):
                vectors = await models.embed(chunks)
        else:
            with TemporaryDirectory(prefix="marketer-material-") as tmp:
                path = Path(tmp) / ("original" + Path(document.filename).suffix.lower())
                await storage.download(document.original_key, path)
                async with asyncio.timeout(2400):
                    chunks = await models.analyze(path, profile)
                    vectors = await models.embed(chunks)
        if not chunks or len(chunks) != len(vectors):
            raise ValueError("Не удалось сформировать индекс")
        async with database.sessionmaker() as session:
            current = await session.scalar(
                select(ProfileDocument).where(ProfileDocument.id == document.id).with_for_update()
            )
            if (
                current is None
                or current.status != "processing"
                or current.updated_at != document.updated_at
            ):
                return
            generation = uuid4().hex
            for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                key = f"profiles/{document.business_id}/markdown/{document.digest}/{generation}/{i:04}.md"
                await storage.put_markdown(key, chunk)
                keys.append(key)
                session.add(
                    KnowledgeChunk(
                        document_id=document.id,
                        position=i,
                        markdown_key=key,
                        content=chunk,
                        embedding=vector,
                    )
                )
            current.status = "ready"
            current.error = None
            await session.commit()
            completed = True
    except Exception as exc:
        log.warning("Не обработан материал %s (%s)", document.id, type(exc).__name__)
        async with database.sessionmaker() as session:
            current = await session.scalar(
                select(ProfileDocument).where(ProfileDocument.id == document.id).with_for_update()
            )
            if (
                current
                and current.status == "processing"
                and current.updated_at == document.updated_at
            ):
                current.status = "failed"
                current.error = (
                    str(exc)[:200]
                    if isinstance(exc, ValueError)
                    else "Ошибка обработки. Повторите попытку."
                )
                await session.commit()
    finally:
        if not completed:
            for key in keys:
                await storage.delete(key)


async def cleanup_one(database: Database, storage: ObjectStorage) -> bool:
    async with database.sessionmaker() as session:
        document = await session.scalar(
            select(ProfileDocument)
            .where(ProfileDocument.status == "deleted")
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if document is None:
            return False
        keys = list(
            await session.scalars(
                select(KnowledgeChunk.markdown_key).where(KnowledgeChunk.document_id == document.id)
            )
        )
        originals = [] if default_profile.is_preset_document(document) else [document.original_key]
        for key in [*originals, *keys]:
            await storage.delete(key)
        await session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
        )
        source = await session.get(Source, document.source_id)
        await session.delete(document)
        await session.flush()
        if source is not None:
            await session.delete(source)
        await session.commit()
        return True


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    db = Database(settings.effective_database_url)
    storage = ObjectStorage(settings)
    models = KnowledgeModels(settings)
    await db.create_all()
    await storage.prepare()
    try:
        while True:
            try:
                async with db.sessionmaker() as session:
                    document = await claim(session)
                if document is not None:
                    await process(db, storage, models, document)
                elif not await cleanup_one(db, storage):
                    await asyncio.sleep(2)
            except Exception:
                log.exception("Ошибка очереди материалов")
                await asyncio.sleep(5)
    finally:
        await models.client.close()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
