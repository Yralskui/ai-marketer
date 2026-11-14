"""Картинка-заглушка в фирменном стиле (Pillow): цвета бренда, паттерн, тема. Без сети."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from PIL import ImageFont

from app.images.base import ImageProvider
from app.images.brand_card import palette_from_text, render_card

# Шрифты с кириллицей на Windows / macOS / Linux
_FONT_CANDIDATES = (
    "arial.ttf",
    "Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _load_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


class MockImageProvider(ImageProvider):
    name = "mock"

    async def generate(self, prompt: str, *, size: tuple[int, int] = (1024, 1024)) -> Path:
        return await asyncio.to_thread(self._render, prompt, size)

    def _render(self, prompt: str, size: tuple[int, int]) -> Path:
        digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
        path = self.output_dir / f"mock_{digest}.png"
        if path.exists():
            return path
        # Описание для заглушки: «тема — компания» и, если есть, стиль бренда с цветами
        first_line = prompt.splitlines()[0] if prompt else ""
        topic, _, business = first_line.rpartition(" — ")
        if not topic:
            topic, business = first_line, ""
        image = render_card(
            topic.strip() or "AI-маркетолог",
            palette=palette_from_text(prompt),
            caption=business.strip(),
            size=size,
        )
        image.save(path, format="PNG")
        return path
