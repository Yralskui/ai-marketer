"""Настоящий логотип поверх сгенерированной картинки.

Нейросеть не умеет точно рисовать чужой логотип и надписи, поэтому сцену она генерирует без
них, а логотип компании накладывается кодом: он всегда точный, в фирменных цветах.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

LOGO_WIDTH_SHARE = 0.22  # ширина логотипа относительно картинки
MARGIN_SHARE = 0.04


def apply_logo(image_path: str | Path, logo_path: str | Path) -> None:
    """Кладёт логотип в правый нижний угол на светлую подложку. Файл картинки перезаписывается."""
    path = Path(image_path)
    with Image.open(path) as source:
        fmt = source.format or "PNG"
        base = source.convert("RGBA")
    with Image.open(logo_path) as raw:
        logo = raw.convert("RGBA")
    width = max(1, int(base.width * LOGO_WIDTH_SHARE))
    logo = logo.resize((width, max(1, round(logo.height * width / logo.width))), Image.LANCZOS)
    margin = int(base.width * MARGIN_SHARE)
    pad = max(6, width // 12)
    plate = Image.new("RGBA", (logo.width + 2 * pad, logo.height + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle(
        (0, 0, plate.width - 1, plate.height - 1), pad, fill=(255, 255, 255, 235)
    )
    plate.alpha_composite(logo, (pad, pad))
    base.alpha_composite(
        plate, (base.width - plate.width - margin, base.height - plate.height - margin)
    )
    result = base.convert("RGB") if fmt.upper() in {"JPEG", "JPG"} else base
    result.save(path, fmt)
