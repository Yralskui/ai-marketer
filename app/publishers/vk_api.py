"""Тонкий клиент VK API: ограничение частоты, повторы и загрузка фото на стену.

Ключ доступа — либо ключ сообщества (Управление → Работа с API), либо токен пользователя,
который администрирует сообщество. Запросы идут POST-ом на https://api.vk.com/method/.

Повторы — только там, где они безопасны:
- 6 (слишком много запросов) и 9 (flood control): ВК запрос не выполнил — повторяем всегда;
- 10 (внутренняя ошибка) и сетевые сбои — только для чтения и загрузки файла: запись на стену
  могла дойти, повтор дал бы дубль. Такой сбой превращается в понятную ошибку для человека.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"
DEFAULT_VERSION = "5.199"
# Ключ сообщества — около 3 запросов в секунду; берём с запасом
MIN_INTERVAL = 0.4
MAX_RETRIES = 3
MAX_DELAY = 10.0

RATE_LIMIT_CODES = frozenset({6, 9})
SERVER_ERROR_CODES = frozenset({10})

_HINTS = {
    5: "ключ доступа недействителен или отозван — создайте новый и обновите VK_COMMUNITIES",
    7: "у ключа нет прав на это действие",
    15: "доступ запрещён — проверьте права ключа (стена, фотографии, управление сообществом)",
    27: "этот метод недоступен с ключом сообщества — нужен токен администратора",
    14: "ВКонтакте запросил капчу — попробуйте позже",
    100: "ВКонтакте отклонил параметры запроса",
    203: "нет доступа к сообществу",
    214: "публиковать на стене сейчас нельзя: закрытая стена или исчерпан лимит публикаций",
    219: "рекламная запись запрещена",
    222: "в тексте есть запрещённая ссылка",
    224: "достигнут суточный лимит публикаций ВКонтакте",
}


class VKError(RuntimeError):
    """Ошибка VK API или сети. `code` — error_code ВК, 0 — сбой связи или разбора ответа."""

    def __init__(self, code: int, message: str, *, network: bool = False) -> None:
        hint = _HINTS.get(code)
        super().__init__(f"{message} ({hint})" if hint and hint not in message else message)
        self.code = code
        self.network = network


class VKClient:
    def __init__(
        self,
        token: str,
        *,
        version: str = DEFAULT_VERSION,
        timeout: float = 20.0,
        max_retries: int = MAX_RETRIES,
        base_delay: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self.version = version
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._sleep = sleep
        self._clock = clock
        self._pace_lock = asyncio.Lock()
        self._last_call = 0.0

    async def _wait(self, seconds: float) -> None:
        await (self._sleep or asyncio.sleep)(seconds)

    async def _pace(self) -> None:
        async with self._pace_lock:
            gap = MIN_INTERVAL - (self._clock() - self._last_call)
            if gap > 0:
                await self._wait(gap)
            self._last_call = self._clock()

    async def call(
        self, method: str, params: Mapping[str, Any] | None = None, *, idempotent: bool = False
    ) -> Any:
        """Вызывает метод VK API и возвращает поле `response`."""
        body = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        body.update(access_token=self._token, v=self.version)
        attempt = 0
        while True:
            await self._pace()
            try:
                response = await self._http.post(API_URL + method, data=body)
            except httpx.HTTPError as exc:
                error = VKError(0, f"нет связи с ВКонтакте ({type(exc).__name__})", network=True)
                if idempotent and attempt < self.max_retries:
                    await self._retry(method, attempt, error)
                    attempt += 1
                    continue
                raise error from exc
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if response.status_code >= 500 or not isinstance(payload, dict):
                error = VKError(
                    0, f"ВКонтакте ответил некорректно (HTTP {response.status_code})", network=True
                )
                if idempotent and attempt < self.max_retries:
                    await self._retry(method, attempt, error)
                    attempt += 1
                    continue
                raise error
            if "error" in payload:
                code = int(payload["error"].get("error_code", 0))
                message = str(payload["error"].get("error_msg", "ошибка ВКонтакте"))
                retryable = code in RATE_LIMIT_CODES or (idempotent and code in SERVER_ERROR_CODES)
                if retryable and attempt < self.max_retries:
                    await self._retry(method, attempt, VKError(code, message))
                    attempt += 1
                    continue
                raise VKError(code, message)
            return payload.get("response")

    async def _retry(self, method: str, attempt: int, error: VKError) -> None:
        delay = min(MAX_DELAY, self.base_delay * (2**attempt))
        log.warning("VK %s: %s; повтор %d через %.1f с", method, error, attempt + 1, delay)
        await self._wait(delay)

    async def upload_wall_photo(self, group_id: int, path: Path) -> str:
        """Загружает картинку для записи на стене сообщества и возвращает вложение `photo…`."""
        server = await self.call(
            "photos.getWallUploadServer", {"group_id": group_id}, idempotent=True
        )
        content = await asyncio.to_thread(path.read_bytes)
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        uploaded: Any = None
        for attempt in range(2):  # загрузка файла ничего не публикует — повтор безопасен
            try:
                response = await self._http.post(
                    server["upload_url"], files={"photo": (path.name, content, mime)}
                )
                response.raise_for_status()
                uploaded = response.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                if attempt:
                    raise VKError(0, "не удалось загрузить картинку на сервер ВКонтакте") from exc
                await self._wait(1.0)
        if not isinstance(uploaded, dict) or not uploaded.get("photo"):
            raise VKError(0, "ВКонтакте не принял файл картинки")
        saved = await self.call(
            "photos.saveWallPhoto",
            {
                "group_id": group_id,
                "server": uploaded.get("server"),
                "photo": uploaded["photo"],
                "hash": uploaded.get("hash"),
            },
        )
        if not saved:
            raise VKError(0, "ВКонтакте не сохранил картинку")
        return f"photo{saved[0]['owner_id']}_{saved[0]['id']}"

    async def close(self) -> None:
        await self._http.aclose()


class VKClients:
    """Клиенты по сообществам: у каждого сообщества свой ключ из настроек (VK_COMMUNITIES)."""

    def __init__(
        self,
        tokens: Mapping[int, str],
        *,
        version: str = DEFAULT_VERSION,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.tokens = dict(tokens)
        self._version = version
        self._transport = transport
        self._sleep = sleep
        self._clients: dict[int, VKClient] = {}

    def __bool__(self) -> bool:
        return bool(self.tokens)

    def has(self, group_id: int) -> bool:
        return group_id in self.tokens

    def get(self, group_id: int) -> VKClient:
        if group_id not in self.tokens:
            raise VKError(
                0,
                f"для сообщества {group_id} не задан ключ доступа — добавьте его в VK_COMMUNITIES",
            )
        if group_id not in self._clients:
            self._clients[group_id] = VKClient(
                self.tokens[group_id],
                version=self._version,
                transport=self._transport,
                sleep=self._sleep,
            )
        return self._clients[group_id]

    def any_client(self) -> VKClient:
        """Любой клиент — для запросов, которым не важен ключ (поиск сообщества по адресу)."""
        if not self.tokens:
            raise VKError(0, "ВКонтакте не настроен — задайте VK_COMMUNITIES")
        return self.get(next(iter(self.tokens)))

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
