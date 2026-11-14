"""Преобразование материала в тематические Markdown-фрагменты."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter

from app.agent.prompts import render_prompt
from app.config import Settings
from app.db.knowledge import EMBEDDING_DIMENSIONS
from app.ingestion.documents import extract_text
from app.llm.routes import call_with_routes, close_clients, openai_clients

VISUAL_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
TEXT_EXTENSIONS = {".docx", ".txt", ".md", ".csv", ".json", ".rtf"}
MATERIAL_EXTENSIONS = VISUAL_EXTENSIONS | TEXT_EXTENSIONS
PAGE_BATCH = 6
MAX_PAGES = 180
MAX_TEXT = 2_000_000
MAX_FILE_INPUT = 35_000_000


class Chunk(BaseModel):
    category: Literal["business", "voice", "visual", "reference"]
    title: str
    markdown: str


class MaterialSummary(BaseModel):
    chunks: list[Chunk]


def pdf_parts(path: Path, output: Path):
    reader = PdfReader(path)
    if len(reader.pages) > MAX_PAGES:
        raise ValueError("PDF: не более 180 страниц на файл")

    def split(first: int, end: int):
        writer = PdfWriter()
        for page in range(first, end):
            writer.add_page(reader.pages[page])
        part = output / f"pages-{first + 1}-{end}.pdf"
        writer.write(part)
        if part.stat().st_size > MAX_FILE_INPUT:
            part.unlink()
            if end - first == 1:
                raise ValueError("Одна страница PDF превышает предел визуального запроса")
            mid = (first + end) // 2
            return split(first, mid) + split(mid, end)
        return [(first + 1, part)]

    return [
        part
        for first in range(0, len(reader.pages), PAGE_BATCH)
        for part in split(first, min(first + PAGE_BATCH, len(reader.pages)))
    ]


def image_for_model(path: Path) -> bytes:
    from io import BytesIO

    with Image.open(path) as source:
        source.thumbnail((2400, 2400))
        buffer = BytesIO()
        source.convert("RGB").save(buffer, "JPEG", quality=91)
        return buffer.getvalue()


def text_parts(path: Path) -> list[str]:
    data = path.read_bytes()
    if path.suffix == ".docx":
        text = extract_text(path.name, data)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1251")
    if not text.strip() or len(text) > MAX_TEXT:
        raise ValueError("Пустой документ или слишком большой объём текста")
    return [text[index : index + 30_000] for index in range(0, len(text), 30_000)]


class KnowledgeModels:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Через LLM_PROXY; временные сбои повторяются в пределах бюджета
        self.clients = openai_clients(settings, timeout=180)
        self.client = self.clients[0]
        self.retry_budget = max(settings.llm_retry_budget_seconds, 180)

    async def close(self) -> None:
        await close_clients(self.clients)

    async def analyze(self, path: Path, profile: str) -> list[str]:
        suffix = path.suffix.lower()
        system = render_prompt("analyze_material", profile=profile[:8_000])
        if suffix == ".pdf":
            parts = await asyncio.to_thread(pdf_parts, path, path.parent)
            inputs = []
            for first, part in parts:
                inputs.append((f"PDF, страницы начиная с {first}", part))
            results = []
            for label, part in inputs:
                content = await asyncio.to_thread(part.read_bytes)
                results.extend(
                    await self._summarize(
                        system,
                        label,
                        {
                            "type": "input_file",
                            "filename": part.name,
                            "file_data": "data:application/pdf;base64,"
                            + base64.b64encode(content).decode(),
                        },
                    )
                )
            return results
        if suffix in VISUAL_EXTENSIONS:
            content = await asyncio.to_thread(image_for_model, path)
            return await self._summarize(
                system,
                "Визуальный референс",
                {
                    "type": "input_image",
                    "detail": "high",
                    "image_url": "data:image/jpeg;base64," + base64.b64encode(content).decode(),
                },
            )
        results = []
        for part in await asyncio.to_thread(text_parts, path):
            results.extend(
                await self._summarize(
                    system, "Текстовый источник", {"type": "input_text", "text": part}
                )
            )
        return results

    async def _summarize(self, instructions: str, label: str, material: dict) -> list[str]:
        model = self.settings.llm_model_heavy or self.settings.llm_model_light or "gpt-6-luna"
        response = await call_with_routes(
            self.clients,
            lambda client: client.responses.parse(
                model=model,
                instructions=instructions,
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": label}, material],
                    }
                ],
                text_format=MaterialSummary,
                reasoning={"effort": "none" if model.startswith("gpt-6-luna") else "low"},
                store=False,
            ),
            budget_seconds=self.retry_budget,
        )
        parsed = response.output_parsed
        if parsed is None or not parsed.chunks:
            raise ValueError("Модель не вернула описание материала")
        return [
            f"## {chunk.title[:160]}\n\nИсточник: {label}\nРаздел: {chunk.category}\n\n{chunk.markdown[:9000]}"
            for chunk in parsed.chunks[:20]
            if chunk.markdown.strip()
        ]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for start in range(0, len(texts), 32):
            part = texts[start : start + 32]
            batch = await call_with_routes(
                self.clients,
                lambda client, part=part: client.embeddings.create(
                    model=self.settings.embedding_model,
                    input=part,
                    dimensions=EMBEDDING_DIMENSIONS,
                ),
                budget_seconds=self.retry_budget,
            )
            result.extend(
                item.embedding for item in sorted(batch.data, key=lambda item: item.index)
            )
        return result
