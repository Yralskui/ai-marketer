"""Галерея сгенерированных изображений, связанных с версиями постов."""

from pathlib import Path

from sqlalchemy import func, select

from app.config import IMAGES_DIR
from app.db.knowledge import HiddenGeneratedImage
from app.db.models import Post, PostRevision
from app.db.publications import RevisionProvenance

LIMIT = 100_000_000


async def entries(session, business_id):
    first = func.min(RevisionProvenance.revision_id).label("id")
    candidates = (
        select(first, RevisionProvenance.image_path.label("path"))
        .join(PostRevision, PostRevision.id == RevisionProvenance.revision_id)
        .join(Post, Post.id == PostRevision.post_id)
        .where(
            Post.business_id == business_id,
            RevisionProvenance.image_source == "generated",
            RevisionProvenance.image_path.is_not(None),
        )
        .group_by(RevisionProvenance.image_path)
        .subquery()
    )
    rows = await session.execute(
        select(candidates.c.id, candidates.c.path)
        .outerjoin(HiddenGeneratedImage, HiddenGeneratedImage.revision_id == candidates.c.id)
        .where(HiddenGeneratedImage.revision_id.is_(None))
        .order_by(candidates.c.id)
    )
    root = IMAGES_DIR.resolve()
    result = []
    remaining = LIMIT
    for revision_id, value in rows:
        path = Path(value).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            continue
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if not 0 < size <= remaining:
            continue
        remaining -= size
        result.append((revision_id, path, size))
    return result


async def available_bytes(session, business_id):
    return LIMIT - sum(size for _, _, size in await entries(session, business_id))
