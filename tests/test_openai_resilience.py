"""Устойчивость LLM к сбоям сети и прокси: запрос «стучится до конца», а не падает."""

from __future__ import annotations

import httpx
import pytest
from openai import AsyncOpenAI

import app.utils.retry as retry_mod
from app.llm.base import LLMError
from app.llm.openai_provider import OpenAIProvider
from app.utils.retry import retry_with_budget

OK_BODY = {
    "id": "c1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-5-mini",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "привет"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Паузы между повторами в тестах не ждём."""

    async def fast(_delay: float) -> None:
        return None

    monkeypatch.setattr(retry_mod.asyncio, "sleep", fast)


def _client(handler) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key="test",
        base_url="https://api.test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _provider(*handlers, budget: float = 60.0) -> OpenAIProvider:
    provider = OpenAIProvider(api_key="test", model="gpt-5-mini", retry_budget=budget)
    provider.clients = [_client(h) for h in handlers]
    return provider


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=OK_BODY)


async def test_dead_proxy_switches_to_next():
    calls = {"dead": 0, "alive": 0}

    def dead(request):
        calls["dead"] += 1
        raise httpx.ConnectError("proxy timeout")

    def alive(request):
        calls["alive"] += 1
        return ok(request)

    response = await _provider(dead, alive).chat([{"role": "user", "content": "hi"}])
    assert response.content == "привет"
    assert calls == {"dead": 1, "alive": 1}
    assert response.raw.usage.prompt_tokens == 10  # учёт расходов получит точные токены


async def test_keeps_knocking_through_flaky_network():
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        if state["n"] <= 4:
            raise httpx.ReadTimeout("slow proxy")
        return ok(request)

    response = await _provider(flaky).chat([{"role": "user", "content": "hi"}])
    assert response.content == "привет" and state["n"] == 5


async def test_rate_limit_and_server_errors_are_retried():
    seq = iter(
        [
            httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "slow"}}),
            httpx.Response(502, json={"error": {"message": "bad gateway"}}),
            httpx.Response(200, json=OK_BODY),
        ]
    )
    response = await _provider(lambda r: next(seq)).chat([{"role": "user", "content": "hi"}])
    assert response.content == "привет"


async def test_wrong_key_is_not_retried():
    calls = {"n": 0}

    def unauthorized(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(LLMError, match="Неверный ключ"):
        await _provider(unauthorized).chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1


async def test_geo_block_gives_proxy_hint():
    def blocked(request):
        return httpx.Response(403, json={"error": {"message": "unsupported_country"}})

    with pytest.raises(LLMError, match="LLM_PROXY"):
        await _provider(blocked).chat([{"role": "user", "content": "hi"}])


async def test_gives_up_only_after_budget():
    clock = {"t": 0.0}
    attempts = {"n": 0}

    async def always_fails(n: int):
        attempts["n"] += 1
        raise ConnectionError("down")

    async def tick(delay: float) -> None:
        clock["t"] += delay

    with pytest.raises(ConnectionError):
        await retry_with_budget(
            always_fails,
            budget_seconds=60,
            retry_on=(ConnectionError,),
            sleep=tick,
            clock=lambda: clock["t"],
        )
    assert attempts["n"] >= 6  # 1+2+4+8+15+15… — несколько попыток, а не одна
    assert clock["t"] < 60


async def test_budget_error_is_friendly():
    def down(request):
        raise httpx.ConnectError("down")

    with pytest.raises(LLMError, match="недоступен"):
        await _provider(down, budget=0.5).chat([{"role": "user", "content": "hi"}])


def test_proxies_from_settings(settings):
    settings.llm_proxy = "http://a:1, socks5://b:2 ,"
    assert settings.llm_proxies == ["http://a:1", "socks5://b:2"]
    provider = OpenAIProvider(api_key="k", model="m", proxies=settings.llm_proxies)
    assert len(provider.clients) == 2
