"""Обработка материалов профиля: классификатор PDF, выбор страниц, модели, поиск, лимиты."""

from __future__ import annotations

import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import Settings
from app.ingestion import pdf_pages
from app.ingestion.knowledge import MAX_TEXT, KnowledgeModels, MaterialSummary, read_text_file
from app.ingestion.pdf_pages import PageInfo, PdfAnalysis, classify, pick_visual_pages
from app.ingestion.profile_files import (
    MAX_PROFILE_IMAGES,
    ProfileFileFormatError,
    fit_image_for_model,
    validate_profile_file,
)
from app.services import document_uploads
from app.services.document_uploads import ImageLimitError, save_document, stage_document
from app.services.knowledge_search import fuse, min_keyword_hits, query_terms

TEXT = "Салаватстекло производит стеклотару для пищевой промышленности. " * 8  # ~500 знаков
SHORT_STYLE = "Фирменный цвет: Pantone 3015, HEX #196C7F. Шрифт Montserrat."


def page(number: int, text: str = TEXT, images: int = 0, graphics: int = 0) -> PageInfo:
    return PageInfo(number, text, images, graphics)


# ----------------------------------------------------------------------------- классификатор PDF


def test_text_pdf_is_text_and_needs_no_page_screenshots():
    pages = [page(n) for n in range(1, 21)]
    assert classify(pages) == "text"
    assert pick_visual_pages(PdfAnalysis("text", pages)) == []


def test_brandbook_is_mixed_and_picks_up_to_ten_visual_pages_by_score():
    pages = [page(n) for n in range(1, 11)]  # много текстовых страниц
    pages += [page(n, "", images=2) for n in range(11, 31)]  # 20 почти пустых с картинками
    pages[14] = page(15, SHORT_STYLE, images=1)  # страница про палитру — самая нужная
    analysis = PdfAnalysis(classify(pages), pages)
    assert analysis.kind == "mixed"
    chosen = pick_visual_pages(analysis)
    assert len(chosen) == 10 and chosen == sorted(chosen)
    assert 15 in chosen  # слова «цвет», «Pantone», «шрифт» подняли страницу
    assert not set(chosen) & set(range(1, 11))  # текстовые страницы смотреть не нужно


def test_scan_without_text_takes_evenly_spread_pages():
    pages = [page(n, "", images=1) for n in range(1, 41)]
    analysis = PdfAnalysis(classify(pages), pages)
    assert analysis.kind == "scan"
    chosen = pick_visual_pages(analysis)
    assert len(chosen) == 10 and chosen[0] == 1 and chosen[-1] >= 30


def minimal_text_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return out.getvalue()


def test_real_pdf_with_text_layer_and_image_only_pdf(tmp_path):
    text_pdf = tmp_path / "text.pdf"
    text_pdf.write_bytes(minimal_text_pdf("Hello brand " * 30))
    analysis = pdf_pages.analyze_pdf(text_pdf, 50)
    assert analysis.has_text and analysis.kind == "text"

    scan = tmp_path / "scan.pdf"
    Image.new("RGB", (200, 200), "teal").save(scan, "PDF")
    analysis = pdf_pages.analyze_pdf(scan, 50)
    assert analysis.kind == "scan" and analysis.pages[0].images == 1
    with pytest.raises(ValueError, match="не более"):
        pdf_pages.analyze_pdf(scan, 0)


# ----------------------------------------------------------------------------- картинки и форматы


def test_image_is_fitted_to_full_hd_without_cropping(tmp_path):
    cases = {  # (исходный размер) -> ожидаемый размер, пропорции сохраняются
        (4000, 3000): (1440, 1080),
        (3000, 4000): (1080, 1440),
        (800, 600): (800, 600),  # маленькие не увеличиваем
    }
    for source, expected in cases.items():
        path = tmp_path / f"{source[0]}x{source[1]}.png"
        Image.new("RGB", source, "red").save(path)
        result = Image.open(BytesIO(fit_image_for_model(path, ".png")))
        assert result.size == expected


def test_new_document_formats_are_accepted_and_validated(tmp_path):
    docx = tmp_path / "a.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", "<w/>")
    validate_profile_file("a.docx", docx)
    (tmp_path / "n.md").write_text("# Заголовок\nТекст", encoding="utf-8")
    validate_profile_file("n.md", tmp_path / "n.md")
    fake = tmp_path / "fake.docx"
    fake.write_bytes(b"not a zip")
    with pytest.raises(ProfileFileFormatError):
        validate_profile_file("fake.docx", fake)
    binary = tmp_path / "bin.txt"
    binary.write_bytes(b"abc\x00def")
    with pytest.raises(ProfileFileFormatError):
        validate_profile_file("bin.txt", binary)


