"""Шаблонные посты без нейросети: осмысленные и по бренд-гиду."""

import pytest

from app.llm.mock_provider import MockLLMProvider
from app.llm.post_templates import (
    PRODUCT_TEMPLATE,
    SALAVATSTEKLO,
    SALAVATSTEKLO_DEFAULT,
    render_post,
)
from app.services.brand import check_text
from app.services.brand_presets import SALAVATSTEKLO as GUIDE


@pytest.mark.parametrize(
    "topic",
    [
        "MultiComfort Light Blue для фасадов",
        "мультикомфорт для бизнес-центра",
        "историю завода к годовщине",
        "наших сотрудников",
        "поиск дилеров в регионах",
        "стекло для окон",
        "зеркала для интерьера",
        "испытания печей закалки",
        "что-то совсем другое",
    ],
)
def test_salavatsteklo_posts_follow_brand_guide(topic):
    post = render_post(topic, "Салаватстекло")
    assert "САЛАВАТСТЕКЛО" in post
    assert check_text(post, GUIDE) == []
    assert "{" not in post


def test_every_template_passes_brand_check():
    for template in (*SALAVATSTEKLO, SALAVATSTEKLO_DEFAULT, PRODUCT_TEMPLATE):
        text = template.text.format(
            topic="тема",
            Topic="Тема",
            business="САЛАВАТСТЕКЛО",
            product="MultiComfort° Next",
            tag="x",
        )
        assert check_text(text, GUIDE) == [], template.text[:40]


def test_product_is_named_correctly():
    post = render_post("multicomfort light blue для фасадов", "Салаватстекло")
    assert post.startswith("MultiComfort° Light Blue 55/38")


def test_topic_picks_matching_template():
    assert "1962" in render_post("история компании", "Салаватстекло")
    assert "Экологичность" in render_post("экология производства", "Салаватстекло")


def test_other_companies_get_neutral_post_with_topic():
    post = render_post("осеннее меню", "Кофейня Зерно")
    assert "осеннее меню" in post and "Кофейня Зерно" in post
    assert "САЛАВАТСТЕКЛО" not in post


async def test_mock_writes_template_post():
    llm = MockLLMProvider()
    text = await llm.complete(
        "system", "Тема: история компании\nНазвание бизнеса: Салаватстекло", task="write_post"
    )
    assert "1962" in text
