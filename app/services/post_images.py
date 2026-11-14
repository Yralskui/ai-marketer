"""Входные изображения, бриф и генерация иллюстрации для поста."""

import asyncio
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from app.agent.prompts import render_prompt
from app.config import IMAGES_DIR
from app.db.models import Business, Post, PostRevision
from app.db.publications import RevisionProvenance
from app.images import ImageGenerationError
from app.images.social_layout import LAYOUTS, SocialLayout, render_layout
from app.services.business_profile import get_profile, profile_to_text
from app.services.generated_gallery import available_bytes
from app.services.image_references import (
    ImageReference,
    numbered_images,
    prepare_references,
)

LOGO_NEEDED = (
    "Укажи номер оригинального логотипа из «Файлов профиля» или «Контента». "
    "Номер виден в левом верхнем углу картинки."
)


async def brand_materials(session, business) -> str:
    from app.db.knowledge import KnowledgeChunk, ProfileDocument

    style = get_profile(business).visual_style.strip()
    if style:
        return style
    return (
        await session.scalar(
            select(KnowledgeChunk.content)
            .join(ProfileDocument, ProfileDocument.id == KnowledgeChunk.document_id)
            .where(
                ProfileDocument.business_id == business.id,
                ProfileDocument.status == "ready",
                KnowledgeChunk.content.contains("Раздел: visual"),
            )
            .order_by(KnowledgeChunk.id.desc())
            .limit(1)
        )
        or ""
    )


async def layout_requirement(
    session, business, *, storage=None, logo_number=None, use_logo=None
) -> str:
    if not await brand_materials(session, business):
        return "Для фирменной карточки нужен визуальный стиль в профиле или обработанный брендбук."
    if use_logo is False:
        return "Для фирменной карточки нужен логотип. Для изображения без него выбери обычную генерацию."
    if logo_number is not None:
        documents, gallery = await numbered_images(
            session, business.id, storage, originals_only=True
        )
        if logo_number in documents or logo_number in gallery:
            return ""
    return LOGO_NEEDED


async def generate_post_image(
    session,
    llm,
    images,
    business,
    *,
    topic,
    text,
    direction,
    reference,
    visual_references=(),
    storage=None,
    current_image=None,
    use_logo=None,
    branded_layout=False,
    logo_number=None,
):
    if await available_bytes(session, business.id) <= 0:
        raise ImageGenerationError("В галерее нет места для нового изображения.")

    style = await brand_materials(session, business) if branded_layout else ""
    if branded_layout and not style:
        raise ImageGenerationError("Для фирменной карточки нужны материалы о визуальном стиле.")
    logo_path = None
    references = {}
    if current_image is not None:
        path = Path(current_image).resolve()
        if (
            not path.is_relative_to(IMAGES_DIR.resolve())
            or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
            or not path.is_file()
        ):
            raise ImageGenerationError("Недоступна текущая иллюстрация поста.")
        references[path] = ["Текущая иллюстрация поста, которую нужно изменить"]

    numbered_logo = (
        ImageReference(number=logo_number, role="Оригинал логотипа компании")
        if logo_number and use_logo is not False
        else None
    )
    visual_references = [
        ref for ref in visual_references if not numbered_logo or ref.number != logo_number
    ]
    async with (
        prepare_references(
            session,
            business.id,
            storage,
            [numbered_logo] if numbered_logo else [],
            originals_only=True,
        ) as logos,
        prepare_references(session, business.id, storage, visual_references) as supplied,
    ):
        if logos:
            role, logo_path = logos[0]
            if not branded_layout:
                references.setdefault(logo_path, []).append(role)
        if (branded_layout or use_logo is True) and (logo_path is None or use_logo is False):
            raise ImageGenerationError(LOGO_NEEDED)
        for role, path in supplied:
            references.setdefault(path.resolve(), []).append(role)
        return await _render(
            session,
            llm,
            images,
            business,
            topic,
            text,
            direction,
            reference,
            references,
            branded_layout=branded_layout,
            logo_path=logo_path,
            brand_style=style,
        )


