"""Автономные функции без нейросети: поводы, темы, контент-план, фирменные картинки, карусель."""

from datetime import date
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from sqlalchemy import select

from app.container import build_container
from app.db.models import Business, Post, PostStatus
from app.images.brand_card import DEFAULT_PALETTE, palette_from_text
from app.llm.mock_provider import MockLLMProvider
from app.llm.post_templates import next_topics, render_post
from app.scheduler.jobs import prepare_occasion_drafts
from app.services import carousel, content_plan, occasions
from app.services.brand import check_text
from app.services.brand_presets import SALAVATSTEKLO as GUIDE
from app.web.miniapp import MiniApp
from tests.test_miniapp import BOT_TOKEN, signed_user


def _salavat() -> Business:
    return Business(id=1, name="САЛАВАТСТЕКЛО", settings={"preset": "salavatsteklo"})


def test_upcoming_and_due_occasions():
    business = _salavat()
    soon = occasions.upcoming(business, date(2026, 11, 14))
    assert soon[0][0].key == "newyear" and soon[0][1] == date(2026, 12, 31)
    assert [o.key for o, _ in occasions.due(business, date(2026, 6, 27))] == ["salavat"]
    assert occasions.due(business, date(2026, 6, 20)) == []
    # День строителя — второе воскресенье августа
    builder = dict((o.key, d) for o, d in occasions.upcoming(business, date(2026, 8, 1), 5))
    assert builder["builder"] == date(2026, 8, 9)


def test_occasion_is_prepared_once_per_year():
    business = _salavat()
    ((occasion, when),) = occasions.due(business, date(2026, 10, 28))
    occasions.mark_done(business, occasion, when)
    assert occasions.due(business, date(2026, 10, 29)) == []
    assert [o.key for o, _ in occasions.due(business, date(2027, 10, 28))] == ["caspian"]


def test_other_companies_have_no_calendar():
    assert occasions.upcoming(Business(id=2, name="Кофейня", settings={}), date(2026, 1, 1)) == []


def test_occasion_posts_use_dedicated_templates():
    for occasion in occasions.SALAVATSTEKLO_OCCASIONS:
        post = render_post(occasion.topic, "САЛАВАТСТЕКЛО")
        assert check_text(post, GUIDE) == [], occasion.key
    assert render_post(
        "День рождения АО «Салаватстекло» — 30 июня 1962 года", "САЛАВАТСТЕКЛО"
    ).startswith("30 июня")
    assert render_post("Поздравление с Днём строителя", "САЛАВАТСТЕКЛО").startswith(
        "С Днём строителя"
    )
    # Обычная тема про историю не превращается в поздравление с днём рождения
    assert render_post("История компании: с 1958 года до наших дней", "САЛАВАТСТЕКЛО").startswith(
        "Сила традиций"
    )


def test_topics_skip_recent_and_stay_unique():
    context = "Профиль САЛАВАТСТЕКЛО. Недавние темы: Экологичность продукции"
    topics = [topic for topic, _ in next_topics(context, 12)]
    assert len(set(topics)) == 12
    assert topics[-1] == "Экологичность продукции"  # бывшая тема — в самом конце
    assert all("Экологичность" not in t for t in topics[:11])
    for topic in topics:
        assert check_text(render_post(topic, "САЛАВАТСТЕКЛО"), GUIDE) == []


async def test_mock_suggests_topic_and_builds_plan(session, owner):
    llm = MockLLMProvider()
    topic = await llm.complete(
        "Профиль: САЛАВАТСТЕКЛО\nНедавние темы:\n", "Предложи тему.", task="suggest_topic"
    )
    assert "mock" not in topic.lower() and len(topic) > 5
    _, business = owner
    plan = await content_plan.generate_plan(
        session, llm, business, period="month", tz=None or __import__("zoneinfo").ZoneInfo("UTC")
    )
    assert len(plan.items) == 12


def test_brand_palette_from_style():
    palette = palette_from_text("цвета: Petrol #196C7F (основной), #285055, #C1D0D3, #DDE1E1")
    assert palette[0] == (25, 108, 127)
    assert palette_from_text("без цветов") == DEFAULT_PALETTE


async def test_mock_image_is_brand_card(images):
    path = await images.generate(
        "Фасады — САЛАВАТСТЕКЛО\nцвета: #196C7F, #285055, #C1D0D3, #DDE1E1"
    )
    with Image.open(path) as image:
        assert image.size == (1024, 1024)
        assert all(
            abs(a - b) <= 3 for a, b in zip(image.getpixel((5, 5))[:3], (25, 108, 127), strict=True)
        )  # фон — фирменный Petrol


async def test_carousel_slides(tmp_path):
    text = (
        "Свет в доме начинается со стекла.\n\nНаша миссия — делать мир светлее. " * 1
        + "\n\nПервый абзац про продукт.\n\nВторой абзац про партнёров.\n\n#САЛАВАТСТЕКЛО #стекло"
    )
    title, slides = carousel.split_slides(text)
    assert title.startswith("Свет в доме") and all("#" not in s for s in slides)
    business = SimpleNamespace(
        id=1, name="САЛАВАТСТЕКЛО", settings={"brand_guide": GUIDE.model_dump()}
    )
    paths = await carousel.build(business, SimpleNamespace(id=7, text=text), tmp_path)
    assert len(paths) == len(slides) + 1 and all(p.exists() for p in paths)


async def test_occasion_job_creates_draft_once(settings, db):
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    async with db.sessionmaker() as session:
        from app.db.repositories import BusinessRepo, UserRepo

        user = await UserRepo(session).get_or_create(4242, "Owner")
        business = await BusinessRepo(session).create(user.id, "САЛАВАТСТЕКЛО")
        business.settings = {"preset": "salavatsteklo"}
        await session.commit()
        business_id = business.id
    assert await prepare_occasion_drafts(container, today=date(2026, 6, 28)) == 1
    assert await prepare_occasion_drafts(container, today=date(2026, 6, 29)) == 0
    async with db.sessionmaker() as session:
        (post,) = (await session.scalars(select(Post).where(Post.business_id == business_id))).all()
        assert post.status == PostStatus.PENDING_APPROVAL
        assert post.text.startswith("30 июня") and post.image_path


async def test_miniapp_occasions_and_carousel(settings, db):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)
    headers = {"Authorization": "Telegram " + signed_user(503)}
    async with TestClient(TestServer(app)) as client:
        state = await (await client.get("/miniapp/api/state", headers=headers)).json()
        assert state["occasions"] and state["occasions"][0]["title"]
        key = state["occasions"][0]["key"]
        response = await client.post(
            "/miniapp/api/occasion", headers=headers, json={"company": state["active"], "key": key}
        )
        assert response.status == 200
        post_id = (await response.json())["id"]
        response = await client.get(f"/miniapp/api/carousel?id={post_id}", headers=headers)
        assert response.status == 200
        assert len((await response.json())["slides"]) >= 2
        # Без запущенного бота отправить карусель в чат нельзя — понятная ошибка
        response = await client.post(
            "/miniapp/api/carousel",
            headers=headers,
            json={"company": state["active"], "id": post_id},
        )
        assert response.status == 400
