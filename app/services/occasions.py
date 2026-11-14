"""Календарь поводов компании: даты из брендбука и отраслевые праздники.

За несколько дней до повода бот сам готовит пост и присылает на согласование — это
автономная задача, которой не нужен диалог и не нужна нейросеть (тексты есть в шаблонах).
Пока календарь есть у стандартного профиля САЛАВАТСТЕКЛО; другим компаниям его можно
добавить, расширив `OCCASIONS`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from app.db.models import Business

LEAD_DAYS = 3
DONE_KEY = "occasions_done"
DONE_LIMIT = 30


@dataclass(frozen=True)
class Occasion:
    key: str
    title: str
    topic: str
    on: Callable[[int], date]


def _fixed(month: int, day: int) -> Callable[[int], date]:
    return lambda year: date(year, month, day)


def _second_sunday_of_august(year: int) -> date:
    first = date(year, 8, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7)


SALAVATSTEKLO_OCCASIONS: tuple[Occasion, ...] = (
    Occasion(
        "saratov",
        "День рождения АО «Саратовстройстекло»",
        "День рождения АО «Саратовстройстекло» — 15 мая 1958 года",
        _fixed(5, 15),
    ),
    Occasion(
        "salavat",
        "День рождения АО «Салаватстекло»",
        "День рождения АО «Салаватстекло» — 30 июня 1962 года",
        _fixed(6, 30),
    ),
    Occasion(
        "builder",
        "День строителя",
        "Поздравление с Днём строителя",
        _second_sunday_of_august,
    ),
    Occasion(
        "caspian",
        "День рождения ООО «Салаватстекло Каспий»",
        "День рождения ООО «Салаватстекло Каспий» — 30 октября 2013 года",
        _fixed(10, 30),
    ),
    Occasion(
        "newyear",
        "Новый год",
        "Поздравление с Новым годом",
        _fixed(12, 31),
    ),
)


def occasions_for(business: Business) -> tuple[Occasion, ...]:
    settings = business.settings or {}
    if settings.get("preset") == "salavatsteklo" or "салаватстекл" in business.name.lower():
        return SALAVATSTEKLO_OCCASIONS
    return ()


def upcoming(business: Business, today: date, limit: int = 4) -> list[tuple[Occasion, date]]:
    """Ближайшие поводы, начиная с сегодняшнего дня."""
    found = []
    for occasion in occasions_for(business):
        when = occasion.on(today.year)
        if when < today:
            when = occasion.on(today.year + 1)
        found.append((occasion, when))
    return sorted(found, key=lambda item: item[1])[:limit]


def mark(occasion: Occasion, when: date) -> str:
    return f"{occasion.key}:{when.year}"


def due(business: Business, today: date) -> list[tuple[Occasion, date]]:
    """Поводы, к которым пора готовить пост и которые ещё не готовили в этом году."""
    done = set((business.settings or {}).get(DONE_KEY) or [])
    return [
        (occasion, when)
        for occasion, when in upcoming(business, today, limit=len(occasions_for(business)))
        if 0 <= (when - today).days <= LEAD_DAYS and mark(occasion, when) not in done
    ]


def mark_done(business: Business, occasion: Occasion, when: date) -> None:
    settings = dict(business.settings or {})
    done = [item for item in settings.get(DONE_KEY) or [] if item != mark(occasion, when)]
    settings[DONE_KEY] = [*done, mark(occasion, when)][-DONE_LIMIT:]
    business.settings = settings  # новый dict — чтобы SQLAlchemy заметил изменение JSON
