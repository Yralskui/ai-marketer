"""Генерация PNG через OpenAI Images API."""

from __future__ import annotations

import asyncio
from base64 import b64decode
from binascii import Error as Base64Error
from pathlib import Path
from uuid import uuid4

import httpx
from openai import AsyncOpenAI, OpenAIError

from app.images import ImageGenerationError
from app.images.base import ImageProvider
from app.llm.base import LLMError


class OpenAIImageProvider(ImageProvider):
    name = "openai"

    def __init__(
        self,
        output_dir: Path,
        *,
        api_key: str,
        model: str,
        timeout: float,
        proxies: list[str] | None = None,
        retry_budget: float = 180.0,
    ) -> None:
        if not api_key:
            raise ValueError("Для IMAGE_PROVIDER=openai задайте LLM_API_KEY или IMAGE_API_KEY")
        super().__init__(output_dir)
        self.model = model
        # По клиенту на каждый прокси из LLM_PROXY; повторы — в пределах retry_budget
        self.clients = [
            AsyncOpenAI(
                api_key=api_key,
                base_url="https://api.openai.com/v1",
                timeout=timeout,
                max_retries=0,
                http_client=httpx.AsyncClient(proxy=proxy, timeout=timeout) if proxy else None,
            )
            for proxy in proxies or [None]
        ]
        self.client = self.clients[0]
        self.retry_budget = retry_budget

    async def generate(self, prompt: str, *, size: tuple[int, int] = (1024, 1024)) -> Path:
        try:
            from app.llm.routes import call_with_routes

            result = await call_with_routes(
                self.clients,
                lambda client: client.images.generate(
                    model=self.model,
                    prompt=prompt,
                    size=f"{size[0]}x{size[1]}",
                    n=1,
                ),
                budget_seconds=self.retry_budget,
            )
            encoded = result.data[0].b64_json if result.data else None
            if not encoded:
                raise ImageGenerationError("OpenAI вернул пустое изображение")
            data = b64decode(encoded, validate=True)
            path = self.output_dir / f"openai_{uuid4().hex}.png"
            await asyncio.to_thread(path.write_bytes, data)
        except (OpenAIError, LLMError, Base64Error, OSError) as exc:
            raise ImageGenerationError("Не удалось получить изображение OpenAI") from exc
        return path
