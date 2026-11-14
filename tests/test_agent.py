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


@pytest.mark.parametrize(
    ("style", "topic", "rule"),
    [
        ("scientific", "Свет в помещении", "Различай наблюдения"),
        ("official", "Новый закон РФ о рекламе", "Официально-деловой"),
        ("social", "Новости рынка", "Эмодзи, символы и хэштеги допустимы"),
        ("neutral", "Исследование теплоизоляции", ""),
    ],
)
async def test_draft_checks_external_facts_before_writing(agent_ctx, style, topic, rule):
    import json

    calls = []

    class Model:
        async def complete(self, prompt, text, *, task, json_mode=False):
            calls.append(task)
            if task == "classify_intent":
                return '{"intent":"create_post"}'
            if task == "args_create_post":
                return json.dumps({"topic": topic, "style": style})
            if task == "web_brief":
                assert topic in text
                return '{"summary":"Подтверждённый факт", "sources":[["https://example.org","Источник"]]}'
            assert task == "write_post"
            assert "Подтверждённый факт" in text
            assert "https://example.org" in text
            if rule:
                assert rule in prompt
            else:
                assert "## Стиль" not in prompt
            return "Проверенный текст"

    agent_ctx.research_llm = agent_ctx.llm = Model()
    result = await answer(agent_ctx, f"Напиши пост — {topic}")
    assert result["draft_id"]
    assert calls == ["classify_intent", "args_create_post", "web_brief", "write_post"]


async def test_unverified_post_is_not_written(agent_ctx):
    class Model:
        async def complete(self, prompt, text, *, task, json_mode=False):
            return {
                "classify_intent": '{"intent":"create_post"}',
                "args_create_post": '{"topic":"Новый закон о рекламе"}',
                "web_brief": '{"summary":"NO_EVIDENCE", "sources":[["https://example.org","Обзор"]]}',
            }[task]

    agent_ctx.research_llm = agent_ctx.llm = Model()
    result = await answer(agent_ctx, "Напиши пост о новом законе о рекламе")
    assert "Не нашёл подтверждения" in result["text"]
    assert not agent_ctx.created_post_ids


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
