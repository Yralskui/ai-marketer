"""Раздел «Контент»: картинки компании, которые может открыть агент."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from app.agent.tools.post_tools import CreatePostArgs, RevisePostArgs, create_post_draft
from app.container import build_container
from app.db.models import MediaFile, Post, PostStatus
from app.services import content_gallery
from app.web.miniapp import MiniApp
from tests.test_miniapp import BOT_TOKEN, signed_user


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(content_gallery, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(content_gallery, "USED_DIR", tmp_path / "used")


def png(size=(3000, 2000), color="red") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


async def test_only_images_are_accepted_and_fitted_to_full_hd(session, owner):
    _, company = owner
    item = await content_gallery.add_image(session, company, "вывеска.png", png())
    assert item.id.startswith("m") and item.kind == "uploaded" and item.label == "вывеска"
    with Image.open(item.path) as saved:
        assert saved.size == (1620, 1080)  # вписано в 1920×1080 без обрезки
    with pytest.raises(content_gallery.ContentError, match="только изображения"):
        await content_gallery.add_image(session, company, "прайс.pdf", b"%PDF-1.4")
    with pytest.raises(content_gallery.ContentError, match="прочитать"):
        await content_gallery.add_image(session, company, "битый.png", b"not an image")


async def test_delete_removes_file_unless_a_post_uses_it(session, owner):
    _, company = owner
    free = await content_gallery.add_image(session, company, "a.png", png((100, 100)))
    used = await content_gallery.add_image(session, company, "b.png", png((100, 100), "blue"))
    session.add(
        Post(business_id=company.id, platform="telegram", text="т", image_path=str(used.path))
    )
    await session.commit()
    assert await content_gallery.delete_content(session, company.id, free.id)
    assert not free.path.exists()
    assert await content_gallery.delete_content(session, company.id, used.id)
    assert used.path.exists()  # пост ссылается на файл — оставляем
    assert not await content_gallery.delete_content(session, company.id, "m999")
    assert await content_gallery.get_content(session, company.id, free.id) is None


async def test_agent_uses_content_image_by_copy_and_cannot_see_profile_files(agent_ctx, session):
    item = await content_gallery.add_image(
        session, agent_ctx.business, "витрина.png", png((800, 600))
    )
    await session.commit()
    result = await create_post_draft(
        agent_ctx, CreatePostArgs(topic="Новая витрина", content_id=item.id, platform="telegram")
    )
    post = await session.get(Post, result["post_id"])
    assert post.status == PostStatus.PENDING_APPROVAL
    assert post.image_path and post.image_path != str(item.path)  # копия, а не сам файл
    assert Path(post.image_path).exists()
    await content_gallery.delete_content(session, agent_ctx.business_id, item.id)
    assert Path(post.image_path).exists()  # готовый пост не ломается от удаления

    missing = await create_post_draft(
        agent_ctx, CreatePostArgs(topic="Тема", content_id="m9999", platform="telegram")
    )
    assert "Контент" in missing["error"]
    # файлов профиля у агента нет: ни в схеме создания поста, ни в схеме правки
    for schema in (CreatePostArgs, RevisePostArgs):
        assert "profile_file_id" not in schema.model_json_schema()["properties"]
        assert "content_id" in schema.model_json_schema()["properties"]


async def test_miniapp_content_upload_preview_and_delete(settings, db):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)

    async with TestClient(TestServer(app)) as client:
        owner = {"Authorization": "Telegram " + signed_user(301)}
        stranger = {"Authorization": "Telegram " + signed_user(302)}
        api = "/miniapp/api/"
        async with client.post(api + "profiles", headers=owner, json={"name": "Студия"}) as r:
            company = (await r.json())["id"]
        headers = {**owner, "Content-Type": "application/octet-stream", "X-Filename": "photo.png"}
        async with client.put(
            api + f"content?company={company}", headers=headers, data=png((400, 300))
        ) as response:
            assert response.status == 200, await response.text()
            image_id = (await response.json())["id"]
        async with client.get(api + "state", headers=owner) as response:
            state = await response.json()
            assert [item["id"] for item in state["content"]] == [image_id]
            assert state["content"][0]["kind"] == "uploaded"
        async with client.get(
            api + "content", headers=owner, params={"company": company, "id": image_id}
        ) as response:
            assert response.status == 200 and response.content_type == "image/jpeg"
        async with client.get(
            api + "content", headers=stranger, params={"company": company, "id": image_id}
        ) as response:
            assert response.status == 404
        pdf_headers = {**headers, "X-Filename": "doc.pdf"}
        async with client.put(
            api + f"content?company={company}", headers=pdf_headers, data=b"%PDF-1.4 x"
        ) as response:
            assert response.status in {415, 422}
        async with client.delete(
            api + "content", headers=owner, json={"company": company, "id": image_id}
        ) as response:
            assert response.status == 200
        async with client.get(api + "state", headers=owner) as response:
            assert (await response.json())["content"] == []
    assert not (await _media_rows(db))


async def _media_rows(db):
    from sqlalchemy import select

    async with db.sessionmaker() as session:
        return list(await session.scalars(select(MediaFile)))
