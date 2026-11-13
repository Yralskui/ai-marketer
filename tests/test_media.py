"""Фотобанк: свои фото клиента в постах."""

from __future__ import annotations

import pytest

from app.db.models import PostStatus
from app.services import accounts, approval, media, posts
from app.services.media import MediaError

JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"test"


async def test_photo_is_saved_once(session, owner, tmp_path):
    _, business = owner
    first, created = await media.save_photo(
        session, business, JPEG, caption="зал", unique_id="abc", root=tmp_path
    )
    assert created and first.path.endswith("abc.jpg")
    again, created = await media.save_photo(session, business, JPEG, unique_id="abc", root=tmp_path)
    assert not created and again.id == first.id


async def test_empty_or_huge_photo_rejected(session, owner, tmp_path):
    _, business = owner
    with pytest.raises(MediaError):
        await media.save_photo(session, business, b"", root=tmp_path)
    with pytest.raises(MediaError):
        await media.save_photo(session, business, b"x" * (media.MAX_PHOTO_BYTES + 1), root=tmp_path)


async def test_post_uses_own_photo_without_generation(session, owner, llm, images, tmp_path):
    _, business = owner
    photo, _ = await media.save_photo(
        session, business, JPEG, caption="тыквенный латте", root=tmp_path
    )

    class NoImages(type(images)):
        async def generate(self, prompt, *, size=(1024, 1024)):
            raise AssertionError("картинку не должны генерировать")

    post = await media.create_post_with_photo(
        session, llm, NoImages(images.output_dir), business, photo, ""
    )
    assert post.image_path == photo.path
    assert "тыквенный латте" in post.topic
    assert len(post.text) <= 1024  # уложились в подпись к фото
    assert photo.used_count == 1


async def test_replacing_photo_requires_new_approval(session, owner, llm, images, tmp_path):
    user, business = owner
    post = await posts.create_draft(session, llm, images, business, "акция", with_image=True)
    await approval.submit_for_approval(session, post, None, None)
    await approval.approve(session, post, user)
    assert post.status == PostStatus.APPROVED

    photo, _ = await media.save_photo(session, business, JPEG, root=tmp_path)
    await media.replace_post_photo(session, post, photo)
    assert post.image_path == photo.path
    assert post.status == PostStatus.DRAFT  # снова на согласование


async def test_photo_of_other_company_is_hidden(session, owner, llm, images, tmp_path):
    admin, company_a = owner
    company_b = await accounts.create_company(session, admin, "Вторая")
    photo_a, _ = await media.save_photo(session, company_a, JPEG, root=tmp_path)
    with pytest.raises(MediaError):
        await media.get_photo(session, company_b, photo_a.id)

    post_b = await posts.create_draft(session, llm, images, company_b, "тема")
    with pytest.raises(MediaError):
        await media.replace_post_photo(session, post_b, photo_a)
