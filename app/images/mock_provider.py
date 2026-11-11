"""Картинка-заглушка: градиент + текст темы (Pillow). Без сети и ключей."""

from __future__ import annotations

import asyncio
import hashlib
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.images.base import ImageProvider

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

        # Цвета зависят от текста — одинаковая тема даёт одинаковую картинку
        seed = int(digest, 16)
        c1 = (60 + seed % 120, 40 + (seed >> 8) % 120, 90 + (seed >> 16) % 120)
        c2 = (min(255, c1[0] + 90), min(255, c1[1] + 70), min(255, c1[2] + 50))
        width, height = size
        img = Image.new("RGB", size, c1)
        draw = ImageDraw.Draw(img)
        for y in range(height):
            t = y / height
            color = tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))
            draw.line([(0, y), (width, y)], fill=color)

        font = _load_font(max(28, width // 18))
        lines = textwrap.wrap(prompt, width=22)[:6] or ["AI-маркетолог"]
        line_h = int(font.size * 1.3) if hasattr(font, "size") else 20
        y = (height - line_h * len(lines)) // 2
        for line in lines:
            box = draw.textbbox((0, 0), line, font=font)
            x = (width - (box[2] - box[0])) // 2
            draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=(255, 255, 255))
            y += line_h
        draw.text((24, height - 48), "mock image", font=_load_font(24), fill=(255, 255, 255))
        img.save(path, format="PNG")
        return path
