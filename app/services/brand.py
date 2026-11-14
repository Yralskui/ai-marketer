"""Бренд-гид компании — «скилл» для нейросети: как писать и что рисовать в стиле бренда.

Гид хранится в `Business.settings["brand_guide"]` и автоматически добавляется к профилю
бизнеса (`business_profile.profile_to_text`) — значит, попадает во все промпты: посты,
правки, ответы на комментарии, отчёты и системный промпт агента. Визуальная часть
подмешивается в описание картинки (`image_style`).

Источник — брендбук компании (PDF/DOCX/TXT) или правила текстом: LLM вытаскивает из них
структуру (`extract_guide`). Перед согласованием пост сверяется с гидом (`check_text`):
написание названий, кавычки, стоп-слова.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.db.models import Business
from app.llm.base import LLMError, LLMProvider

MAX_SOURCE_CHARS = 15000
MIN_TERM_LEN = 3
# Перед названием — организационно-правовая форма: АО «Салаватстекло», ООО «Салаватстекло Каспий»
LEGAL_NAME_RE = re.compile(r"(?<![\w])(?:АО|ООО|ПАО|ЗАО|ОАО|ИП)\s*«$")


class BrandGuide(BaseModel):
    voice: str = ""  # тон и характер текстов
    values: list[str] = Field(default_factory=list)  # смыслы и ценности бренда
    do: list[str] = Field(default_factory=list)  # обязательные правила текста
    dont: list[str] = Field(default_factory=list)  # запреты
    spelling: list[str] = Field(default_factory=list)  # точное написание названий
    stop_words: list[str] = Field(default_factory=list)  # слова, которых не должно быть
    visual: str = ""  # стиль картинок
    colors: list[str] = Field(default_factory=list)  # фирменные цвета

    @field_validator("values", "do", "dont", "spelling", "stop_words", "colors", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = re.split(r"[\n;]+", value)
        return [str(v).strip() for v in value if str(v).strip()]

    @field_validator("voice", "visual", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> str:
        if isinstance(value, list):
            return "; ".join(str(v) for v in value)
        return str(value or "").strip()

    def is_filled(self) -> bool:
        return any(
            [self.voice, self.values, self.do, self.dont, self.spelling, self.visual, self.colors]
        )

    def merged_with(self, other: BrandGuide) -> BrandGuide:
        """Новые тексты заменяют старые, списки объединяются без повторов."""
        data = self.model_dump()
        for key, value in other.model_dump().items():
            if isinstance(value, list):
                data[key] = list(dict.fromkeys([*data[key], *value]))
            elif value:
                data[key] = value
        return BrandGuide.model_validate(data)


# ---------------------------------------------------------------- хранение


def get_guide(business: Business) -> BrandGuide:
    raw = (business.settings or {}).get("brand_guide") or {}
    try:
        return BrandGuide.model_validate(raw)
    except ValueError:
        return BrandGuide()


def set_guide(business: Business, guide: BrandGuide | None) -> None:
    settings = dict(business.settings or {})
    if guide is None or not guide.is_filled():
        settings.pop("brand_guide", None)
    else:
        settings["brand_guide"] = guide.model_dump()
    business.settings = settings  # новый dict — чтобы SQLAlchemy заметил изменение JSON


# ---------------------------------------------------------------- для промптов


def guide_to_text(guide: BrandGuide) -> str:
    """Бренд-гид для промпта. Пустой гид — пустая строка."""
    if not guide.is_filled():
        return ""
    parts = []
    if guide.voice:
        parts.append(f"Тон и характер: {guide.voice}")
    sections = [
        ("Смыслы и ценности бренда", guide.values),
        ("Обязательно", guide.do),
        ("Нельзя", guide.dont),
        (
            "Точное написание названий (копируй символ в символ, не склоняй, без кавычек)",
            guide.spelling,
        ),
    ]
    for title, items in sections:
        if items:
            parts.append(f"{title}:\n" + "\n".join(f"- {item}" for item in items))
    if guide.visual:
        parts.append(f"Визуальный стиль картинок: {guide.visual}")
    if guide.colors:
        parts.append("Фирменные цвета: " + ", ".join(guide.colors))
    return "\n".join(parts)


def image_style(business: Business) -> str:
    """Добавка к описанию картинки: стиль и цвета бренда, без логотипов и надписей."""
    guide = get_guide(business)
    bits = [guide.visual] if guide.visual else []
    if guide.colors:
        bits.append("цвета: " + ", ".join(guide.colors[:4]))
    if not bits:
        return ""
    return "Стиль бренда: " + "; ".join(bits) + ". Без логотипов и надписей на изображении."


# ---------------------------------------------------------------- проверка текста


def _term_pattern(token: str) -> re.Pattern[str]:
    base = re.escape(token.rstrip("°®™"))
    # Слово целиком: не часть другого слова (Comfort ≠ MultiComfort), знак ° — по желанию
    return re.compile(rf"(?<![\w]){base}[°®™]?(?![\w])", re.IGNORECASE)


def check_text(text: str, guide: BrandGuide) -> list[str]:
    """Нарушения бренд-гида в тексте поста. Пустой список — всё хорошо."""
    issues: list[str] = []
    seen: set[str] = set()
    for entry in guide.spelling:
        token = entry.split()[0]
        if len(token.rstrip("°®™")) < MIN_TERM_LEN or token.lower() in seen:
            continue
        seen.add(token.lower())
        for match in _term_pattern(token).finditer(text):
            found = match.group(0)
            before = text[max(0, match.start() - 8) : match.start()]
            if before.endswith("#"):  # хештег: знаки ° и ® в нём невозможны — не ошибка
                continue
            legal = LEGAL_NAME_RE.search(before) is not None
            if found == token:
                if before.endswith(("«", '"')) and not legal:
                    issues.append(f"«{token}» — без кавычек")
                    break
                continue
            if legal:  # юридическое наименование (АО «…», ООО «…») пишется иначе — это можно
                continue
            issues.append(f"пишите «{token}», а не «{found}»")
            break
    lowered = text.lower()
    for word in guide.stop_words:
        if word.lower() in lowered:
            issues.append(f"стоп-слово «{word}»")
    return issues


# ---------------------------------------------------------------- извлечение из брендбука


async def extract_guide(llm: LLMProvider, source: str) -> BrandGuide:
    """Бренд-гид из текста брендбука или правил. LLM недоступна или mock — эвристика."""
    source = source.strip()[:MAX_SOURCE_CHARS]
    if not source:
        return BrandGuide()
    if llm.name != "mock":
        from app.agent.prompts import render_prompt
        from app.services.business_profile import parse_json_object

        try:
            raw = await llm.complete(
                render_prompt("extract_brand"), source, json_mode=True, task="extract_brand"
            )
            return BrandGuide.model_validate(parse_json_object(raw))
        except (LLMError, ValueError):
            pass
    return _heuristic_guide(source)


def _heuristic_guide(source: str) -> BrandGuide:
    lines = [line.strip(" -•\t") for line in source.splitlines() if line.strip(" -•\t")]
    colors = re.findall(r"#[0-9A-Fa-f]{6}\b", source)
    dont = [
        line for line in lines if re.search(r"\b(нельзя|запрещ|не используй|не пиши)", line, re.I)
    ]
    do = [
        line for line in lines if re.search(r"\b(всегда|обязательно|пишется|пишутся)\b", line, re.I)
    ]
    rest = [line for line in lines if line not in dont and line not in do]
    return BrandGuide(
        voice="; ".join(rest[:3])[:500],
        do=do[:10],
        dont=dont[:10],
        colors=list(dict.fromkeys(c.upper() for c in colors))[:8],
    )
