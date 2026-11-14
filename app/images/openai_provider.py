"""Генерация PNG через OpenAI Images API."""

from __future__ import annotations

import asyncio
import logging
from base64 import b64decode
from binascii import Error as Base64Error
from pathlib import Path
from uuid import uuid4

from openai import APITimeoutError, AsyncOpenAI, OpenAIError

from app.images import ImageGenerationError, ImageGenerationTimeout
from app.images.base import ImageProvider

log = logging.getLogger(__name__)


class OpenAIImageProvider(ImageProvider):
    name = "openai"

    def __init__(self, output_dir: Path, *, api_key: str, model: str, timeout: float) -> None:
        if not api_key:
            raise ValueError("Для IMAGE_PROVIDER=openai задайте LLM_API_KEY или IMAGE_API_KEY")
        super().__init__(output_dir)
        self.model = model
        self.timeout = timeout
        self.client = AsyncOpenAI(
            api_key=api_key, base_url="https://api.openai.com/v1", timeout=timeout, max_retries=0
        )

    async def generate(self, prompt: str, *, size: tuple[int, int] = (1024, 1024)) -> Path:
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.client.images.generate(
                    model=self.model,
                    prompt=prompt,
                    size=f"{size[0]}x{size[1]}",
                    n=1,
                )
            encoded = result.data[0].b64_json if result.data else None
            if not encoded:
                raise ImageGenerationError("OpenAI вернул пустое изображение")
            data = b64decode(encoded, validate=True)
            path = self.output_dir / f"openai_{uuid4().hex}.png"
            await asyncio.to_thread(path.write_bytes, data)
        except (TimeoutError, APITimeoutError) as exc:
            log.warning("Images API: ожидание прервано через %s с", self.timeout)
            raise ImageGenerationTimeout(
                "Генератор изображения не ответил вовремя. Черновик сохранён без картинки."
            ) from exc
        except (OpenAIError, Base64Error, OSError) as exc:
            log.warning(
                "Images API: %s (status=%s, request_id=%s)",
                type(exc).__name__,
                getattr(exc, "status_code", None),
                getattr(exc, "request_id", None),
            )
            raise ImageGenerationError("Не удалось получить изображение OpenAI") from exc
        return path
