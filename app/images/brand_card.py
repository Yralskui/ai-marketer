"""Карточки в фирменном стиле без нейросети: фон бренда, паттерн из кругов, заголовок.

Используется картинкой-заглушкой (пока не подключена генерация) и каруселью из поста.
Логотип и товарный знак не рисуем: их можно ставить только по оригинальному макету.
"""

from __future__ import annotations

import re
import textwrap

from PIL import Image, ImageDraw, ImageFont

Color = tuple[int, int, int]

# Палитра по умолчанию — спокойная сине-серая, если у компании нет фирменных цветов
DEFAULT_PALETTE: tuple[Color, ...] = ((36, 85, 217), (19, 29, 48), (200, 214, 240), (232, 239, 255))

_BOLD_FONTS = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "arialbd.ttf",
    "seguisb.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
_REGULAR_FONTS = (
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "arial.ttf",
    "segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)
HEX_RE = re.compile(r"#([0-9A-Fa-f]{6})\b")


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _BOLD_FONTS if bold else _REGULAR_FONTS:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def palette_from_text(text: str) -> tuple[Color, ...]:
    """Цвета из описания стиля («Petrol #196C7F, #285055…»); первые — основные."""
    colors: list[Color] = []
    for value in HEX_RE.findall(text):
        color = tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
        if color not in colors:
            colors.append(color)  # type: ignore[arg-type]
    if len(colors) < 2:
        return DEFAULT_PALETTE
    while len(colors) < 4:
        colors.append(tuple(min(255, c + 150) for c in colors[0]))  # type: ignore[arg-type]
    return tuple(colors[:4])


def _mix(a: Color, b: Color, t: float) -> Color:
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))  # type: ignore[return-value]


def _wrap(draw: ImageDraw.ImageDraw, text: str, typeface, max_width: int) -> list[str]:
    """Перенос по ширине в пикселях, а не по числу символов."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words, current = paragraph.split(), ""
        for word in words:
            probe = f"{current} {word}".strip()
            if draw.textlength(probe, font=typeface) <= max_width:
                current = probe
            else:
                if current:
                    lines.append(current)
                current = (
                    word
                    if draw.textlength(word, font=typeface) <= max_width
                    else (textwrap.shorten(word, 20, placeholder="…"))
                )
        if current:
            lines.append(current)
    return lines


def _pattern(image: Image.Image, palette: tuple[Color, ...]) -> None:
    """Фирменный паттерн: круги и «таблетки» в основных цветах, в правом нижнем углу."""
    width, height = image.size
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    unit = width // 9
    light, lighter = palette[2], palette[3]
    shapes = [
        ("circle", 6.2, 6.0, light, 150),
        ("pill", 4.6, 7.2, lighter, 120),
        ("circle", 7.6, 7.4, lighter, 90),
        ("ring", 7.4, 4.6, lighter, 170),
        ("circle", 5.2, 8.4, palette[1], 160),
    ]
    for kind, cx, cy, color, alpha in shapes:
        x, y = int(cx * unit), int(cy * unit * height / width)
        r = unit
        fill = (*color, alpha)
        if kind == "circle":
            draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)
        elif kind == "ring":
            draw.ellipse((x - r, y - r, x + r, y + r), outline=fill, width=max(4, unit // 9))
        else:
            draw.rounded_rectangle((x - 2 * r, y - r, x + r, y + r), radius=r, fill=fill)
    image.alpha_composite(layer)


def render_card(
    title: str,
    *,
    palette: tuple[Color, ...] = DEFAULT_PALETTE,
    caption: str = "",
    body: str = "",
    counter: str = "",
    size: tuple[int, int] = (1080, 1080),
) -> Image.Image:
    """Квадратная карточка: заголовок (и текст слайда) на фирменном фоне."""
    width, height = size
    base, dark = palette[0], palette[1]
    image = Image.new("RGBA", size, (*base, 255))
    draw = ImageDraw.Draw(image)
    for y in range(height):  # мягкий вертикальный градиент к тёмному цвету
        draw.line([(0, y), (width, y)], fill=(*_mix(base, dark, (y / height) * 0.75), 255))
    _pattern(image, palette)
    draw = ImageDraw.Draw(image)

    margin = width // 12
    text_width = int(width * 0.78)
    y = margin
    if caption:
        small = font(max(22, width // 40), bold=True)
        draw.text((margin, y), caption.upper(), font=small, fill=(*palette[3], 255))
        y += int(small.size * 2.2) if hasattr(small, "size") else 40
        draw.line((margin, y - 18, margin + width // 6, y - 18), fill=(*palette[3], 255), width=4)

    title_font = font(max(40, width // (15 if body else 12)), bold=True)
    for line in _wrap(draw, title, title_font, text_width)[: 3 if body else 5]:
        draw.text((margin, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += int(getattr(title_font, "size", 40) * 1.22)

    if body:
        y += width // 30
        body_font = font(max(28, width // 30))
        for line in _wrap(draw, body, body_font, text_width)[:9]:
            draw.text((margin, y), line, font=body_font, fill=(235, 242, 245, 255))
            y += int(getattr(body_font, "size", 28) * 1.45)

    if counter:
        small = font(max(22, width // 42), bold=True)
        draw.text((margin, height - margin), counter, font=small, fill=(*palette[3], 255))
    return image.convert("RGB")
