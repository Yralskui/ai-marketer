"""Стандартный профиль «САЛАВАТСТЕКЛО»: шаблон карточки и готовая база знаний в коде.

Профиль создаётся каждому пользователю при первом входе в Mini App и помечается
`settings["preset"]`. Такой профиль нельзя удалить, а его материал базы знаний — убрать.
База знаний лежит в `app/defaults/salavatsteklo/knowledge/*.md`: имя файла — раздел
(business, voice, visual, reference), каждый блок «## …» — готовый фрагмент. Воркер не
разбирает её моделью, а только считает эмбеддинги.
"""

from __future__ import annotations

import re
from functools import cache
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.knowledge import ProfileDocument
from app.db.models import Business, SourceType
from app.services import brand
from app.services.brand_presets import SALAVATSTEKLO
from app.services.business_profile import BusinessProfile, add_source

PRESET_ID = "salavatsteklo"
PRESET_DIR = Path(__file__).resolve().parent.parent / "defaults" / PRESET_ID
PROFILE_FILE = PRESET_DIR / "profile.json"
KNOWLEDGE_DIR = PRESET_DIR / "knowledge"
# Ключ вместо файла в S3: по нему воркер узнаёт встроенный материал
PRESET_KEY_PREFIX = "preset:"
DOCUMENT_NAME = "САЛАВАТСТЕКЛО — стандартная база знаний.md"
SECTIONS = ("business", "voice", "visual", "reference")
SOURCE_LABEL = "Брендбук САЛАВАТСТЕКЛО от 15.11.2023 (стандартный профиль)"


class PresetLockedError(ValueError):
    """Попытка удалить стандартный профиль или его базу знаний."""


def is_preset(business: Business) -> bool:
    return bool((business.settings or {}).get("preset"))


def is_preset_document(document: ProfileDocument) -> bool:
    return document.original_key.startswith(PRESET_KEY_PREFIX)


def load_template(path: str | Path | None = None) -> BusinessProfile:
    """Карточка профиля: свой файл из DEFAULT_PROFILE_FILE или встроенный."""
    template = BusinessProfile.model_validate_json(Path(path or PROFILE_FILE).read_bytes())
    if not template.name:
        raise ValueError("У шаблона компании должно быть имя")
    return template


def uses_bundled(path: str | Path | None) -> bool:
    return not path or Path(path).resolve() == PROFILE_FILE


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


@cache
def knowledge_chunks() -> tuple[str, ...]:
    """Фрагменты в том же формате, что делает воркер из загруженных материалов."""
    chunks = []
    for section in SECTIONS:
        text = _strip_comments((KNOWLEDGE_DIR / f"{section}.md").read_text(encoding="utf-8"))
        for block in re.split(r"(?m)^## ", text):
            title, _, body = block.strip().partition("\n")
            if not title or not body.strip():
                continue
            chunks.append(
                f"## {title.strip()}\n\nИсточник: {SOURCE_LABEL}\nРаздел: {section}\n\n"
                f"{body.strip()}"
            )
    return tuple(chunks)


def apply_brand_guide(business: Business) -> None:
    """Бренд-гид САЛАВАТСТЕКЛО: правила написания для проверки постов и стиль картинок."""
    if not brand.get_guide(business).is_filled():
        brand.set_guide(business, SALAVATSTEKLO)


def knowledge_text() -> str:
    return "\n\n".join(knowledge_chunks())


def knowledge_digest() -> str:
    return sha256(knowledge_text().encode()).hexdigest()


async def seed_knowledge(session: AsyncSession, business: Business) -> ProfileDocument:
    """Ставит встроенную базу знаний в очередь воркера (повторный вызов ничего не дублирует)."""
    digest = knowledge_digest()
    existing = await session.scalar(
        select(ProfileDocument).where(
            ProfileDocument.business_id == business.id, ProfileDocument.digest == digest
        )
    )
    if existing is not None:
        if existing.status == "deleted":
            existing.status = "queued"
            existing.error = None
        return existing
    text = knowledge_text()
    source = await add_source(
        session,
        business,
        SourceType.DOCUMENT,
        DOCUMENT_NAME,
        text,
        document_bytes=len(text.encode()),
    )
    document = ProfileDocument(
        business_id=business.id,
        source_id=source.id,
        filename=DOCUMENT_NAME,
        digest=digest,
        original_key=f"{PRESET_KEY_PREFIX}{PRESET_ID}/{digest}",
        size=len(text.encode()),
    )
    session.add(document)
    await session.flush()
    return document
