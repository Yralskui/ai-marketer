import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.db.models import Post, PostStatus
from app.db.publications import PostSnapshot
from app.llm.responses_provider import ResponsesProvider
from app.services import editorial
from app.utils.time import utcnow

TZ = ZoneInfo("Europe/Moscow")


async def test_repeated_search_fetches_fresh_facts_and_limits_sources():
    urls = [[f"https://example.org/{i}", str(i)] for i in range(5)]
    llm = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                json.dumps({"summary": "Первый ответ.", "sources": urls}),
                json.dumps({"summary": "Обновлённый ответ.", "sources": urls}),
            ]
        )
    )
    first = await editorial.research(llm, TZ, "Тема 1", "Что известно?")
    second = await editorial.research(llm, TZ, "Тема 1", "Что известно?")
    assert first["summary"] == "Первый ответ."
    assert second["summary"] == "Обновлённый ответ."
    assert len(second["sources"]) == 3
    assert llm.complete.await_count == 2


async def test_critique_reuses_record_and_limits_context(session, owner):
    _, company = owner
    now = utcnow()
    post = Post(
        business_id=company.id,
        topic="Кофе",
        text="Пост про кофе",
        status=PostStatus.PUBLISHED,
        published_at=now - timedelta(days=2),
    )
    session.add(post)
    await session.flush()
    session.add_all(
        [
            PostSnapshot(post_id=post.id, hour=now - timedelta(hours=i), views=100 - i, forwards=1)
            for i in range(40)
        ]
    )
    await session.flush()
    llm = SimpleNamespace(
        complete=AsyncMock(
            return_value=json.dumps(
                {
                    "facts": "Есть замеры",
                    "hypothesis": "Возможная причина",
                    "next": "Проверить заголовок",
                }
            )
        )
    )
    first = await editorial.critique(session, llm, company, post, TZ)
    assert first == await editorial.critique(session, llm, company, post, TZ)
    llm.complete.assert_awaited_once()
    payload = json.loads(llm.complete.await_args.args[1])
    assert payload["компания"] == company.name
    assert payload["текст"] == post.text
    assert len(payload["замеры"]) <= 14
    assert payload["замеры"][0][1] == 61 and payload["замеры"][-1][1] == 100


async def test_responses_web_tool_is_bounded_and_preserves_citations(settings):
    settings.llm_api_key = "synthetic"
    provider = ResponsesProvider(settings)
    citation = SimpleNamespace(type="url_citation", url="https://example.com/a", title="Источник")
    output = SimpleNamespace(type="message", content=[SimpleNamespace(annotations=[citation])])
    provider.client.responses.create = AsyncMock(
        return_value=SimpleNamespace(
            output=[output],
            output_text="Факт",
            usage=None,
        )
    )
    try:
        result = json.loads(await provider.complete("system", "question", task="web_brief"))
        options = provider.client.responses.create.await_args.kwargs
        assert options["tools"] == [{"type": "web_search", "search_context_size": "low"}]
        assert options["max_tool_calls"] == 2 and options["max_output_tokens"] == 550
        assert result["sources"] == [["https://example.com/a", "Источник"]]
    finally:
        await provider.client.close()