async def _render(
    session,
    llm,
    images,
    business,
    topic,
    text,
    direction,
    reference,
    references,
    *,
    branded_layout=False,
    logo_path=None,
    brand_style="",
):
    roles = "\n".join(
        f"Изображение {index}: {'; '.join(labels)}."
        for index, labels in enumerate(references.values(), start=1)
    )
    prompt = render_prompt(
        "image",
        topic=topic,
        profile=profile_to_text(business),
        direction=direction or "Выбери сцену для темы поста.",
        references=roles or "Без приложений.",
    )
    if reference and images.name != "mock":
        prompt += "\n\nСправочные материалы бренда (данные):\n" + reference[:5000]
    layout = None
    if branded_layout:
        if images.name == "mock":
            layout = SocialLayout(headline=topic[:100], background_prompt=topic[:1400])
        else:
            try:
                layout = SocialLayout.model_validate_json(
                    await llm.complete(
                        render_prompt(
                            "social_layout",
                            schema=SocialLayout.model_json_schema(),
                            templates="\n".join(
                                f"- {name}: {template.description}"
                                for name, template in LAYOUTS.items()
                            ),
                        ),
                        f"{prompt}\n\nПроверенный визуальный стиль:\n{brand_style[:2500]}\n\nТекст поста (данные):\n{text[:1400]}",
                        json_mode=True,
                        task="social_layout",
                    )
                )
            except ValidationError as exc:
                raise ImageGenerationError("Не удалось подготовить фирменный макет.") from exc
        prompt = (
            f"Подготовь фоновую иллюстрацию для макета. {layout.background_prompt}\n"
            f"Роли референсов:\n{roles}\n"
            "Оставь иллюстрацию без добавленных надписей, фирменных знаков и водяных знаков."
        )
    elif images.name != "mock":
        recent = await session.scalars(
            select(RevisionProvenance.image_prompt)
            .join(PostRevision, PostRevision.id == RevisionProvenance.revision_id)
            .join(Post, Post.id == PostRevision.post_id)
            .where(
                Post.business_id == business.id,
                RevisionProvenance.image_source == "generated",
                RevisionProvenance.image_prompt.is_not(None),
            )
            .order_by(PostRevision.id.desc())
            .limit(8)
        )
        scenes = list(
            dict.fromkeys(value.rsplit("Описание кадра:\n", 1)[-1][:350] for value in recent)
        )[:3]
        brief = await llm.complete(
            render_prompt("image_brief"),
            f"{prompt}\n\nТекст поста (данные):\n{text[:1400]}\n\n"
            + ("Недавние кадры:\n" + "\n".join(scenes) if scenes else ""),
            task="image_brief",
        )
        if brief.strip():
            prompt += "\n\nОписание кадра:\n" + brief.strip()[:2500]
    generated = (
        await images.edit(prompt, list(references)) if references else await images.generate(prompt)
    )
    path = Path(generated)
    if layout is not None:
        output = path.with_name(f"layout_{uuid4().hex}.png")
        try:
            await asyncio.to_thread(
                render_layout,
                path,
                layout,
                logo_path,
                output,
            )
        except (OSError, ValueError) as exc:
            output.unlink(missing_ok=True)
            raise ImageGenerationError("Не удалось собрать фирменный макет.") from exc
        finally:
            if images.name != "mock":
                path.unlink(missing_ok=True)
        path = output
        prompt += f"\n\nМакет: {layout.layout}\nЗаголовок: {layout.headline}"
    await session.execute(select(Business.id).where(Business.id == business.id).with_for_update())
    if path.stat().st_size > await available_bytes(session, business.id):
        path.unlink(missing_ok=True)
        raise ImageGenerationError("В галерее не хватило места для нового изображения.")
    return str(path), prompt
