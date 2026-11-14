"""Форматы материалов профиля (изображения, PDF, текстовые документы) и безопасное декодирование."""

from io import BytesIO
from pathlib import Path
from xml.etree.ElementTree import ParseError, tostring

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import parse
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from resvg_py import svg_to_bytes

register_heif_opener(thumbnails=False)

RASTER_EXTENSIONS = {
    ".png",
    ".apng",
    ".jpg",
    ".jpeg",
    ".jfif",
    ".webp",
    ".gif",
    ".bmp",
    ".dib",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
    ".ico",
    ".icns",
    ".jp2",
    ".j2k",
    ".jpf",
    ".jpx",
    ".ppm",
    ".pgm",
    ".pbm",
    ".pnm",
    ".tga",
    ".qoi",
    ".psd",
}
IMAGE_EXTENSIONS = RASTER_EXTENSIONS | {".svg"}
# Текстовые документы читаются как текст, без просмотра моделью
TEXT_EXTENSIONS = {".docx", ".txt", ".md"}
PROFILE_FILE_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"} | TEXT_EXTENSIONS
FORMAT_ERROR = (
    "Принимаются PDF, Word (.docx), текстовые файлы (.txt, .md) и изображения. "
    "Презентации экспортируйте в PDF."
)
# Отдельных картинок в профиле не больше: каждая обрабатывается тяжёлой моделью
MAX_PROFILE_IMAGES = 5
# Картинка для модели вписывается в рамку Full HD (пропорции сохраняются, без обрезки)
MODEL_IMAGE_BOX = (1920, 1080)
RASTER_FORMATS = (
    "PNG",
    "JPEG",
    "WEBP",
    "GIF",
    "BMP",
    "DIB",
    "TIFF",
    "HEIF",
    "AVIF",
    "ICO",
    "ICNS",
    "JPEG2000",
    "PPM",
    "TGA",
    "QOI",
    "PSD",
)
MAX_PIXELS = 64_000_000


class ProfileFileFormatError(ValueError):
    """Файл не является поддерживаемым документом или изображением."""


def _svg_text(path: Path) -> str:
    root = parse(path, forbid_dtd=True).getroot()
    if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
        raise ProfileFileFormatError(FORMAT_ERROR)
    for node in root.iter():
        for key, value in list(node.attrib.items()):
            if key.rsplit("}", 1)[-1] == "href" and not value.startswith("#"):
                del node.attrib[key]
    return tostring(root, encoding="unicode")


def validate_profile_file(filename: str, path: Path) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix not in PROFILE_FILE_EXTENSIONS:
        raise ProfileFileFormatError(FORMAT_ERROR)
    try:
        if suffix == ".pdf":
            with path.open("rb") as stream:
                if not stream.read(8).startswith(b"%PDF-"):
                    raise ProfileFileFormatError(FORMAT_ERROR)
        elif suffix == ".docx":
            with path.open("rb") as stream:
                if not stream.read(4).startswith(b"PK"):  # docx — это zip-архив
                    raise ProfileFileFormatError(FORMAT_ERROR)
        elif suffix in {".txt", ".md"}:
            if b"\x00" in path.read_bytes()[:4096]:  # бинарный файл под видом текста
                raise ProfileFileFormatError(FORMAT_ERROR)
        elif suffix == ".svg":
            _svg_text(path)
        else:
            with Image.open(path, formats=RASTER_FORMATS) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise ProfileFileFormatError("Разрешение изображения превышает 64 мегапикселя.")
                image.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError, ParseError, DefusedXmlException):
        raise ProfileFileFormatError(FORMAT_ERROR) from None


def render_profile_image(path: Path, suffix: str, size: int, output_format: str) -> bytes:
    source = (
        BytesIO(svg_to_bytes(svg_string=_svg_text(path), width=size, height=size))
        if suffix == ".svg"
        else path
    )
    with Image.open(source, formats=RASTER_FORMATS) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ProfileFileFormatError("Разрешение изображения превышает 64 мегапикселя.")
        image.thumbnail((size, size))
        rendered = ImageOps.exif_transpose(image).convert("RGBA")
        background = Image.new("RGB", rendered.size, "white")
        background.paste(rendered, mask=rendered.getchannel("A"))
        with BytesIO() as output:
            background.save(output, output_format, quality=91 if output_format == "JPEG" else 78)
            return output.getvalue()


def fit_image_for_model(
    path: Path,
    suffix: str,
    box: tuple[int, int] = MODEL_IMAGE_BOX,
    quality: int = 85,
    *,
    preserve_transparency: bool = False,
) -> bytes:
    """JPEG для модели: вписывается в рамку Full HD с сохранением пропорций, без обрезки.

    Вертикальная картинка вписывается в такую же рамку, повёрнутую набок (1080×1920), а не
    в горизонтальную — так портрет не теряет в разрешении. Увеличивать маленькие картинки
    незачем: они остаются как есть.
    """
    source = (
        BytesIO(svg_to_bytes(svg_string=_svg_text(path), width=box[0], height=box[0]))
        if suffix == ".svg"
        else path
    )
    with Image.open(source, formats=RASTER_FORMATS) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ProfileFileFormatError("Разрешение изображения превышает 64 мегапикселя.")
        rendered = ImageOps.exif_transpose(image).convert("RGBA")
    limit = box if rendered.width >= rendered.height else (box[1], box[0])
    rendered.thumbnail(limit, Image.Resampling.LANCZOS)  # только уменьшает, пропорции сохраняет
    if preserve_transparency and rendered.getchannel("A").getextrema()[0] < 255:
        with BytesIO() as output:
            rendered.save(output, "PNG", optimize=True)
            return output.getvalue()
    background = Image.new("RGB", rendered.size, "white")
    background.paste(rendered, mask=rendered.getchannel("A"))
    with BytesIO() as output:
        background.save(output, "JPEG", quality=quality, optimize=True)
        return output.getvalue()