def test_text_is_limited_to_500k_characters(tmp_path):
    ok = tmp_path / "ok.txt"
    ok.write_text("а" * MAX_TEXT, encoding="utf-8")
    assert len(read_text_file(ok)) == MAX_TEXT
    big = tmp_path / "big.txt"
    big.write_text("а" * (MAX_TEXT + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="Слишком большой"):
        read_text_file(big)


# ----------------------------------------------------------------------------- модели: кто что смотрит


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        chunk = SimpleNamespace(category="business", title="Тема", markdown="Факт")
        return SimpleNamespace(output_parsed=MaterialSummary.model_construct(chunks=[chunk]))


@pytest.fixture
def models() -> tuple[KnowledgeModels, FakeResponses]:
    settings = Settings(
        _env_file=None,
        llm_api_key="test",
        llm_model_light="light-model",
        llm_model_heavy="heavy-model",
    )
    instance = KnowledgeModels(settings)
    fake = FakeResponses()
    instance.client = SimpleNamespace(responses=fake)
    return instance, fake


async def test_text_document_is_one_light_call_and_no_images(models, tmp_path):
    instance, fake = models
    path = tmp_path / "doc.txt"
    path.write_text("Прайс и описание продукции. " * 50, encoding="utf-8")
    chunks = await instance.analyze(path, "профиль " * 1000)
    assert len(fake.calls) == 1 and fake.calls[0]["model"] == "light-model"
    assert chunks and "Источник" in chunks[0]
    # профиль в промпте укорочен: в инструкцию не попадает весь длинный профиль
    assert fake.calls[0]["instructions"].count("профиль") <= 2000 // len("профиль ") + 1


async def test_image_goes_to_heavy_model_compressed(models, tmp_path):
    instance, fake = models
    path = tmp_path / "photo.png"
    Image.new("RGB", (4000, 3000), "blue").save(path)
    await instance.analyze(path, "")
    (call,) = fake.calls
    assert call["model"] == "heavy-model"
    assert call["input"][0]["content"][1]["type"] == "input_image"


async def test_text_pdf_uses_light_model_only(models, tmp_path):
    instance, fake = models
    path = tmp_path / "doc.pdf"
    path.write_bytes(minimal_text_pdf("Contract text " * 30))
    await instance.analyze(path, "")
    assert [call["model"] for call in fake.calls] == ["light-model"]


async def test_scan_pdf_uses_heavy_model_only(models, tmp_path):
    instance, fake = models
    path = tmp_path / "scan.pdf"
    Image.new("RGB", (300, 300), "teal").save(path, "PDF")
    await instance.analyze(path, "")
    assert [call["model"] for call in fake.calls] == ["heavy-model"]
    assert fake.calls[0]["input"][0]["content"][1]["type"] == "input_file"


# ----------------------------------------------------------------------------- гибридный поиск


def test_query_terms_drop_noise_and_cap_length():
    assert query_terms("Какой у нас цвет и шрифт для логотипа?") == [
        "какой",
        "цвет",
        "шрифт",
        "логотипа",
    ]
    assert len(query_terms(" ".join(f"слово{i}" for i in range(30)))) == 8


def test_keyword_match_needs_two_words_unless_query_has_one():
    assert min_keyword_hits(["цвет", "шрифт"]) == 2
    assert min_keyword_hits(["артикул-12"]) == 1


def test_fusion_lifts_chunks_found_by_both_methods():
    assert fuse([1, 2, 3], [3, 9], 3) == [3, 1, 2]
    only_vector = fuse([5, 6], [], 6)
    assert only_vector == [5, 6]  # без ключевых слов порядок вектора не меняется
    assert len(fuse(list(range(10)), list(range(10, 20)), 6)) == 6


# ----------------------------------------------------------------------------- лимит картинок


class FakeStorage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def upload(self, path, key) -> None:
        self.keys.append(key)

    async def delete(self, key) -> None:
        self.keys.remove(key)


async def test_profile_accepts_at_most_five_separate_images(session, owner, monkeypatch, tmp_path):
    _, company = owner
    storage = FakeStorage()
    monkeypatch.setattr(document_uploads, "DATA_DIR", tmp_path)

    async def add(name: str, content: bytes) -> None:
        with stage_document(company.id) as staged:
            staged.write_bytes(content)
            await save_document(session, company, name, staged, storage=storage)

    for number in range(MAX_PROFILE_IMAGES):
        await add(f"photo{number}.png", f"image-{number}".encode())
    with pytest.raises(ImageLimitError, match="не больше 5"):
        await add("photo-extra.png", b"image-extra")
    await add("price.pdf", b"%PDF-1.4 price")  # документы лимитом картинок не ограничены
    await add("notes.txt", b"notes")
    assert len(storage.keys) == MAX_PROFILE_IMAGES + 2
