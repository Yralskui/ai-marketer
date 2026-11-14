"""Рисует аватарку бота и приветственные картинки в «стеклянной» стилистике.

Запуск из корня проекта:  python scripts/make_brand_assets.py
Результат — app/bot/assets/: avatar.png (640x640), welcome.png (1280x720),
description.png (640x360, для BotFather → Edit Description Picture).

Нужны шрифты Segoe UI (есть в Windows). Картинки уже лежат в репозитории, поэтому
скрипт нужен только для перерисовки.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "app" / "bot" / "assets"
FONTS = Path("C:/Windows/Fonts")
SS = 3  # суперсэмплинг для сглаживания

NAVY = (8, 30, 66)
DEEP = (12, 52, 110)
TEAL = (22, 160, 200)
CYAN = (120, 225, 245)
WHITE = (255, 255, 255)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size * SS)


def gradient(w: int, h: int, top: tuple, bottom: tuple, *, diagonal: bool = True) -> Image.Image:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = ((x / w) * 0.45 + (y / h) * 0.55) if diagonal else y / h
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
    return img


def glow(size: tuple[int, int], center: tuple[int, int], radius: int, color, alpha: int):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*color, alpha))
    return layer.filter(ImageFilter.GaussianBlur(radius // 2))


def glass_pane(
    size: tuple[int, int], box: tuple[int, int, int, int], radius: int, *, alpha: int = 46
) -> Image.Image:
    """Полупрозрачная «стеклянная» пластина с бликом и светлой кромкой."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(box, radius, fill=(255, 255, 255, alpha))
    d.rounded_rectangle(box, radius, outline=(255, 255, 255, 150), width=2 * SS)
    # Диагональный блик
    x0, y0, x1, y1 = box
    hl = Image.new("RGBA", size, (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    w = x1 - x0
    hd.polygon(
        [(x0 + w * 0.08, y0), (x0 + w * 0.38, y0), (x0 + w * 0.12, y1), (x0 - w * 0.18, y1)],
        fill=(255, 255, 255, 38),
    )
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius, fill=255)
    layer.alpha_composite(Image.composite(hl, Image.new("RGBA", size, (0, 0, 0, 0)), mask))
    return layer


def sparkle(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color=WHITE) -> None:
    """Четырёхлучевая звёздочка — символ AI."""
    k = r * 0.22
    d.polygon(
        [
            (cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
            (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k),
        ],
        fill=color,
    )  # fmt: skip


def growth_arrow(d: ImageDraw.ImageDraw, pts: list[tuple[float, float]], width: int) -> None:
    d.line(pts, fill=WHITE, width=width, joint="curve")
    for x, y in pts[:-1]:
        r = width * 0.9
        d.ellipse((x - r, y - r, x + r, y + r), fill=WHITE)
    # Наконечник
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    ang = math.atan2(y2 - y1, x2 - x1)
    L = width * 3.2
    left = (x2 - L * math.cos(ang - 0.5), y2 - L * math.sin(ang - 0.5))
    right = (x2 - L * math.cos(ang + 0.5), y2 - L * math.sin(ang + 0.5))
    tip = (x2 + width * 0.6 * math.cos(ang), y2 + width * 0.6 * math.sin(ang))
    d.polygon([tip, left, right], fill=WHITE)


def emblem(size: int) -> Image.Image:
    """Эмблема: стеклянная пластина, график роста и искра AI. Квадрат size×size."""
    S = size * SS
    img = gradient(S, S, NAVY, TEAL).convert("RGBA")
    img.alpha_composite(glow((S, S), (int(S * 0.72), int(S * 0.25)), int(S * 0.35), CYAN, 110))
    img.alpha_composite(glow((S, S), (int(S * 0.2), int(S * 0.85)), int(S * 0.3), DEEP, 160))

    # Две пластины стекла со сдвигом — объём
    img.alpha_composite(
        glass_pane((S, S), (int(S * 0.30), int(S * 0.24), int(S * 0.80), int(S * 0.74)),
                   int(S * 0.08), alpha=30)
    )  # fmt: skip
    img.alpha_composite(
        glass_pane((S, S), (int(S * 0.20), int(S * 0.28), int(S * 0.72), int(S * 0.78)),
                   int(S * 0.08), alpha=52)
    )  # fmt: skip

    d = ImageDraw.Draw(img)
    pts = [(S * 0.30, S * 0.64), (S * 0.41, S * 0.53), (S * 0.50, S * 0.59), (S * 0.63, S * 0.40)]
    growth_arrow(d, pts, int(S * 0.028))
    sparkle(d, S * 0.67, S * 0.30, S * 0.07)
    sparkle(d, S * 0.76, S * 0.42, S * 0.033, (*CYAN, 255))
    sparkle(d, S * 0.27, S * 0.37, S * 0.026, (*CYAN, 255))
    return img.resize((size, size), Image.LANCZOS)


def avatar() -> Image.Image:
    return emblem(640).convert("RGB")


def welcome(w: int = 1280, h: int = 720) -> Image.Image:
    W, H = w * SS, h * SS
    img = gradient(W, H, NAVY, TEAL).convert("RGBA")
    img.alpha_composite(glow((W, H), (int(W * 0.85), int(H * 0.15)), int(H * 0.5), CYAN, 90))
    img.alpha_composite(glow((W, H), (int(W * 0.1), int(H * 0.95)), int(H * 0.45), DEEP, 150))

    # Декоративные стеклянные пластины справа
    for box, a in [
        ((0.70, 0.36, 1.00, 0.70), 26),
        ((0.78, 0.54, 1.06, 0.98), 34),
        ((0.63, 0.62, 0.80, 0.90), 22),
    ]:
        x0, y0, x1, y1 = box
        img.alpha_composite(
            glass_pane((W, H), (int(W * x0), int(H * y0), int(W * x1), int(H * y1)),
                       int(H * 0.05), alpha=a)
        )  # fmt: skip

    # Эмблема
    em = emblem(int(h * 0.30)).resize((int(H * 0.30), int(H * 0.30)), Image.LANCZOS)
    mask = Image.new("L", em.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *em.size), int(em.size[0] * 0.22), fill=255)
    img.paste(em, (int(W * 0.06), int(H * 0.09)), mask)

    d = ImageDraw.Draw(img)
    x = int(W * 0.06)
    tx = x + em.size[0] + int(W * 0.025)
    d.text((tx, int(H * 0.11)), "AI-маркетолог", font=font("segoeuib.ttf", 64), fill=WHITE)
    d.text(
        (tx, int(H * 0.25)),
        "Ваш SMM-менеджер, который работает 24/7",
        font=font("segoeui.ttf", 28),
        fill=(*CYAN, 255),
    )

    items = [
        ("Изучает бизнес", "по рассказу, файлам и сайту"),
        ("Пишет посты и рисует картинки", "в стиле вашего бренда"),
        ("Публикует только после «Да»", "кнопки «Опубликовать / Изменить»"),
        ("Работает сам по расписанию", "черновики, публикации, отчёты"),
    ]
    y = int(H * 0.46)
    for title, sub in items:
        sparkle(d, x + 18 * SS, y + 22 * SS, 16 * SS)
        d.text((x + 50 * SS, y), title, font=font("seguisb.ttf", 30), fill=WHITE)
        d.text((x + 50 * SS, y + 40 * SS), sub, font=font("segoeui.ttf", 22), fill=(200, 230, 245))
        y += int(H * 0.125)
    return img.resize((w, h), Image.LANCZOS).convert("RGB")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    avatar().save(OUT / "avatar.png", optimize=True)
    big = welcome()
    big.save(OUT / "welcome.png", optimize=True)
    big.resize((640, 360), Image.LANCZOS).save(OUT / "description.png", optimize=True)
    for name in ("avatar.png", "welcome.png", "description.png"):
        print(OUT / name)


if __name__ == "__main__":
    main()
