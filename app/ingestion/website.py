"""Загрузка и очистка текста сайта: главная + несколько внутренних страниц."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver
from bs4 import BeautifulSoup
from yarl import URL

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; AIMarketerBot/0.1)"
URL_RE = re.compile(r"https?://[^\s<>\"']+|(?:www\.)[^\s<>\"']+", re.IGNORECASE)
# Внутренние страницы, которые обычно полезны для профиля бизнеса
PRIORITY_HINTS = (
    "about",
    "o-nas",
    "onas",
    "company",
    "uslugi",
    "services",
    "menu",
    "catalog",
    "price",
    "prices",
    "ceny",
    "контакт",
    "contacts",
    "о-нас",
    "услуги",
    "меню",
)
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


async def fetch_site_text(
    url: str, max_pages: int = 4, request_timeout: float = 15.0
) -> list[PageText]:
    """Главная + до (max_pages-1) внутренних страниц того же домена.

    TODO(поток A): sitemap.xml, приоритизация страниц, кэширование.
    """
    async with website_client(request_timeout) as client:
        try:
            main_html, final_url = await _get(client, url)
        except (ClientError, TimeoutError) as exc:
            raise WebsiteFetchError("Не удалось открыть сайт") from exc
        pages = [await asyncio.to_thread(html_to_page, final_url, main_html)]
        for link in pick_internal_links(final_url, main_html, limit=max_pages - 1):
            try:
                html, real_url = await _get(client, link)
            except (ClientError, TimeoutError, WebsiteFetchError):
                log.info("Не удалось прочитать внутреннюю страницу сайта")
                continue
            pages.append(await asyncio.to_thread(html_to_page, real_url, html))

    pages = [p for p in pages if p.text]
    if not pages:
        raise WebsiteFetchError("На сайте не нашлось текста (возможно, он рисуется JavaScript-ом)")
    return pages


@asynccontextmanager
async def website_client(request_timeout: float):
    resolver = PublicResolver()
    try:
        async with ClientSession(
            connector=TCPConnector(resolver=resolver, use_dns_cache=False),
            timeout=ClientTimeout(total=request_timeout),
            headers={"User-Agent": USER_AGENT},
            trust_env=False,
        ) as client:
            yield client
    finally:
        await resolver.close()


def public_url(url: str) -> str:
    try:
        normalized = str(URL(url))
        parsed = urlparse(normalized)
        port = parsed.port
    except ValueError as exc:
        raise WebsiteFetchError("Укажите корректный адрес сайта") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80 if parsed.scheme == "http" else 443}
        or "%" in parsed.hostname
    ):
        raise WebsiteFetchError("Укажите публичный адрес сайта HTTP или HTTPS")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(parsed.hostname))
        except OSError:
            return normalized
    if not is_public_ip(address):
        raise WebsiteFetchError("Сайт должен быть доступен из интернета")
    return normalized


def is_public_ip(address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public_ip(address.ipv4_mapped)
        if address.sixtofour is not None or address.teredo is not None:
            return False
    return address.is_global and not address.is_multicast and not address.is_reserved


class PublicResolver(AbstractResolver):
    """Проверяет адреса, которые HTTP-клиент использует при подключении."""

    def __init__(self) -> None:
        self.inner = ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        records = await self.inner.resolve(host, port, family)
        public = [
            record for record in records if is_public_ip(ipaddress.ip_address(record["host"]))
        ]
        if not public:
            raise OSError("Сайт должен быть доступен из интернета")
        return public

    async def close(self) -> None:
        await self.inner.close()


async def _get(client: ClientSession, url: str) -> tuple[str, str]:
    for _ in range(4):
        async with client.get(public_url(url), allow_redirects=False) as response:
            if response.status in {301, 302, 303, 307, 308} and response.headers.get("Location"):
                url = urljoin(str(response.url), response.headers["Location"])
                continue
            response.raise_for_status()
            if response.content_type not in {"text/html", "application/xhtml+xml"}:
                raise WebsiteFetchError("Страница не в формате HTML")
            data = bytearray()
            async for part in response.content.iter_chunked(65536):
                data.extend(part)
                if len(data) > 2_000_000:
                    raise WebsiteFetchError("HTML-страница превышает 2 МБ")
            try:
                html = data.decode(response.charset or "utf-8", errors="replace")
            except LookupError:
                html = data.decode("utf-8", errors="replace")
            return html, str(response.url)
    raise WebsiteFetchError("Слишком много перенаправлений")


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
