"""Проверки маршрутизации, контекста и числа вызовов без API."""

import pytest

from app.agent.harness import answer
from app.db.models import MessageRole
from app.db.repositories import AgentMessageRepo, PostRepo


@pytest.mark.parametrize(
    ("question", "intent", "task", "expected", "search_count"),
    [
        ("Как утвердить план?", "help", "platform_help", "Утвердить план", 0),
        ("Есть ли ВКонтакте?", "integration", "platform_help", "ВКонтакте", 0),
        ("Какой цвет у наших витрин?", "company", "company", "контекст из индекса", 1),
    ],
)
async def test_route_has_minimal_context(
    agent_ctx, monkeypatch, question, intent, task, expected, search_count
):
    calls, searches = [], []

    async def retrieve(session, settings, business_id, query):
        searches.append((business_id, query))
        return "контекст из индекса"

    class Model:
        async def complete(self, system, user, *, task, json_mode=False):
            calls.append((task, system, user))
            if task == "classify_intent":
                assert json_mode
                assert "контекст из индекса" not in system + user
                return '{"intent":"' + intent + '"}'
            return "Ответ"

    monkeypatch.setattr("app.agent.harness.retrieve", retrieve)
    agent_ctx.llm = Model()
    repo = AgentMessageRepo(agent_ctx.session)
    await repo.add(agent_ctx.user_id, MessageRole.USER, "чужой контекст", business_id=None)
    assert await answer(agent_ctx, question) == {"text": "Ответ"}

    assert [call[0] for call in calls] == ["classify_intent", task]
    assert expected in calls[-1][1] + calls[-1][2]
    assert all("чужой контекст" not in system + user for _, system, user in calls)
    assert searches == ([(agent_ctx.business_id, question)] if search_count else [])


async def test_classifier_prefix_is_stable_and_reset_excludes_history(agent_ctx):
    prompts = []

    class Model:
        async def complete(self, system, user, *, task, json_mode=False):
            if task == "classify_intent":
                prompts.append((system, user))
                return '{"intent":"help"}'
            return "Ответ"

    agent_ctx.llm = Model()
    await answer(agent_ctx, "Первый вопрос")
    await answer(agent_ctx, "Второй вопрос")
    await answer(agent_ctx, "После сброса", history_limit=0)

    assert prompts[0][0] == prompts[1][0] == prompts[2][0]
    assert "Первый вопрос" in prompts[1][1]
    assert prompts[2][1] == "После сброса"


async def test_invalid_date_clarifies_before_search_or_draft(agent_ctx, monkeypatch):
    async def forbidden_search(*args):
        pytest.fail("Поиск до уточнения даты")

    class Model:
        async def complete(self, system, user, *, task, json_mode=False):
            return {
                "classify_intent": '{"intent":"create_post"}',
                "args_create_post": '{"topic":"Витрины","when":"неизвестная дата"}',
            }[task]

    monkeypatch.setattr("app.agent.harness.retrieve", forbidden_search)
    agent_ctx.llm = Model()
    reply = await answer(agent_ctx, "Подготовь пост про витрины, дату потом уточню")
    assert reply == {"text": "Уточните дату и время публикации."}
    assert await PostRepo(agent_ctx.session).list_for_business(agent_ctx.business_id) == []
