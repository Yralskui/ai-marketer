"""Цикл агента с mock-LLM: сообщение -> инструмент -> черновик на согласовании."""

from __future__ import annotations

import pytest

import app.agent.tools  # noqa: F401  — регистрация инструментов
from app.agent.core import Agent
from app.agent.harness import answer
from app.db.models import ContentPlanStatus, MessageRole, PostStatus
from app.db.repositories import AgentMessageRepo, PostRepo


@pytest.fixture
def agent() -> Agent:
    return Agent(max_steps=6, history_limit=20)


async def test_message_creates_pending_draft(agent, agent_ctx, notifier, session):
    result = await agent.run(agent_ctx, "Напиши пост про наше новое осеннее меню")

    assert result.tool_calls == ["create_post_draft"]
    assert len(result.created_post_ids) == 1
    post = await PostRepo(session).get(result.created_post_ids[0])
    assert post.status == PostStatus.PENDING_APPROVAL
    assert "осеннее меню" in post.text
    assert post.image_path is None
    assert notifier.drafts == [(agent_ctx.user.telegram_id, post.id)]
    assert "согласование" in result.text


async def test_image_requested(agent, agent_ctx, session):
    result = await agent.run(agent_ctx, "Подготовь пост про скидки выходного дня с картинкой")
    post = await PostRepo(session).get(result.created_post_ids[0])
    assert post.image_path and post.image_path.endswith(".png")
    assert len(post.text) <= 1024  # укладываемся в подпись к фото


async def test_revise_via_agent(agent, agent_ctx, session):
    first = await agent.run(agent_ctx, "Напиши пост про новый десерт")
    post_id = first.created_post_ids[0]
    before = (await PostRepo(session).get(post_id)).text

    second = await agent.run(agent_ctx, f"Сделай пост #{post_id} короче")
    assert second.tool_calls == ["revise_post"]
    post = await PostRepo(session).get(post_id)
    assert post.text != before
    assert post.status == PostStatus.PENDING_APPROVAL
    assert len(await PostRepo(session).list_revisions(post_id)) == 2


async def test_agent_creates_weekly_plan(agent, agent_ctx):
    from app.services.content_plan import recent_plans

    result = await agent.run(agent_ctx, "Составь контент-план на неделю")
    assert result.tool_calls == ["create_content_plan"]
    assert "Утвердите" in result.text
    (plan,) = await recent_plans(agent_ctx.session, agent_ctx.business_id)
    assert plan.status == ContentPlanStatus.DRAFT
    assert len(plan.items) == 5
    assert len({item.planned_at.date() for item in plan.items}) == 5


async def test_platform_help_uses_product_guide_without_running_agent(agent_ctx):
    calls = []

    class HelpModel:
        name = "openai"

        async def complete(self, prompt, text, *, task, json_mode=False):
            calls.append(task)
            if task == "classify_intent":
                assert json_mode
                return '{"intent":"help"}'
            assert task == "platform_help"
            assert "Утвердить план" in text
            assert "Тестовый Владелец" not in prompt
            return "План можно утвердить на главной."

    agent_ctx.llm = HelpModel()
    result = await answer(agent_ctx, "Как утвердить план?")

    assert result == {"text": "План можно утвердить на главной."}
    assert calls == ["classify_intent", "platform_help"]


async def test_harness_suggests_schedule_without_approving(agent_ctx, session):
    class ScenarioModel:
        async def complete(self, prompt, text, *, task, json_mode=False):
            return {
                "classify_intent": '{"intent":"create_post"}',
                "args_create_post": '{"topic":"Витрины","when":"завтра в 10:00"}',
                "write_post": "Текст про витрины",
            }[task]

    agent_ctx.llm = ScenarioModel()
    result = await answer(agent_ctx, "Подготовь пост про витрины на завтра в 10:00")
    proposal = result["proposal"]
    post = await PostRepo(session).get(proposal["id"])
    assert proposal["action"] == "schedule"
    assert proposal["company"] == agent_ctx.business_id
    assert post.status == PostStatus.PENDING_APPROVAL
    assert post.scheduled_at is None


