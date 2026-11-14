"""Бренд-гид: проверка написания, попадание в промпты и картинки, извлечение из текста."""

from __future__ import annotations

from app.llm.mock_provider import MockLLMProvider
from app.services import brand, posts
from app.services.brand import BrandGuide
from app.services.brand_presets import SALAVATSTEKLO
from app.services.business_profile import profile_to_text


def test_salavatsteklo_spelling_violations():
    text = "Новинка от Салаватстекло: стекло multicomfort Light Blue для фасадов."
    issues = brand.check_text(text, SALAVATSTEKLO)
    assert any("САЛАВАТСТЕКЛО" in i for i in issues)
    assert any("MultiComfort°" in i for i in issues)


def test_correct_text_passes():
    text = (
        "САЛАВАТСТЕКЛО представляет MultiComfort° Light Blue 55/38 — для светлых фасадов. "
        "Производитель — АО «Салаватстекло»."
    )
    assert brand.check_text(text, SALAVATSTEKLO) == []


def test_product_in_quotes_is_flagged():
    issues = brand.check_text("Серия «Mirroline» для интерьеров", SALAVATSTEKLO)
    assert any("без кавычек" in i for i in issues)


def test_no_false_positive_inside_other_word():
    # «Comfort°» — отдельная серия; внутри «MultiComfort°» её искать нельзя
    assert brand.check_text("MultiComfort° Next", SALAVATSTEKLO) == []


def test_stop_words():
    guide = BrandGuide(stop_words=["только сегодня"])
    assert brand.check_text("Скидка только сегодня!", guide) == ["стоп-слово «только сегодня»"]


async def test_guide_goes_into_every_prompt(owner):
    _, business = owner
    assert "Бренд-гид" not in profile_to_text(business)
    brand.set_guide(business, SALAVATSTEKLO)
    text = profile_to_text(business)
    assert "Бренд-гид" in text and "MultiComfort° Light Blue 55/38" in text
    assert "#196C7F" in text


async def test_images_follow_brand_style(session, owner, llm, images):
    _, business = owner
    brand.set_guide(business, SALAVATSTEKLO)
    prompts: list[str] = []

    class SpyImages(type(images)):
        async def generate(self, prompt, *, size=(1024, 1024)):
            prompts.append(prompt)
            return await super().generate(prompt, size=size)

    await posts.create_draft(
        session, llm, SpyImages(images.output_dir), business, "новые фасады", with_image=True
    )
    assert "стеклянными фасадами" in prompts[0] and "#196C7F" in prompts[0]
    assert "Без логотипов" in prompts[0]


async def test_extract_from_plain_rules_without_llm():
    guide = await brand.extract_guide(
        MockLLMProvider(),
        "Тон деловой и спокойный.\nНельзя писать капслоком.\nЦвета: #196C7F и #dde1e1",
    )
    assert "#196C7F" in guide.colors and "#DDE1E1" in guide.colors
    assert any("Нельзя" in d for d in guide.dont)


def test_merge_keeps_old_and_adds_new():
    old = BrandGuide(voice="старый", spelling=["A1"])
    new = BrandGuide(voice="новый", spelling=["A1", "B2"])
    merged = old.merged_with(new)
    assert merged.voice == "новый" and merged.spelling == ["A1", "B2"]


async def test_clearing_guide(owner):
    _, business = owner
    brand.set_guide(business, SALAVATSTEKLO)
    brand.set_guide(business, None)
    assert not brand.get_guide(business).is_filled()


def test_legal_names_and_hashtags_are_not_errors():
    text = (
        "АО «Салаватстекло», АО «Саратовстройстекло» и ООО «Салаватстекло Каспий» — одна команда. "
        "#MultiComfort #САЛАВАТСТЕКЛО"
    )
    assert brand.check_text(text, SALAVATSTEKLO) == []


def test_example_posts_pass_brand_check():
    """Эталонные посты из content/salavatsteklo проходят проверку бренд-гида."""
    import re
    from pathlib import Path

    source = Path("content/salavatsteklo/example_posts.md").read_text(encoding="utf-8")
    posts = [p.split("\n---")[0] for p in re.split(r"\n## \d\. ", source)[1:]]
    assert len(posts) >= 5
    for post in posts:
        assert brand.check_text(post, SALAVATSTEKLO) == [], post[:60]
