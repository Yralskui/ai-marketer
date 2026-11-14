"""Общие маршруты к OpenAI для всех клиентов: прокси, смена маршрута и повторы «до конца».

С сервера в РФ OpenAI доступен только через прокси (LLM_PROXY). Чат, воркер знаний,
эмбеддинги и картинки создают клиентов здесь, чтобы ни один запрос не пошёл в обход прокси
и не упал с первого сетевого сбоя.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
import openai
from openai import AsyncOpenAI

from app.config import Settings
from app.llm.base import LLMError
from app.llm.openai_provider import _RETRYABLE, _retry_after
from app.utils.retry import retry_with_budget

T = TypeVar("T")


def openai_clients(
    settings: Settings,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
) -> list[AsyncOpenAI]:
    """По клиенту на каждый прокси; без LLM_PROXY — один прямой клиент."""
    timeout = timeout or settings.llm_timeout
    return [
        AsyncOpenAI(
            api_key=api_key or settings.llm_api_key,
            base_url=base_url or settings.llm_base_url or None,
            timeout=timeout,
            # Повторы делает call_with_routes, встроенные выключены
            max_retries=0,
            http_client=httpx.AsyncClient(proxy=proxy, timeout=timeout) if proxy else None,
        )
        for proxy in settings.llm_proxies or [None]
    ]


async def call_with_routes(
    clients: list[AsyncOpenAI],
    request: Callable[[AsyncOpenAI], Awaitable[T]],
    *,
    budget_seconds: float,
) -> T:
    """Повторяет временные ошибки до конца бюджета, каждый раз через следующий маршрут."""
    try:
        return await retry_with_budget(
            lambda attempt: request(clients[attempt % len(clients)]),
            budget_seconds=budget_seconds,
            retry_on=_RETRYABLE,
            delay_for=_retry_after,
        )
    except openai.PermissionDeniedError as exc:
        raise LLMError(
            "OpenAI отказал в доступе (403). С сервера в РФ нужен прокси — проверьте LLM_PROXY."
        ) from exc
    except openai.AuthenticationError as exc:
        raise LLMError("Неверный ключ OpenAI (LLM_API_KEY).") from exc
    except _RETRYABLE as exc:
        raise LLMError(
            f"AI сейчас недоступен: связь не восстановилась за {budget_seconds:.0f} с."
        ) from exc


async def close_clients(clients: list[AsyncOpenAI]) -> None:
    for client in clients:
        await client.close()