async def test_draft_brief_asks_then_builds_visual_for_the_selected_idea(agent_ctx, session):
    class Model:
        async def complete(self, prompt, text, *, task, json_mode=False):
            if task == "classify_intent":
                return '{"intent":"create_post"}'
            if task == "args_create_post":
                if text.endswith("первый вариант"):
                    assert "собаках и попугаях" in text
                    return (
                        '{"topic":"Пост о соседстве собак и попугаев", "with_image":true,'
                        '"image_direction":"Собака наблюдает за попугаем через стекло",'
                        '"style":"anti_llm"}'
                    )
                return (
                    '{"clarification":"Показать соседство питомцев или рассказать про уход?",'
                    '"suggestion":"Для первого варианта подойдёт общий кадр двух животных."}'
                )
            assert task == "write_post"
            assert "мнимые возражения" in prompt
            return "Собака и попугай в одном доме."

    agent_ctx.llm = Model()
    asked = await answer(agent_ctx, "Напиши пост о собаках и попугаях")
    assert "соседство питомцев" in asked["text"]
    assert "общий кадр" in asked["text"]
    assert not agent_ctx.created_post_ids

    result = await answer(agent_ctx, "первый вариант")
    post = await PostRepo(session).get(result["draft_id"])
    assert post.image_path
    assert post.status == PostStatus.PENDING_APPROVAL


@pytest.mark.parametrize("search_status", ["ready", "empty", "error", "pending"])
async def test_web_context_is_optional_and_does_not_delay_writing(agent_ctx, db, search_status):
    import asyncio

    from app.agent.harness import choose_draft_option
    from app.llm.base import LLMError

    started, finished, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    class Model:
        async def complete(self, prompt, text, *, task, json_mode=False):
            calls.append(task)
            if task == "classify_intent":
                return '{"intent":"create_post"}'
            if task == "args_create_post":
                return '{"topic":"Новый закон о рекламе","avoid_image":true}'
            if task == "web_brief":
                started.set()
                if search_status == "pending":
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.set()
                if search_status == "error":
                    finished.set()
                    raise LLMError("Поиск недоступен")
                finished.set()
                if search_status == "empty":
                    return '{"summary":"NO_EVIDENCE","sources":[]}'
                return '{"summary":"Подтверждённый факт","sources":[["https://example.org","Источник"]]}'
            assert task == "write_post"
            assert ("https://example.org" in text) == (search_status == "ready")
            assert "Официально-деловой" in prompt
            return "Готовый текст на основе доступных данных."

    agent_ctx.llm = agent_ctx.research_llm = Model()
    agent_ctx.session_factory = db.sessionmaker
    agent_ctx.interactive_drafts = True
    first = await answer(agent_ctx, "Подготовь пост про новый закон о рекламе, без фото.")
    await asyncio.wait_for(started.wait(), 1)
    if search_status != "pending":
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0.05)
    result = await asyncio.wait_for(
        choose_draft_option(agent_ctx, first["draft_options"]["token"], "official"), 1
    )
    assert result["draft_id"]
    assert calls.count("web_brief") == 1
    if search_status == "pending":
        await asyncio.wait_for(cancelled.wait(), 1)


@pytest.mark.parametrize(
    "brief",
    [
        {"topic": None, "clarification": "Уточните тему нового поста.", "when": None},
        {
            "topic": "Новая работа: поставляем энергосберегающие стёкла в офис Сбербанка",
            "research_question": "Найдите публичное подтверждение поставки в офис Сбербанка",
        },
        {
            "topic": "Новости компании: новая поставка стёкол для офиса клиента",
            "research_question": "Проверь, были ли новости об этой поставке",
        },
    ],
)
async def test_generic_draft_uses_given_topic_without_model_triggered_search(
    agent_ctx, session, db, brief
):
    import json

    calls = []

    class Model:
        async def complete(self, prompt, text, *, task, json_mode=False):
            calls.append(task)
            if task == "classify_intent":
                return '{"intent":"create_post"}'
            if task == "args_create_post":
                return json.dumps(brief)
            assert task == "write_post"
            assert "офис" in text
            assert "Веб-справка" not in text
            return "Поставляем стёкла для офиса."

    agent_ctx.research_llm = agent_ctx.llm = Model()
    agent_ctx.session_factory = db.sessionmaker
    result = await answer(
        agent_ctx,
        "Сделай пост на тему новой работы с подрядчиком Сбербанка, мы ставим им стёкла в офис",
    )
    assert result["draft_id"]
    assert calls == ["classify_intent", "args_create_post", "write_post"]
    assert (await PostRepo(session).get(result["draft_id"])).status == PostStatus.PENDING_APPROVAL


