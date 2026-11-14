"""Собирает карточку из фотографии, текста и оригинального логотипа."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field

SIZE = 1200
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
MAC_FONT_DIR = Path("/System/Library/Fonts/Supplemental")


class SocialLayout(BaseModel):
    layout: Literal["greeting", "editorial", "product", "split", "cover", "poster"] = "editorial"
    headline: str = Field(min_length=1, max_length=100)
    eyebrow: str = Field("", max_length=28)
    subhead: str = Field("", max_length=100)
    background_prompt: str = Field(min_length=1, max_length=1400)
    accent: str = Field("#334A57", pattern=r"^#[0-9A-Fa-f]{6}$")
    surface: str = Field("#F6F8F8", pattern=r"^#[0-9A-Fa-f]{6}$")
    logo_position: Literal["left", "center", "right"] = "left"


@dataclass(frozen=True)
class LayoutTemplate:
    description: str
    photo: tuple[int, int, int, int]
    panel: tuple[int, int, int, int]
    dark_panel: bool = False


# Общие входы: визуальные материалы бренда, оригинал логотипа, фотография и заголовок.
# У каждого шаблона есть требование к сюжету и свои области фото и текста.
LAYOUTS = {
    "greeting": LayoutTemplate(
        "Поздравление с известным поводом и атмосферное фото.",
        (0, 154, 1200, 1200),
        (0, 706, 1200, 1200),
        True,
    ),
    "editorial": LayoutTemplate(
        "Новость или анонс с конкретной темой и горизонтальным фото.",
        (62, 522, 1138, 1138),
        (0, 146, 1200, 514),
    ),
    "product": LayoutTemplate(
        "Названный продукт, узнаваемый предметный кадр и, если есть, короткое описание.",
        (558, 414, 1138, 1138),
        (62, 422, 530, 1138),
    ),
    "split": LayoutTemplate(
        "Вертикальный кадр с одним главным объектом и короткий поясняющий текст.",
        (0, 0, 584, 1200),
        (584, 190, 1200, 1200),
    ),
    "cover": LayoutTemplate(
        "Один выразительный сюжет с основным объектом в верхней половине и коротким заголовком.",
        (0, 0, 1200, 1200),
        (54, 662, 1146, 1146),
        True,
    ),
    "poster": LayoutTemplate(
        "Событие или объявление с конкретным поводом и широким фото сверху.",
        (62, 172, 1138, 752),
        (0, 768, 1200, 1200),
    ),
}


def _font(size: int, *, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidate = FONT_DIR / name
    if not candidate.is_file():
        candidate = MAC_FONT_DIR / ("Arial Bold.ttf" if bold else "Arial.ttf")
    return ImageFont.truetype(str(candidate), size)


def _lines(draw, text, font, width):
    lines = []
    for word in text.split():
        if draw.textlength(word, font=font) > width:
            return []
        if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines


def render_layout(
    path: Path,
    layout: SocialLayout,
    logo_path: Path,
    output: Path,
) -> None:
    canvas = Image.new("RGB", (SIZE, SIZE), layout.surface)
    accent = ImageColor.getrgb(layout.accent)
    template = LAYOUTS[layout.layout]
    photo, panel, dark = template.photo, template.panel, template.dark_panel
    x, y, right, bottom = photo
    with Image.open(path) as source:
        fitted = ImageOps.fit(
            source.convert("RGB"), (right - x, bottom - y), Image.Resampling.LANCZOS
        )
    canvas.paste(fitted, (x, y))
    draw = ImageDraw.Draw(canvas)
    if layout.layout == "product":
        draw.rounded_rectangle(
            panel, radius=24, fill=tuple(round(242 + channel * 0.05) for channel in accent)
        )
        draw.line((62, 394, 1138, 394), fill=layout.accent, width=5)
    if dark:
        draw.rounded_rectangle(
            panel, radius=22, fill=tuple(round(channel * 0.40) for channel in accent)
        )
    with Image.open(logo_path) as original:
        logo = original.convert("RGBA")
    origin = {"left": 52, "center": 438, "right": 824}[layout.logo_position]
    draw.rounded_rectangle((origin, 28, origin + 324, 130), radius=16, fill="white")
    logo.thumbnail((286, 80), Image.Resampling.LANCZOS)
    canvas.paste(logo, (origin + (324 - logo.width) // 2, 39 + (80 - logo.height) // 2), logo)
    if layout.layout == "product":
        _draw_copy(
            draw,
            layout.model_copy(update={"subhead": ""}),
            (62, 146, 1138, 394),
            dark=_prefers_white(layout.surface),
        )
        layout = layout.model_copy(
            update={"headline": layout.subhead, "subhead": "", "eyebrow": ""}
        )
    _draw_copy(
        draw,
        layout,
        panel,
        dark=dark or (layout.layout != "product" and _prefers_white(layout.surface)),
    )
    canvas.save(output)


def _draw_copy(draw, layout, panel, *, dark=False):
    left, top, right, bottom = panel
    inset = 36 if layout.layout == "product" else 62
    left += inset
    right -= inset
    top += 34
    color = "white" if dark else "#19343A"
    secondary = "#DDEBEB" if dark else "#344D52"
    draw.line((left, top, min(left + 110, right), top), fill=layout.accent, width=6)
    top += 23
    if layout.eyebrow:
        for size in range(24, 12, -1):
            label_font = _font(size, bold=True)
            if draw.textlength(layout.eyebrow, font=label_font) <= right - left:
                draw.text((left, top), layout.eyebrow, font=label_font, fill=secondary, anchor="lt")
                break
        top += 45
    if not layout.headline:
        return
    subhead_font = _font(28)
    subhead = _lines(draw, layout.subhead, subhead_font, right - left)
    if layout.subhead and not subhead:
        subhead_font = _font(18)
        subhead = _lines(draw, layout.subhead, subhead_font, right - left)
    available = bottom - top - 24 - (18 + len(subhead) * 37 if subhead else 0)
    for size in range(94, 19, -3):
        font = _font(size, bold=True)
        lines = _lines(draw, layout.headline, font, right - left)
        step = int(size * 1.22)
        if lines and len(lines) * step <= available:
            break
    else:
        raise ValueError("Заголовок не помещается в выбранный макет.")
    for line in lines:
        draw.text((left, top), line, font=font, fill=color, anchor="lt")
        top += step
    top += 16
    for line in subhead:
        draw.text((left, top), line, font=subhead_font, fill=secondary, anchor="lt")
        top += 37


def _prefers_white(value: str) -> bool:
    red, green, blue = ImageColor.getrgb(value)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue < 135
