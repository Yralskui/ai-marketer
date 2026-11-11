"""Работа со временем.

Соглашение проекта: в БД все даты хранятся как *naive UTC* (SQLite не хранит пояс).
Наружу (пользователю, в разбор дат) — в часовом поясе из настроек `TIMEZONE`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, tzinfo

import dateparser


class DateParseError(ValueError):
    """Не удалось понять дату/время из текста."""


def utcnow() -> datetime:
    """Текущее время в naive UTC — формат хранения в БД."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_utc_naive(dt: datetime) -> datetime:
    """Aware-дату переводит в naive UTC. Naive считается уже UTC."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def to_local(dt: datetime, tz: tzinfo) -> datetime:
    """Naive UTC из БД -> aware-дата в поясе пользователя."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz)


def format_local(dt: datetime | None, tz: tzinfo) -> str:
    """Человекочитаемая дата в поясе пользователя."""
    if dt is None:
        return "—"
    return to_local(dt, tz).strftime("%d.%m.%Y %H:%M")


# «в 10», «в 10 часов» -> «в 10:00»: иначе dateparser принимает число за день месяца
_BARE_HOUR = re.compile(r"\bв (\d{1,2})(?: час(?:а|ов)?)?(?=\s|$)")


def _normalize(text: str) -> tuple[str, bool]:
    """Чистит текст и запоминает, нужно ли прибавить 12 часов («вечера»)."""
    t = " " + text.strip().lower().replace("ё", "е") + " "
    pm = False
    for marker in (" вечера", " вечером", " дня", " ночи", " утра", " утром"):
        if marker in t:
            pm = pm or marker in (" вечера", " вечером", " дня")
            t = t.replace(marker, " ")
    t = " ".join(t.split())
    return _BARE_HOUR.sub(r"в \1:00", t), pm


def parse_schedule_time(text: str, tz: tzinfo, now: datetime | None = None) -> datetime:
    """Разбирает дату/время из свободного текста («завтра в 10 утра», «25.11 18:30»).

    Возвращает aware-дату в поясе `tz`. Бросает DateParseError, если не понял
    или если время уже прошло.
    """
    now_local = (now or datetime.now(tz)).astimezone(tz)
    normalized, pm = _normalize(text)
    parsed = dateparser.parse(
        normalized,
        languages=["ru", "en"],
        settings={
            "TIMEZONE": str(tz),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now_local.replace(tzinfo=None),
            "DATE_ORDER": "DMY",
        },
    )
    if parsed is None:
        raise DateParseError(f"Не понял дату: {text!r}")
    parsed = parsed.astimezone(tz)
    if pm and parsed.hour < 12:
        parsed = parsed.replace(hour=parsed.hour + 12)
    if parsed <= now_local:
        raise DateParseError("Это время уже прошло")
    return parsed
