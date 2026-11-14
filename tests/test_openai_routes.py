"""Все клиенты OpenAI идут через LLM_PROXY и повторяют временные сбои."""

import httpx
import openai
import pytest

from app.images.factory import create_image_provider
from app.ingestion.knowledge import KnowledgeModels
from app.llm.base import LLMError
from app.llm.routes import call_with_routes, openai_clients


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1"))


def test_one_client_per_proxy(settings):
    settings.llm_api_key = "test-key"
    settings.llm_proxy = "http://127.0.0.1:1, http://127.0.0.1:2"
    assert len(openai_clients(settings)) == 2
    settings.llm_proxy = ""
    assert len(openai_clients(settings)) == 1


def test_knowledge_and_images_use_proxy_routes(settings, tmp_path):
    settings.llm_api_key = "test-key"
    settings.llm_proxy = "http://127.0.0.1:1,http://127.0.0.1:2"
    assert len(KnowledgeModels(settings).clients) == 2
    settings.image_provider = "openai"
    assert len(create_image_provider(settings, tmp_path).clients) == 2


async def test_failed_route_switches_to_next(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    used: list[str] = []

    async def request(client):
        used.append(client)
        if client == "broken":
            raise _connection_error()
        return "ok"

    assert await call_with_routes(["broken", "good"], request, budget_seconds=30) == "ok"
    assert used == ["broken", "good"]


async def test_budget_exhausted_gives_clear_error(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)

    async def request(_client):
        raise _connection_error()

    with pytest.raises(LLMError, match="недоступен"):
        await call_with_routes(["a"], request, budget_seconds=0)
