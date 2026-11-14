"""Рисует аватарку бота и приветственные картинки в фирменном стиле.

Стиль — по брендбуку АО «Салаватстекло» (15.11.2023):
- основные цвета: Petrol #196C7F (Pantone 3155 C), #285055 (2216 C), #C1D0D3 (5513 C),
  #DDE1E1 (7541 C);
- фирменный паттерн — круги и скруглённые формы, только в основных цветах;
- шрифт: Segoe UI Black (заголовки) и Segoe UI (текст) — разрешённая замена основной пары
  Intro Black / Avenir Next Cyr.
Логотип и товарный знак не используются: по брендбуку это требует согласования.

Запуск из корня проекта:  python scripts/make_brand_assets.py
Результат — app/bot/assets/: avatar.png (640x640), welcome.png (1280x720),
description.png (640x360, для BotFather → Edit Description Picture).
Нужны шрифты Segoe UI (есть в Windows).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "app" / "bot" / "assets"
FONTS = Path("C:/Windows/Fonts")
SS = 3  # суперсэмплинг для сглаживания

PETROL = (25, 108, 127)  # #196C7F — основной
DARK = (40, 80, 85)  # #285055
MIST = (193, 208, 211)  # #C1D0D3
LIGHT = (221, 225, 225)  # #DDE1E1
WHITE = (255, 255, 255)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size * SS)


def circle(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, **kw) -> None:
    d.ellipse((cx - r, cy - r, cx + r, cy + r), **kw)


def capsule(d: ImageDraw.ImageDraw, x0: float, y0: float, x1: float, y1: float, **kw) -> None:
    """Скруглённая «таблетка» — элемент фирменного паттерна."""
    radius = min(x1 - x0, y1 - y0) / 2
    d.rounded_rectangle((x0, y0, x1, y1), radius, **kw)


def sparkle(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color=WHITE) -> None:
    """Четырёхлучевая искра — знак AI."""
    k = r * 0.24
    d.polygon(
        [
            (cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
            (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k),
        ],
        fill=color,
    )  # fmt: skip


def pattern(d: ImageDraw.ImageDraw, ox: float, oy: float, u: float) -> None:
    """Композиция из кругов и «таблеток» на сетке 3×3 с шагом u (брендбук, стр. 30–31).

    На фоне Petrol используем остальные основные цвета, чтобы фигуры не сливались.
    """
    line = int(u * 0.07)
    gap = u * 0.04
    circle(d, ox + 0.5 * u, oy + 0.5 * u, u / 2 - gap, outline=WHITE, width=line)
    capsule(d, ox + u + gap, oy + gap, ox + 3 * u - gap, oy + u - gap, fill=MIST)
    capsule(d, ox + gap, oy + u + gap, ox + u - gap, oy + 3 * u - gap, fill=DARK)
    circle(d, ox + 1.5 * u, oy + 1.5 * u, u / 2 - gap, fill=LIGHT)
    circle(d, ox + 2.5 * u, oy + 1.5 * u, u / 2 - gap, outline=MIST, width=line)
    capsule(d, ox + u + gap, oy + 2 * u + gap, ox + 3 * u - gap, oy + 3 * u - gap, fill=LIGHT)


def avatar(size: int = 640) -> Image.Image:
    """Аватарка: фирменный Petrol, круги и «таблетка», искра AI в центре кольца."""
    S = size * SS
    img = Image.new("RGB", (S, S), PETROL)
    d = ImageDraw.Draw(img)
    u = S * 0.2  # шаг сетки; композиция 3u×3u по центру — безопасно для круглой маски
    ox = oy = (S - 3 * u) / 2
    capsule(d, ox, oy, ox + u, oy + 2 * u, fill=DARK)
    circle(d, ox + 2 * u, oy + 0.5 * u, u / 2, fill=LIGHT)
    capsule(d, ox + u, oy + u, ox + 3 * u, oy + 2 * u, fill=MIST)
    circle(d, ox + 0.5 * u, oy + 2.5 * u, u / 2, fill=LIGHT)
    ring_cx, ring_cy, ring_r = ox + 2 * u, oy + 2.5 * u, u / 2 - u * 0.05
    circle(d, ring_cx, ring_cy, ring_r, outline=WHITE, width=int(u * 0.09))
    sparkle(d, ring_cx, ring_cy, u * 0.26)
    return img.resize((size, size), Image.LANCZOS)


def welcome(w: int = 1280, h: int = 720) -> Image.Image:
    W, H = w * SS, h * SS
    img = Image.new("RGB", (W, H), PETROL)
    d = ImageDraw.Draw(img)

    # Паттерн справа, уходит за край — как на обложках брендбука
    pattern(d, W * 0.64, H * 0.14, H * 0.24)

    x = int(W * 0.06)
    d.text((x, int(H * 0.12)), "AI-маркетолог", font=font("seguibl.ttf", 70), fill=WHITE)
    d.text(
        (x, int(H * 0.27)),
        "SMM-менеджер, который работает 24/7",
        font=font("segoeui.ttf", 30),
        fill=LIGHT,
    )
    items = [
        ("Изучает бизнес", "по рассказу, файлам, сайту и вашим фото"),
        ("Пишет посты и готовит картинки", "в тоне вашего бренда"),
        ("Публикует только после «Да»", "черновик с кнопками — на согласование"),
        ("Работает сам по расписанию", "публикации, ответы на комментарии, отчёты"),
    ]
    y = int(H * 0.45)
    for title, sub in items:
        circle(d, x + 14 * SS, y + 21 * SS, 9 * SS, fill=LIGHT)
        d.text((x + 44 * SS, y), title, font=font("seguisb.ttf", 30), fill=WHITE)
        d.text((x + 44 * SS, y + 40 * SS), sub, font=font("segoeui.ttf", 22), fill=MIST)
        y += int(H * 0.125)
    return img.resize((w, h), Image.LANCZOS)


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
