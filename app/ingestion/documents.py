"""Извлечение текста из документов: PDF, DOCX, TXT/MD."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # лимит Telegram Bot API на скачивание


class UnsupportedDocumentError(ValueError):
    """Формат не поддерживается или файл не читается."""


def extract_text(filename: str, data: bytes) -> str:
    """Текст документа по расширению имени файла."""
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        text = _from_pdf(data)
    elif ext == ".docx":
        text = _from_docx(data)
    elif ext in (".txt", ".md"):
        text = _from_txt(data)
    else:
        raise UnsupportedDocumentError(
            f"Формат {ext or 'без расширения'} не поддерживается. "
            f"Пришлите {', '.join(SUPPORTED_EXTENSIONS)}"
        )
    text = _normalize(text)
    if not text:
        raise UnsupportedDocumentError("В документе не нашлось текста (возможно, это скан)")
    return text


async def extract_text_async(filename: str, data: bytes) -> str:
    """То же, но в отдельном потоке — парсинг не блокирует бота."""
    return await asyncio.to_thread(extract_text, filename, data)


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except PdfReadError as exc:
        raise UnsupportedDocumentError(f"Не удалось прочитать PDF: {exc}") from exc


def _from_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # python-docx бросает разные исключения на битых файлах
        raise UnsupportedDocumentError(f"Не удалось прочитать DOCX: {exc}") from exc
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def _from_txt(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _normalize(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.replace("\r", "").split("\n")]
    result: list[str] = []
    for line in lines:
        if line or (result and result[-1]):
            result.append(line)
    return "\n".join(result).strip()
