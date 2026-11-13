"""Профиль бизнеса: сбор из сырых материалов через LLM и ручное обновление."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import render_prompt
from app.db.models import Business, EventType, Source, SourceType
from app.db.repositories import BusinessRepo, EventLogRepo, SourceRepo
from app.llm.base import LLMError, LLMProvider

log = logging.getLogger(__name__)

# Сколько символов сырых материалов отдаём LLM за раз.
# TODO(поток A): суммаризация больших документов по частям вместо обрезки.
MAX_SOURCES_CHARS = 15_000

PROFILE_FIELD_TITLES: dict[str, str] = {
    "name": "Название",
    "description": "Чем занимается",
    "products": "Продукты и услуги",
    "audience": "Целевая аудитория",
    "usp": "УТП",
    "tone": "Тон общения",
    "restrictions": "Запреты",
}


class NoSourcesError(ValueError):
    """У бизнеса ещё нет ни одного материала для профиля."""


class BusinessProfile(BaseModel):
    """Структура JSON-поля Business.profile."""

    name: str = ""
    description: str = ""
    products: list[str] = []
    audience: str = ""
    usp: str = ""
    tone: str = ""
    restrictions: list[str] = []

    @field_validator("products", "restrictions", mode="before")
    @classmethod
    def _split_str(cls, value: Any) -> Any:
        # LLM иногда возвращает строку вместо списка
        if isinstance(value, str):
            return [p.strip(" -•") for p in re.split(r"[,;\n]", value) if p.strip(" -•")]
        return value or []

    @field_validator("name", "description", "audience", "usp", "tone", mode="before")
    @classmethod
    def _none_to_str(cls, value: Any) -> Any:
        if isinstance(value, list):
            return ", ".join(map(str, value))
        return value or ""

    def is_filled(self) -> bool:
        return bool(self.description or self.products or self.usp)

    def merged_with(self, other: BusinessProfile) -> BusinessProfile:
        """Новые непустые значения заменяют старые."""
        data = self.model_dump()
        for key, value in other.model_dump().items():
            if value:
                data[key] = value
        return BusinessProfile.model_validate(data)


def get_profile(business: Business) -> BusinessProfile:
    profile = BusinessProfile.model_validate(business.profile or {})
    if not profile.name:
        profile.name = business.name
    return profile


def profile_to_text(business: Business) -> str:
    """Профиль простым текстом — для промптов LLM.

    Если у компании есть бренд-гид, он добавляется в конец: так стиль бренда попадает во все
    промпты, которые получают профиль (посты, правки, комментарии, отчёты, агент).
    """
    from app.services.brand import get_guide, guide_to_text

    profile = get_profile(business)
    if not profile.is_filled():
        text = f"Профиль пока не заполнен. Известно только название: {profile.name}."
    else:
        lines = []
        for key, title in PROFILE_FIELD_TITLES.items():
            value = getattr(profile, key)
            if isinstance(value, list):
                value = "; ".join(value)
            lines.append(f"- {title}: {value or '—'}")
        text = "\n".join(lines)
    guide = guide_to_text(get_guide(business))
    if guide:
        text += "\n\n### Бренд-гид (соблюдай строго)\n" + guide
    return text


async def add_source(
    session: AsyncSession, business: Business, type_: SourceType, origin: str, text: str
) -> Source:
    source = await SourceRepo(session).add(business.id, type_, origin, text)
    await EventLogRepo(session).log(
        EventType.SOURCE_ADDED,
        f"{type_.value}: {origin}",
        business_id=business.id,
        payload={"chars": len(text)},
    )
    return source


async def build_profile(
    session: AsyncSession, llm: LLMProvider, business: Business
) -> BusinessProfile:
    """Собирает профиль из всех источников бизнеса и сохраняет его."""
    sources = await SourceRepo(session).list_for_business(business.id)
    if not sources:
        raise NoSourcesError(
            "Нет материалов о бизнесе: расскажите о нём текстом, файлом или ссылкой"
        )

    current = get_profile(business)
    system = render_prompt(
        "extract_profile",
        current_profile=json.dumps(current.model_dump(), ensure_ascii=False, indent=2),
    )
    user = "Материалы о бизнесе:\n" + combine_sources(sources, MAX_SOURCES_CHARS)
    raw = await llm.complete(system, user, json_mode=True, task="extract_profile")
    extracted = BusinessProfile.model_validate(parse_json_object(raw))
    profile = current.merged_with(extracted)

    await BusinessRepo(session).set_profile(business, profile.model_dump())
    await EventLogRepo(session).log(
        EventType.PROFILE_UPDATED, "Профиль собран из материалов", business_id=business.id
    )
    return profile


async def update_profile_fields(
    session: AsyncSession, business: Business, updates: dict[str, Any]
) -> BusinessProfile:
    """Точечное обновление полей (например, из инструмента агента)."""
    unknown = set(updates) - set(PROFILE_FIELD_TITLES)
    if unknown:
        raise ValueError(f"Неизвестные поля профиля: {', '.join(sorted(unknown))}")
    data = get_profile(business).model_dump()
    data.update({k: v for k, v in updates.items() if v is not None})
    profile = BusinessProfile.model_validate(data)
    await BusinessRepo(session).set_profile(business, profile.model_dump())
    await EventLogRepo(session).log(
        EventType.PROFILE_UPDATED,
        f"Обновлены поля: {', '.join(sorted(updates))}",
        business_id=business.id,
    )
    return profile


def combine_sources(sources: list[Source], max_chars: int) -> str:
    """Склеивает источники, честно деля лимит символов между ними."""
    per_source = max(500, max_chars // max(1, len(sources)))
    parts = []
    for s in sources:
        text = s.text.strip()
        if len(text) > per_source:
            text = text[:per_source] + " …[обрезано]"
        parts.append(f"### {s.type.value}: {s.origin}\n{text}")
    return "\n\n".join(parts)[:max_chars]


def parse_json_object(raw: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа LLM (в т.ч. обёрнутый в ```json ... ```)."""
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        raise LLMError(f"LLM не вернула JSON: {raw[:200]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"Битый JSON от LLM: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("LLM вернула не объект")
    return data
