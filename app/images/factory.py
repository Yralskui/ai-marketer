"""Выбор генератора изображений по настройкам."""

from __future__ import annotations

from pathlib import Path

from app.config import IMAGES_DIR, Settings
from app.images.base import ImageProvider


def create_image_provider(settings: Settings, output_dir: Path = IMAGES_DIR) -> ImageProvider:
    provider = settings.image_provider
    if provider == "mock":
        from app.images.mock_provider import MockImageProvider

        return MockImageProvider(output_dir)
    if provider == "openai":
        from app.images.openai_provider import OpenAIImageProvider

        return OpenAIImageProvider(
            output_dir,
            api_key=settings.image_api_key or settings.llm_api_key,
            model=settings.image_model,
            timeout=min(settings.image_timeout, 60.0),
        )
    raise ValueError(f"Неизвестный IMAGE_PROVIDER: {provider!r}")