async def test_missing_fact_is_explained_in_chat_and_is_not_saved_as_post(agent_ctx):
    class Model:
        async def complete(self, prompt, text, *, task, json_mode=False):
            return {
                "classify_intent": '{"intent":"create_post"}',
                "args_create_post": '{"topic":"Объявление о переносе встречи"}',
                "write_post": "MISSING_INFO: Укажите новую дату встречи.",
            }[task]

    agent_ctx.llm = Model()
    result = await answer(agent_ctx, "Объяви о переносе встречи.")
    assert result == {"text": "Для поста не хватает данных. Укажите новую дату встречи."}
    assert not agent_ctx.created_post_ids


async def test_explicit_draft_is_not_replaced_by_web_search(agent_ctx):
    class Model:
        async def complete(self, prompt, text, *, task, json_mode=False):
            return {
                "classify_intent": '{"intent":"web_search"}',
                "args_create_post": '{"topic":null,"clarification":"Уточните тему."}',
                "write_post": "Море особенно красиво на рассвете.",
            }[task]

    agent_ctx.llm = agent_ctx.research_llm = Model()
    result = await answer(agent_ctx, "Напиши пост про море.")
    assert result["draft_id"]


async def test_malformed_brief_does_not_ask_for_topic_again(agent_ctx):
    from app.llm.base import LLMError

    class Model:
        async def complete(self, prompt, text, *, task, json_mode=False):
            return (
                '{"intent":"create_post"}'
                if task == "classify_intent"
                else '{"platform":"instagram"}'
            )

    agent_ctx.llm = Model()
    with pytest.raises(LLMError, match="параметры поста"):
        await answer(agent_ctx, "Сделай пост о поставке стёкол для нового офиса")


async def test_history_is_saved_and_windowed(agent, agent_ctx, session):
    await agent.run(agent_ctx, "Привет!")
    await agent.run(agent_ctx, "Напиши пост про акцию")
    history = await AgentMessageRepo(session).last_n(agent_ctx.user_id, 50)
    roles = [m.role for m in history]
    # привет -> ответ; запрос -> tool_call -> tool -> ответ
    assert roles == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    from app.agent.core import _history_to_messages

    # Окно, обрезанное посреди цепочки, начинается с сообщения пользователя
    window = _history_to_messages(history[-3:])
    assert window == []
    window = _history_to_messages(history[-4:])
    assert window[0]["role"] == "user"


