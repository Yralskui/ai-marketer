"""Карусель из поста: 3–6 слайдов в фирменном стиле без нейросети.

Первый слайд — заголовок, дальше — абзацы поста, последний — призыв к действию.
Хештеги на слайды не попадают. Цвета берутся из бренд-гида компании.
"""

from __future__ import annotations

import asyncio
import re
from hashlib import sha1
from pathlib import Path

from app.db.models import Business, Post
from app.images.brand_card import palette_from_text, render_card
from app.services.brand import get_guide

MAX_SLIDES = 6
SLIDE_CHARS = 260
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿]️?")


def _clean(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def split_slides(text: str) -> tuple[str, list[str]]:
    """Заголовок и тексты слайдов из поста."""
    paragraphs = [
        _clean(part)
        for part in re.split(r"\n\s*\n", text)
        if part.strip() and not part.strip().startswith("#")
    ]
    paragraphs = [part for part in paragraphs if part]
    if not paragraphs:
        return "Пост", []
    title, rest = paragraphs[0], paragraphs[1:]
    slides: list[str] = []
    for paragraph in rest:
        # Длинный абзац делим по предложениям, чтобы текст на слайде читался
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        current = ""
        for sentence in sentences:
            if current and len(current) + len(sentence) > SLIDE_CHARS:
                slides.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            slides.append(current)
    return title, slides[: MAX_SLIDES - 1]


def palette_for(business: Business):
    return palette_from_text(" ".join(get_guide(business).colors))


async def build(business: Business, post: Post, output_dir: Path) -> list[Path]:
    """PNG-слайды карусели; одинаковый текст даёт одни и те же файлы."""
    title, slides = split_slides(post.text)
    palette = palette_for(business)
    total = len(slides) + 1
    digest = sha1(f"{business.id}:{post.text}".encode()).hexdigest()[:10]
    output_dir.mkdir(parents=True, exist_ok=True)

    def render() -> list[Path]:
        paths = []
        cards = [("", title)] + [(text, "") for text in slides]
        for index, (body, heading) in enumerate(cards, start=1):
            path = output_dir / f"carousel_{post.id}_{digest}_{index}.png"
            if not path.exists():
                image = render_card(
                    heading or title,
                    palette=palette,
                    caption=business.name if index == 1 else f"{index}/{total}",
                    body=body,
                    counter="Листайте →" if index < total else "",
                )
                image.save(path, format="PNG")
            paths.append(path)
        return paths

    return await asyncio.to_thread(render)
