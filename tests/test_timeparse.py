"""Разбор времени планирования из свободного текста."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.utils.time import DateParseError, parse_schedule_time, to_local, to_utc_naive

TZ = ZoneInfo("Asia/Yekaterinburg")
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=TZ)  # среда, 15:00


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("завтра в 10:00", datetime(2026, 11, 12, 10, 0)),
        ("завтра в 10 утра", datetime(2026, 11, 12, 10, 0)),
        ("завтра в 7 вечера", datetime(2026, 11, 12, 19, 0)),
        ("сегодня в 18:00", datetime(2026, 11, 11, 18, 0)),
        ("через 2 часа", datetime(2026, 11, 11, 17, 0)),
        ("25.11 18:30", datetime(2026, 11, 25, 18, 30)),
        ("25 ноября в 9:15", datetime(2026, 11, 25, 9, 15)),
    ],
)
def test_parse(text, expected):
    result = parse_schedule_time(text, TZ, now=NOW)
    assert result.tzinfo is not None
    assert result.replace(tzinfo=None) == expected


@pytest.mark.parametrize("text", ["вчера", "абракадабра", ""])
def test_parse_errors(text):
    with pytest.raises(DateParseError):
        parse_schedule_time(text, TZ, now=NOW)


def test_utc_roundtrip():
    local = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
    stored = to_utc_naive(local)
    assert stored == datetime(2026, 11, 12, 5, 0)  # UTC+5
    assert to_local(stored, TZ) == local
