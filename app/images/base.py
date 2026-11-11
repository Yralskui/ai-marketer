"""Интерфейс генератора изображений.

⚠️ Стабильный интерфейс: меняется только по согласованию с напарником.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class ImageProvider(ABC):
    name: str = "base"

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    async def generate(self, prompt: str, *, size: tuple[int, int] = (1024, 1024)) -> Path:
        """Создаёт изображение по описанию и возвращает путь к файлу (PNG/JPG)."""
