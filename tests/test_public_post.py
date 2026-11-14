"""Публичная страница поста и баннер: из них ВКонтакте собирает карточку с картинкой."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from app.config import IMAGES_DIR
from app.container import build_container
from app.db.models import Platform, Post, PostStatus
from app.services import public_posts
from app.web.public_post import PublicPosts
from tests.test_miniapp import BOT_TOKEN

SECRET = "test-secret"


def test_token_is_signed():
    token = public_posts.make_token(SECRET, 42)
    assert public_posts.read_token(SECRET, token) == 42
    assert public_posts.read_token("other-secret", token) is None
    assert public_posts.read_token(SECRET, "42.deadbeef") is None
    assert public_posts.read_token(SECRET, "43." + token.split(".")[1]) is None
    for junk in ("", ".", "abc.def", "42", "9" * 30 + ".x"):
        assert public_posts.read_token(SECRET, junk) is None


def test_split_text_skips_hashtags():
    title, description = public_posts.split_text("Заголовок\n\nПервый абзац.\n\n#тег #ещё")
    assert title == "Заголовок" and description == "Первый абзац."
    assert public_posts.split_text("")[0] == "Пост"


def test_banner_keeps_the_whole_picture(tmp_path: Path):
    source = tmp_path / "card.png"
    Image.new("RGB", (1000, 1000), (25, 108, 127)).save(source)
    result = public_posts.banner(source, tmp_path / "og" / "card.jpg")
    with Image.open(result) as image:
        assert image.size == public_posts.OG_SIZE
        assert abs(image.getpixel((5, 5))[0] - 25) <= 4  # фон — цвет самой картинки


@pytest.fixture
async def client(settings, db):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_public_url = "https://nexussub.test"
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    PublicPosts(container).register(app)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def _post(db, owner, *, text, image: Path | None):
    _, company = owner
    async with db.sessionmaker() as session:
        item = Post(
            business_id=company.id,
            platform=Platform.VK,
            text=text,
            status=PostStatus.PUBLISHED,
            image_path=str(image) if image else None,
        )
        session.add(item)
        await session.commit()
        return item.id


async def test_page_has_open_graph_tags_and_escapes_text(client, db, owner):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    source = IMAGES_DIR / "test_public_post.png"
    Image.new("RGB", (64, 64), (25, 108, 127)).save(source)
    post_id = await _post(
        db, owner, text="Заголовок <script>alert(1)</script>\n\nОписание поста", image=source
    )
    banner_file = IMAGES_DIR / "og" / f"{post_id}.jpg"
    try:
        token = public_posts.make_token(BOT_TOKEN, post_id)
        response = await client.get(f"/p/{token}")
        html = await response.text()
        assert response.status == 200 and "noindex" in response.headers["X-Robots-Tag"]
        assert '<meta property="og:title" content="Заголовок &lt;script&gt;' in html
        assert "<script>alert" not in html
        assert f'content="https://nexussub.test/p/{token}/card.jpg"' in html
        assert 'property="og:image:width" content="1200"' in html

        image = await client.get(f"/p/{token}/card.jpg")
        assert image.status == 200 and image.content_type == "image/jpeg"
        with Image.open(io.BytesIO(await image.read())) as banner:
            assert banner.size == (1200, 630)
    finally:
        source.unlink(missing_ok=True)
        banner_file.unlink(missing_ok=True)


async def test_page_without_image_has_no_image_tags(client, db, owner):
    post_id = await _post(db, owner, text="Только текст", image=None)
    token = public_posts.make_token(BOT_TOKEN, post_id)
    html = await (await client.get(f"/p/{token}")).text()
    assert "og:image" not in html
    assert (await client.get(f"/p/{token}/card.jpg")).status == 404


async def test_foreign_or_forged_links_are_not_found(client, db, owner):
    post_id = await _post(db, owner, text="Секрет", image=None)
    forged = f"{post_id}.{'0' * 24}"
    assert (await client.get(f"/p/{forged}")).status == 404
    assert (await client.get("/p/999.abc")).status == 404
    missing = public_posts.make_token(BOT_TOKEN, 99999)
    assert (await client.get(f"/p/{missing}")).status == 404


async def test_files_outside_the_images_folder_are_not_served(client, db, owner, tmp_path):
    outside = tmp_path / "secret.png"
    Image.new("RGB", (8, 8)).save(outside)
    post_id = await _post(db, owner, text="Чужой файл", image=outside)
    token = public_posts.make_token(BOT_TOKEN, post_id)
    assert (await client.get(f"/p/{token}/card.jpg")).status == 404
    assert "og:image" not in await (await client.get(f"/p/{token}")).text()


async def test_no_routes_without_a_secret(settings, db):
    settings.bot_token = ""
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    PublicPosts(container).register(app)
    async with TestClient(TestServer(app)) as test_client:
        assert (await test_client.get("/p/1.abc")).status == 404
