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
    # TODO(поток A): реальный провайдер (OpenAI Images / Kandinsky / YandexART) —
    # новый файл app/images/<name>_provider.py + ветка здесь.
    raise ValueError(f"Неизвестный IMAGE_PROVIDER: {provider!r}")