@pytest.mark.parametrize("image_fails", [False, True])
async def test_published_photo_edit_keeps_context_and_requires_approval(
    agent_ctx, session, tmp_path, monkeypatch, image_fails
):
    import json
    from unittest.mock import AsyncMock

    from PIL import Image

    from app.db.models import MediaFile, Platform, Post
    from app.images import ImageGenerationError
    from app.services import content_gallery, image_references, post_images

    agent_ctx.interactive_drafts = True
    monkeypatch.setattr(image_references, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(post_images, "IMAGES_DIR", tmp_path)
    original, logo, rendered = [tmp_path / f"{name}.png" for name in ("office", "logo", "edit")]
    for path in (original, logo, rendered):
        Image.new("RGB", (16, 16), "blue").save(path)
    post = await PostRepo(session).add(
        Post(
            business_id=agent_ctx.business_id,
            platform=Platform.TELEGRAM,
            topic="Стекло для офиса",
            text="Текст опубликованного поста",
            status=PostStatus.PUBLISHED,
            image_path=str(original),
        )
    )
    logo_media = MediaFile(business_id=agent_ctx.business_id, path=str(logo))
    session.add(logo_media)
    await session.flush()
    number = content_gallery.ContentImage(
        f"m{logo_media.id}",
        "uploaded",
        logo,
        logo.stat().st_size,
        "Логотип",
    ).number
    await AgentMessageRepo(session).add(
        agent_ctx.user_id,
        MessageRole.ASSISTANT,
        "Черновик готов",
        business_id=agent_ctx.business_id,
        name=f"draft:{post.id}",
    )
    await session.commit()
    original_id = post.id
    calls = []

    async def complete(prompt, text, *, task, json_mode=False):
        calls.append(task)
        if task == "classify_intent":
            return '{"intent":"revise_post"}'
        assert task == "args_revise_post"
        assert f"Карточка поста №{original_id}" in text
        return json.dumps(
            {
                "image_direction": "Добавь на стекло логотип и надпись «Салават Стекло»",
                "edit_current_image": True,
                "use_logo": True,
                "keep_text": True,
                "clarification": "Укажите номер исходной картинки",
            }
        )

    async def edit(prompt, references):
        assert references == [original, logo]
        assert "Салават Стекло" in prompt
        if image_fails:
            raise ImageGenerationError("Генератор недоступен")
        return rendered

    monkeypatch.setattr(agent_ctx.llm, "complete", complete)
    edit_mock = AsyncMock(side_effect=edit)
    monkeypatch.setattr(agent_ctx.images, "edit", edit_mock)
    agent_ctx.images.generate = AsyncMock()

    question = await answer(
        agent_ctx,
        "а можешь на этой же картинке сгенерировать на стекле логотип наш и надпись «Салават Стекло»",
    )
    assert "Создать новый черновик" in question["text"]
    picker = await answer(agent_ctx, "давай")
    assert "Выберите оригинал логотипа" in picker["text"]
    assert f"№{number}" in picker["text"]
    edit_mock.assert_not_awaited()
    result = await answer(agent_ctx, str(number))

    assert calls == ["classify_intent", "args_revise_post"]
    edit_mock.assert_awaited_once()
    agent_ctx.images.generate.assert_not_awaited()
    source = await PostRepo(session).get(original_id)
    assert source.status == PostStatus.PUBLISHED
    assert source.image_path == str(original)
    if image_fails:
        assert result == {"text": "Генератор недоступен"}
        counts = await PostRepo(session).count_by_status(agent_ctx.business_id)
        assert counts[PostStatus.PUBLISHED] == sum(counts.values()) == 1
    else:
        draft = await PostRepo(session).get(result["draft_id"])
        assert draft.id != source.id
        assert draft.status == PostStatus.PENDING_APPROVAL
        assert draft.image_path == str(rendered)
        assert draft.text == source.text


@pytest.mark.parametrize(
    ("request_text", "confirm", "expected_calls"),
    [
        ("можешь копию этого поста опять предложить отправить", "давай", []),
        ("сделай копию этого поста как есть", "ничего не меняй", []),
        ("сделай копию этого поста", "в официальном стиле", ["revise_post"]),
        ("сделай копию этого поста в стиле анти-LLM", "да", ["args_revise_post", "revise_post"]),
        ("сделай копию этого поста и замени заголовок", "да", ["args_revise_post", "revise_post"]),
    ],
)
async def test_copy_post_with_optional_revision(
    agent_ctx, session, request_text, confirm, expected_calls
):
    from app.db.models import Platform, Post
    from app.db.publications import PostAttachments
    from app.services import posts

    agent_ctx.interactive_drafts = True
    original = await PostRepo(session).add(
        Post(
            business_id=agent_ctx.business_id,
            platform=Platform.TELEGRAM,
            topic="Стекло",
            text="«Исходный текст»",
            status=PostStatus.PUBLISHED,
        )
    )
    album = [{"type": "image", "path": "photo.png", "name": "Фото"}]
    session.add(PostAttachments(post_id=original.id, items=album))
    await AgentMessageRepo(session).add(
        agent_ctx.user_id,
        MessageRole.ASSISTANT,
        "Пост опубликован",
        business_id=agent_ctx.business_id,
        name=f"draft:{original.id}",
    )
    calls = []

    class Model:
        async def complete(self, prompt, text, *, task, json_mode=False):
            calls.append(task)
            if task == "args_revise_post":
                if "анти-LLM" in text:
                    return '{"style":"anti_llm","keep_text":true}'
                return '{"instruction":"Замени заголовок"}'
            assert task == "revise_post"
            assert original.text in text
            if "официальном" in confirm:
                assert "Официально-деловой" in prompt
            if "анти-LLM" in request_text:
                assert "мнимые возражения" in prompt
            return "Новый текст"

    agent_ctx.llm = Model()
    asked = await answer(agent_ctx, request_text)
    assert asked["draft_options"]["stage"] == "copy_draft"
    result = await answer(agent_ctx, confirm)
    draft = await PostRepo(session).get(result["draft_id"])
    assert original.status == PostStatus.PUBLISHED
    assert original.text == "«Исходный текст»"
    assert draft.status == PostStatus.PENDING_APPROVAL
    assert draft.text == ("Новый текст" if expected_calls else original.text)
    assert (await session.get(PostAttachments, draft.id)).items == album
    if "официальном" in confirm:
        assert await posts.post_style(session, draft.id) == posts.PostStyle.OFFICIAL
    assert calls == expected_calls
