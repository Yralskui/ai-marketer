"""Публичная страница поста: ссылка-карточка для соцсетей, которые не принимают картинку.

ВКонтакте не даёт ключу сообщества загружать фото, зато умеет делать из ссылки карточку:
берёт заголовок, описание и большую картинку из метаданных страницы (Open Graph). Поэтому у
каждого поста может быть публичная страница `/p/<токен>`. Токен подписан секретом бота —
угадать страницу чужого поста нельзя.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from PIL import Image

OG_SIZE = (1200, 630)


def make_token(secret: str, post_id: int) -> str:
    signature = hmac.new(secret.encode(), f"public-post:{post_id}".encode(), hashlib.sha256)
    return f"{post_id}.{signature.hexdigest()[:24]}"


def read_token(secret: str, token: str) -> int | None:
    """id поста по токену или None, если токен чужой или подделан."""
    head, _, signature = token.partition(".")
    if not head.isdecimal() or len(head) > 12 or not signature:
        return None
    expected = make_token(secret, int(head)).partition(".")[2]
    return int(head) if hmac.compare_digest(signature, expected) else None


def split_text(text: str) -> tuple[str, str]:
    """Заголовок (первая строка без хештегов) и описание (остальное) для карточки."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    body = [line for line in lines if not line.startswith("#")]
    title = (body[0] if body else "Пост")[:90]
    return title, " ".join(body[1:])[:200]


def banner(source: Path, target: Path) -> Path:
    """Баннер 1200×630: картинка по центру на фоне её среднего цвета (карточки не обрезаются)."""
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    average = image.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    canvas = Image.new("RGB", OG_SIZE, average)
    image.thumbnail(OG_SIZE)
    canvas.paste(image, ((OG_SIZE[0] - image.width) // 2, (OG_SIZE[1] - image.height) // 2))
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "JPEG", quality=90)
    return target
