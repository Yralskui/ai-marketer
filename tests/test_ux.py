"""Удобство: быстрое время, «Состояние» клиента, карточка черновика."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.bot import texts
from app.db.models import Platform, Post, PostStatus
from app.utils.time import quick_schedule_options

TZ = ZoneInfo("Asia/Yekaterinburg")


def test_quick_times_are_future_and_sorted():
    now = datetime(2026, 11, 13, 9, 10, tzinfo=UTC)  # 14:10 по Екатеринбургу
    options = quick_schedule_options(TZ, now)
    times = [when for _, when in options]
    assert all(when > now for when in times)
    assert times == sorted(times)
    labels = [label for label, _ in options]
    assert labels[0].startswith("Через час") and "Сегодня 18:00" in labels
    assert "Завтра 10:00" in labels


def test_no_evening_option_late_in_the_day():
    late = datetime(2026, 11, 13, 13, 0, tzinfo=UTC)  # 18:00 по Екатеринбургу
    labels = [label for label, _ in quick_schedule_options(TZ, late)]
    assert "Сегодня 18:00" not in labels


def test_client_status_has_no_tech_details():
    text = texts.client_status(
        company="Кофейня",
        channel="<code>@coffee</code> — ✅",
        counts={PostStatus.PENDING_APPROVAL: 2, PostStatus.PUBLISHED: 5},
        month_cost="12.00 ₽",
    )
    assert "Ждут вашего решения: <b>2</b>" in text and "Опубликовано всего: <b>5</b>" in text
    for tech in ("LLM", "mock", "DRY-RUN", "Инструментов", "Планировщик"):
        assert tech not in text


def test_draft_card_without_text_for_photo_preview():
    post = Post(id=7, text="Длинный текст поста", platform=Platform.TELEGRAM,
                status=PostStatus.PENDING_APPROVAL)  # fmt: skip
    card = texts.draft_card(post, channel="@c", tz=TZ, with_text=False)
    assert "Так пост будет выглядеть" in card and "Черновик #7" in card
    assert "Длинный текст поста" not in card
