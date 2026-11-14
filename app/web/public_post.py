"""Публичная страница поста `/p/<токен>` и её баннер: из них соцсети собирают карточку-ссылку."""

from __future__ import annotations

import asyncio
import logging
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from app.config import IMAGES_DIR
from app.db.models import Business, Post
from app.services import public_posts

if TYPE_CHECKING:
    from app.container import AppContainer

log = logging.getLogger(__name__)

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex",
    "Referrer-Policy": "no-referrer",
}

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta property="og:type" content="article">
<meta property="og:site_name" content="{company}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="{url}">
{image_meta}<style>
body{{margin:0;background:#eef1f6;color:#131d30;font:16px/1.55 system-ui,sans-serif}}
main{{max-width:640px;margin:0 auto;background:#fff;min-height:100vh}}
img{{display:block;width:100%;height:auto}}
article{{padding:20px 22px 32px}}
h1{{margin:0 0 12px;font-size:1.35rem;line-height:1.25}}
p{{margin:0 0 12px;white-space:pre-line}}
.by{{color:#526178;font-size:.85rem}}
</style></head><body><main>{image_tag}<article>
<h1>{title}</h1><p>{text}</p><p class="by">{company}</p></article></main></body></html>
"""


class PublicPosts:
    def __init__(self, container: AppContainer) -> None:
        self.container = container
        self.secret = container.settings.bot_token

    def register(self, app: web.Application) -> None:
        if not self.secret:  # без секрета подписать ссылки нечем
            return
        app.router.add_get(r"/p/{token}", self.page)
        app.router.add_get(r"/p/{token}/card.jpg", self.card)

    async def _load(self, token: str) -> tuple[Post, Business] | None:
        post_id = public_posts.read_token(self.secret, token)
        if post_id is None:
            return None
        async with self.container.db.sessionmaker() as session:
            post = await session.get(Post, post_id)
            business = await session.get(Business, post.business_id) if post else None
        return (post, business) if post and business else None

    @staticmethod
    def _image(post: Post) -> Path | None:
        if not post.image_path:
            return None
        path = Path(post.image_path).resolve()
        if not path.is_relative_to(IMAGES_DIR.resolve()) or not path.is_file():
            return None
        return path

    @staticmethod
    def _log(request: web.Request, what: str) -> None:
        """Кто и что запрашивает: так видно, заходит ли сканер соцсети за карточкой."""
        agent = request.headers.get("User-Agent", "-")[:90]
        log.info("Публичный пост, %s: %s | %s", what, request.path.split("/")[-1][:12], agent)

    async def page(self, request: web.Request) -> web.Response:
        self._log(request, "страница")
        token = request.match_info["token"]
        loaded = await self._load(token)
        if loaded is None:
            raise web.HTTPNotFound
        post, business = loaded
        base = self.container.settings.miniapp_public_url.rstrip("/")
        title, description = public_posts.split_text(post.text)
        has_image = self._image(post) is not None
        image_url = f"{base}/p/{token}/card.jpg"
        html = PAGE.format(
            title=escape(title),
            description=escape(description),
            company=escape(business.name),
            url=escape(f"{base}/p/{token}"),
            text=escape("\n".join(line for line in post.text.splitlines() if line.strip())),
            image_meta=(
                f'<meta property="og:image" content="{escape(image_url)}">\n'
                '<meta property="og:image:width" content="1200">\n'
                '<meta property="og:image:height" content="630">\n'
                if has_image
                else ""
            ),
            image_tag=f'<img src="/p/{escape(token)}/card.jpg" alt="">' if has_image else "",
        )
        return web.Response(text=html, content_type="text/html", headers=HEADERS)

    async def card(self, request: web.Request) -> web.StreamResponse:
        self._log(request, "баннер")
        loaded = await self._load(request.match_info["token"])
        source = self._image(loaded[0]) if loaded else None
        if loaded is None or source is None:
            raise web.HTTPNotFound
        target = IMAGES_DIR / "og" / f"{loaded[0].id}.jpg"
        path = await asyncio.to_thread(public_posts.banner, source, target)
        return web.FileResponse(path, headers={**HEADERS, "Cache-Control": "public, max-age=86400"})
