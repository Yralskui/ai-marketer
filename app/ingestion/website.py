"""Загрузка и очистка текста сайта: главная + несколько внутренних страниц."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urldefrag, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; AIMarketerBot/0.1)"
URL_RE = re.compile(r"https?://[^\s<>\"']+|(?:www\.)[^\s<>\"']+", re.IGNORECASE)
# Внутренние страницы, которые обычно полезны для профиля бизнеса
PRIORITY_HINTS = ("about", "o-nas", "onas", "company", "uslugi", "services", "menu", "catalog",
                  "price", "prices", "ceny", "контакт", "contacts", "о-нас", "услуги", "меню")
SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".pdf", ".zip", ".svg", ".webp", ".mp4")


class WebsiteFetchError(RuntimeError):
    """Сайт недоступен или не содержит текста."""


@dataclass
class PageText:
    url: str
    title: str
    text: str


def find_url(text: str) -> str | None:
    """Первая ссылка в сообщении пользователя."""
    match = URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(").,;")
    return url if url.startswith("http") else f"https://{url}"


async def fetch_site_text(url: str, max_pages: int = 4, timeout: float = 15.0) -> list[PageText]:
    """Главная + до (max_pages-1) внутренних страниц того же домена.

    TODO(поток A): sitemap.xml, приоритизация страниц, кэширование.
    """
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as client:
        try:
            main_html, final_url = await _get(client, url)
        except httpx.HTTPError as exc:
            raise WebsiteFetchError(f"Не удалось открыть {url}: {exc}") from exc

        pages = [await asyncio.to_thread(html_to_page, final_url, main_html)]
        for link in pick_internal_links(final_url, main_html, limit=max_pages - 1):
            try:
                html, real_url = await _get(client, link)
            except httpx.HTTPError as exc:
                log.info("Пропускаю %s: %s", link, exc)
                continue
            pages.append(await asyncio.to_thread(html_to_page, real_url, html))

    pages = [p for p in pages if p.text]
    if not pages:
        raise WebsiteFetchError("На сайте не нашлось текста (возможно, он рисуется JavaScript-ом)")
    return pages


async def _get(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    resp = await client.get(url)
    resp.raise_for_status()
    if "html" not in resp.headers.get("content-type", "html"):
        raise httpx.HTTPError(f"не HTML: {resp.headers.get('content-type')}")
    return resp.text, str(resp.url)


def html_to_page(url: str, html: str) -> PageText:
    """Основной текст страницы: trafilatura, фолбэк — BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title else ""
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    except Exception as exc:  # trafilatura иногда падает на экзотической разметке
        log.debug("trafilatura не справилась с %s: %s", url, exc)
    if len(text) < 200:
        for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
            tag.decompose()
        fallback = " ".join(soup.get_text(" ").split())
        if len(fallback) > len(text):
            text = fallback
    return PageText(url=url, title=title, text=text.strip())


def pick_internal_links(base_url: str, html: str, limit: int) -> list[str]:
    """Внутренние ссылки того же домена; сначала «о нас», «услуги», «меню»..."""
    if limit <= 0:
        return []
    domain = urlparse(base_url).netloc.removeprefix("www.")
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = {urldefrag(base_url)[0].rstrip("/")}
    links: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(base_url, a["href"]))[0].rstrip("/")
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.removeprefix("www.") != domain or href in seen:
            continue
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):
            continue
        seen.add(href)
        haystack = (parsed.path + " " + a.get_text(" ")).lower()
        priority = 0 if any(h in haystack for h in PRIORITY_HINTS) else 1
        links.append((priority, href))
    links.sort(key=lambda item: item[0])
    return [href for _, href in links[:limit]]


def pages_to_text(pages: list[PageText]) -> str:
    return "\n\n".join(f"## {p.title or p.url}\n{p.url}\n{p.text}" for p in pages)
