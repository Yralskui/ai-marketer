"""Преобразование материала в сжатые тематические Markdown-фрагменты для поиска.

Правила обработки (чтобы не платить за лишнее и не терять смысл):

* один файл — один вызов лёгкой модели для всего текста файла: у файлов разные темы, склеивать
  их в один запрос нет смысла;
* тяжёлая модель смотрит только то, что нельзя прочитать текстом: до 10 «визуальных» страниц
  PDF (брендбук: палитра, шрифты, стиль) и отдельные картинки, сжатые до Full HD;
* чисто текстовые PDF, Word и текстовые файлы читаются как текст, без картинок;
* описание сжатое, с максимальной плотностью: цвета, шрифты и факты о компании, без отступов
  и вёрстки — посты пишутся текстом.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.agent.prompts import render_prompt
from app.config import Settings
from app.db.knowledge import EMBEDDING_DIMENSIONS
from app.ingestion.documents import extract_text
from app.ingestion.pdf_pages import analyze_pdf, pick_visual_pages, write_pages
from app.ingestion.profile_files import IMAGE_EXTENSIONS, fit_image_for_model

MAX_PAGES = 400
MAX_TEXT = 500_000  # символов текста на один файл
MAX_FILE_INPUT = 35_000_000
PROFILE_CONTEXT_CHARS = 8_000  # весь профиль компании: он нужен модели, чтобы понимать контекст
CHUNK_CHARS = 3_000
MAX_CHUNKS = 12


class Chunk(BaseModel):
    category: Literal["business", "voice", "visual", "reference"]
    title: str
    markdown: str


class MaterialSummary(BaseModel):
    chunks: list[Chunk]


def read_text_file(path: Path) -> str:
    """Текст .docx/.txt/.md. Слишком большой документ отклоняется, а не обрезается молча."""
    data = path.read_bytes()
    if path.suffix.lower() == ".docx":
        text = extract_text(path.name, data)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1251")
    return _checked_text(text)


def _checked_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("В документе нет текста")
    if len(text) > MAX_TEXT:
        raise ValueError(
            f"Слишком большой документ: больше {MAX_TEXT:,} символов".replace(",", " ")
        )
    return text


class KnowledgeModels:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=180, max_retries=1
        )

    @property
    def light_model(self) -> str:
        return self.settings.llm_model_light or self.settings.llm_model or "gpt-6-luna"

    @property
    def heavy_model(self) -> str:
        return self.settings.llm_model_heavy or self.light_model

    async def analyze(self, path: Path, profile: str) -> list[str]:
        suffix = path.suffix.lower()
        context = profile[:PROFILE_CONTEXT_CHARS]
        text_system = render_prompt("analyze_text", profile=context)
        visual_system = render_prompt("analyze_visual", profile=context)
        if suffix == ".pdf":
            return await self._analyze_pdf(path, text_system, visual_system)
        if suffix in IMAGE_EXTENSIONS:
            content = await asyncio.to_thread(fit_image_for_model, path, suffix)
            return await self._summarize(
                self.heavy_model,
                visual_system,
                "Изображение",
                {
                    "type": "input_image",
                    "detail": "high",
                    "image_url": "data:image/jpeg;base64," + base64.b64encode(content).decode(),
                },
            )
        text = await asyncio.to_thread(read_text_file, path)
        return await self._summarize(
            self.light_model,
            text_system,
            "Текстовый документ",
            {"type": "input_text", "text": text},
        )

    async def _analyze_pdf(self, path: Path, text_system: str, visual_system: str) -> list[str]:
        analysis = await asyncio.to_thread(analyze_pdf, path, MAX_PAGES)
        results: list[str] = []
        if analysis.has_text:
            text = _checked_text(analysis.text)
            results += await self._summarize(
                self.light_model,
                text_system,
                f"PDF ({analysis.kind}), текст документа",
                {"type": "input_text", "text": text},
            )
        numbers = pick_visual_pages(analysis)
        if numbers:
            part = path.parent / "visual-pages.pdf"
            while True:
                await asyncio.to_thread(write_pages, path, numbers, part)
                if part.stat().st_size <= MAX_FILE_INPUT or len(numbers) == 1:
                    break
                numbers = numbers[:-1]  # не влезло в запрос — убираем последнюю страницу
            if part.stat().st_size > MAX_FILE_INPUT:
                raise ValueError("Страница PDF превышает предел визуального запроса")
            content = await asyncio.to_thread(part.read_bytes)
            pages = ", ".join(map(str, numbers))
            results += await self._summarize(
                self.heavy_model,
                visual_system,
                f"PDF, страницы {pages}",
                {
                    "type": "input_file",
                    "filename": part.name,
                    "file_data": "data:application/pdf;base64,"
                    + base64.b64encode(content).decode(),
                },
            )
        if not results:
            raise ValueError("В PDF не нашлось ни текста, ни страниц для просмотра")
        return results

    async def _summarize(
        self, model: str, instructions: str, label: str, material: dict
    ) -> list[str]:
        response = await self.client.responses.parse(
            model=model,
            instructions=instructions,
            input=[{"role": "user", "content": [{"type": "input_text", "text": label}, material]}],
            text_format=MaterialSummary,
            reasoning={"effort": "none" if model.startswith("gpt-6-luna") else "low"},
            store=False,
        )
        parsed = response.output_parsed
        if parsed is None or not parsed.chunks:
            raise ValueError("Модель не вернула описание материала")
        return [
            f"## {chunk.title[:160]}\n\nИсточник: {label}\nРаздел: {chunk.category}\n\n"
            f"{chunk.markdown[:CHUNK_CHARS]}"
            for chunk in parsed.chunks[:MAX_CHUNKS]
            if chunk.markdown.strip()
        ]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for start in range(0, len(texts), 32):
            batch = await self.client.embeddings.create(
                model=self.settings.embedding_model,
                input=texts[start : start + 32],
                dimensions=EMBEDDING_DIMENSIONS,
            )
            result.extend(
                item.embedding for item in sorted(batch.data, key=lambda item: item.index)
            )
        return result
