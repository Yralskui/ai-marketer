"""Профиль бизнеса через mock-LLM, картинка-заглушка, задачи планировщика."""

from __future__ import annotations

import pytest
from PIL import Image

from app.db.models import SourceType
from app.services import business_profile
from app.services.business_profile import BusinessProfile, NoSourcesError, parse_json_object


async def test_build_profile_from_text(session, owner, llm):
    _, business = owner
    await business_profile.add_source(
        session,
        business,
        SourceType.TEXT,
        "сообщение",
        "Мы кофейня «Зерно» в центре Екатеринбурга. Предлагаем: эспрессо, капучино и выпечку. "
        "Наши клиенты — студенты и офисные работники. Нельзя упоминать конкурентов.",
    )
    profile = await business_profile.build_profile(session, llm, business)
    assert profile.name == "Зерно"
    assert business.name == "Зерно"
    assert "капучино" in profile.products
    assert any("конкурент" in r for r in profile.restrictions)
    assert profile.is_filled()
    assert "Зерно" in business_profile.profile_to_text(business)


async def test_build_profile_without_sources(session, owner, llm):
    _, business = owner
    with pytest.raises(NoSourcesError):
        await business_profile.build_profile(session, llm, business)


def test_profile_lenient_parsing():
    profile = BusinessProfile.model_validate({"products": "кофе, чай\nвыпечка", "tone": None})
    assert profile.products == ["кофе", "чай", "выпечка"]
    assert profile.tone == ""
    assert parse_json_object('Вот:\n```json\n{"name": "X"}\n```') == {"name": "X"}


async def test_mock_image(images):
    path = await images.generate("Осеннее меню в кофейне")
    assert path.exists()
    with Image.open(path) as img:
        assert img.size == (1024, 1024)
    assert await images.generate("Осеннее меню в кофейне") == path  # детерминированно


def test_scheduler_jobs_follow_flags(settings):
    """Заглушечные задачи включаются только флагами ENABLE_*."""
    from app.container import build_container
    from app.scheduler.setup import create_scheduler

    container = build_container(settings)
    assert [j.id for j in create_scheduler(container).get_jobs()] == ["publish_due_posts"]
    settings.enable_auto_drafts = True
    settings.enable_weekly_report = True
    ids = {j.id for j in create_scheduler(container).get_jobs()}
    assert ids == {"publish_due_posts", "generate_daily_drafts", "weekly_report"}
