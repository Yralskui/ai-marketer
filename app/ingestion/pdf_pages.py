"""Классификатор PDF и выбор страниц, которые стоит посмотреть глазами.

Большинство PDF — это текст: договор, прайс, регламент. Картинки страниц для них только
удорожают обработку. Исключение — брендбуки и презентации, где часть смысла лежит в
оформлении. Поэтому каждый PDF сначала классифицируется по текстовому слою:

* `text`   — почти все страницы с текстом и нет «визуальных» страниц: берём один текст;
* `mixed`  — текст есть, но есть и страницы с малым текстом и картинками: текст целиком плюс
             до MAX_VISUAL_PAGES самых «визуальных» страниц на просмотр;
* `scan`   — у большинства страниц нет текста (скан): смотреть можно только глазами, берём
             до MAX_VISUAL_PAGES страниц, равномерно по документу.

Выбор «визуальных» страниц идёт по тексту: мало знаков, есть изображения или графика, в
тексте слова про цвет, шрифт, стиль. Отступы и сетка нам не важны — посты пишутся текстом.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pypdf import PdfReader, PdfWriter

MAX_VISUAL_PAGES = 10
TEXT_PAGE_CHARS = 200  # меньше — страницу считаем «без текста»
VISUAL_PAGE_CHARS = 900  # больше — страница текстовая, смотреть её глазами незачем
TEXT_SHARE = 0.7  # доля страниц с текстом для типа `text`
SCAN_SHARE = 0.3  # меньше — скан
VISUAL_SHARE = 0.15  # доля «визуальных» страниц, после которой PDF уже не чисто текстовый

PdfKind = Literal["text", "mixed", "scan"]

# Слова, по которым страница, скорее всего, про стиль бренда
_STYLE_WORDS = re.compile(
    r"цвет|палитр|pantone|пантон|rgb|cmyk|hex|#[0-9a-f]{6}|шрифт|типограф|логотип|фирменн|"
    r"паттерн|стиль|фотограф|иллюстрац|графическ|айдентик|brand|color|font|typeface",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PageInfo:
    number: int  # с единицы
    text: str
    images: int
    graphics: int  # вложенные формы: векторные схемы, плашки с цветами

    @property
    def chars(self) -> int:
        return len(self.text.strip())

    @property
    def is_visual(self) -> bool:
        return self.chars <= VISUAL_PAGE_CHARS and (self.images > 0 or self.graphics > 0)

    @property
    def score(self) -> float:
        """Чем выше, тем нужнее посмотреть страницу: мало текста, есть графика, слова про стиль."""
        words = len(_STYLE_WORDS.findall(self.text))
        emptiness = 1 - min(self.chars, VISUAL_PAGE_CHARS) / VISUAL_PAGE_CHARS
        return self.images * 2 + self.graphics + 3 * min(words, 4) + emptiness


@dataclass(frozen=True)
class PdfAnalysis:
    kind: PdfKind
    pages: list[PageInfo]

    @property
    def text(self) -> str:
        return "\n\n".join(page.text.strip() for page in self.pages if page.text.strip())

    @property
    def has_text(self) -> bool:
        return sum(page.chars for page in self.pages) >= TEXT_PAGE_CHARS


def _xobjects(page) -> list:
    try:
        resources = page["/Resources"].get_object()
        return list(resources["/XObject"].get_object().values())
    except (KeyError, AttributeError, TypeError):
        return []


def _inspect_page(page, number: int) -> PageInfo:
    images = graphics = 0
    for ref in _xobjects(page):
        try:
            subtype = ref.get_object().get("/Subtype")
        except (AttributeError, TypeError):
            continue
        if subtype == "/Image":
            images += 1
        elif subtype == "/Form":
            graphics += 1
    try:
        text = page.extract_text() or ""
    except Exception:  # битая страница не должна ронять весь файл
        text = ""
    return PageInfo(number, text, images, graphics)


def classify(pages: list[PageInfo]) -> PdfKind:
    if not pages:
        return "scan"
    with_text = sum(1 for page in pages if page.chars >= TEXT_PAGE_CHARS)
    if with_text / len(pages) < SCAN_SHARE:
        return "scan"
    visual = sum(1 for page in pages if page.is_visual)
    if with_text / len(pages) >= TEXT_SHARE and visual / len(pages) <= VISUAL_SHARE:
        return "text"
    return "mixed"


def analyze_pdf(path: Path, max_pages: int) -> PdfAnalysis:
    reader = PdfReader(path)
    if len(reader.pages) > max_pages:
        raise ValueError(f"PDF: не более {max_pages} страниц на файл")
    pages = [_inspect_page(page, index + 1) for index, page in enumerate(reader.pages)]
    return PdfAnalysis(classify(pages), pages)


def pick_visual_pages(analysis: PdfAnalysis, limit: int = MAX_VISUAL_PAGES) -> list[int]:
    """Номера страниц (с единицы) для просмотра моделью. Для `text` — пусто."""
    pages = analysis.pages
    if analysis.kind == "text" or not pages:
        return []
    if analysis.kind == "scan":
        if len(pages) <= limit:
            return [page.number for page in pages]
        step = len(pages) / limit
        return sorted({pages[int(index * step)].number for index in range(limit)})
    candidates = sorted(
        (page for page in pages if page.is_visual), key=lambda page: page.score, reverse=True
    )
    return sorted(page.number for page in candidates[:limit])


def write_pages(path: Path, numbers: list[int], output: Path) -> Path:
    """PDF только из выбранных страниц — он и уходит модели вместо целого файла."""
    reader = PdfReader(path)
    writer = PdfWriter()
    for number in numbers:
        writer.add_page(reader.pages[number - 1])
    writer.write(output)
    return output
