"""Форматы визуальных материалов профиля и их безопасное декодирование."""

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
PROFILE_FILE_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}
FORMAT_ERROR = "Принимаются изображения и PDF. Презентации экспортируйте в PDF."
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
    """Файл не является поддерживаемым изображением или PDF."""


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
