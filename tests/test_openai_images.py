"""Проверка запроса к Images API и сохранения черновика при отказе."""

import json
from base64 import b64encode
from io import BytesIO

import httpx
from openai import AsyncOpenAI
from PIL import Image

from app.agent.tools.post_tools import CreatePostArgs, create_post_draft
from app.images.factory import create_image_provider


async def test_openai_images_uses_shared_key_and_saves_png(settings, tmp_path):
    settings.image_provider = "openai"
    settings.llm_api_key = "test-project-key"
    settings.image_model = "gpt-image-2.5-flare"
    provider = create_image_provider(settings, tmp_path)
    client = provider.client
    buffer = BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="PNG")
    png = buffer.getvalue()

    def generate(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer test-project-key"
        payload = json.loads(request.content)
        assert payload["model"] == settings.image_model
        assert payload["prompt"] == "Стеклянный фасад"
        assert payload["size"] == "1024x1024"
        return httpx.Response(200, json={"data": [{"b64_json": b64encode(png).decode()}]})

    await client.close()
    async with AsyncOpenAI(
        api_key="test-project-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(generate)),
    ) as fake_client:
        # Сохраняем настройки ключа, модели и адреса, собранные фабрикой.
        fake_client.api_key = client.api_key
        fake_client.base_url = client.base_url
        provider.client = fake_client
        provider.clients = [fake_client]
        path = await provider.generate("Стеклянный фасад")

    assert path.parent == tmp_path
    assert path.read_bytes() == png


async def test_failed_image_still_sends_draft_for_approval(agent_ctx, tmp_path):
    agent_ctx.settings.image_provider = "openai"
    agent_ctx.settings.llm_api_key = "test-project-key"
    provider = create_image_provider(agent_ctx.settings, tmp_path)
    await provider.client.close()
    transport = httpx.MockTransport(
        lambda _: httpx.Response(403, json={"error": {"message": "test failure"}})
    )
    async with AsyncOpenAI(
        api_key="test-project-key", http_client=httpx.AsyncClient(transport=transport)
    ) as fake_client:
        provider.client = fake_client
        provider.clients = [fake_client]
        agent_ctx.images = provider
        result = await create_post_draft(
            agent_ctx, CreatePostArgs(topic="Новое производство", with_image=True)
        )

    assert result["status"] == "pending_approval"
    assert not result["has_image"]
    assert result["warning"]
    assert agent_ctx.notifier.drafts == [(agent_ctx.target_chat_id, result["post_id"])]
