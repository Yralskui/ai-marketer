"""Извлечение текста из TXT и DOCX (файлы создаются прямо в тесте)."""

from __future__ import annotations

import io

import docx
import pytest

from app.ingestion.documents import UnsupportedDocumentError, extract_text, extract_text_async


def test_txt_utf8():
    data = "Кофейня «Зерно»\r\nЛучший кофе в городе.".encode()
    assert extract_text("about.txt", data) == "Кофейня «Зерно»\nЛучший кофе в городе."


def test_txt_cp1251():
    data = "Пекарня у дома".encode("cp1251")
    assert extract_text("info.TXT", data) == "Пекарня у дома"


async def test_docx_paragraphs_and_tables():
    document = docx.Document()
    document.add_paragraph("Студия йоги «Лотос»")
    document.add_paragraph("Занятия для начинающих и продолжающих.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Абонемент"
    table.rows[0].cells[1].text = "5000 ₽"
    buffer = io.BytesIO()
    document.save(buffer)

    text = await extract_text_async("price.docx", buffer.getvalue())
    assert "Студия йоги «Лотос»" in text
    assert "Занятия для начинающих" in text
    assert "Абонемент | 5000 ₽" in text


def test_unsupported_extension():
    with pytest.raises(UnsupportedDocumentError):
        extract_text("photo.jpg", b"\xff\xd8")


def test_empty_text_rejected():
    with pytest.raises(UnsupportedDocumentError):
        extract_text("empty.txt", b"   \n  ")
