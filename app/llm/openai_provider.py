"""Провайдер для OpenAI и любых OpenAI-совместимых API.

Устойчивость к нестабильной сети (сервер в РФ, OpenAI — только через прокси):
- `proxies` — один или несколько прокси; при обрыве или таймауте следующая попытка идёт
  через следующий прокси;
- временные ошибки (сеть, таймаут, 429, 5xx) повторяются, пока не выйдет бюджет времени
  `retry_budget` — запрос «стучится до конца», а не падает с первого сбоя;
- постоянные ошибки (неверный ключ, плохой запрос, 403 без прокси) не повторяются.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from app.llm.base import LLMError, LLMProvider, LLMResponse, Message, ToolCall
from app.utils.retry import retry_with_budget

log = logging.getLogger(__name__)

_RETRYABLE = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        temperature: float = 0.7,
        proxies: list[str] | None = None,
        retry_budget: float = 180.0,
    ) -> None:
        if not api_key:
            raise ValueError("LLM_API_KEY не задан, а LLM_PROVIDER=openai")
        # Повторы делаем сами (retry_with_budget), встроенные выключаем.
        # По клиенту на каждый прокси; без прокси — один прямой клиент.
        routes = proxies or [None]
        self.clients = [
            AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=0,
                http_client=httpx.AsyncClient(proxy=proxy, timeout=timeout) if proxy else None,
            )
            for proxy in routes
        ]
        self.client = self.clients[0]
        self.model = model
        self.max_retries = max_retries
        self.temperature = temperature
        self.retry_budget = retry_budget

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        task: str | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if tools:
            kwargs["tools"] = tools
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        log.debug("LLM запрос task=%s, сообщений=%d", task, len(messages))

        async def attempt(n: int) -> Any:
            # Смена маршрута на каждой попытке: если прокси залагал, идём через другой
            client = self.clients[n % len(self.clients)]
            return await client.chat.completions.create(**kwargs)

        try:
            resp = await retry_with_budget(
                attempt,
                budget_seconds=self.retry_budget,
                retry_on=_RETRYABLE,
                delay_for=_retry_after,
            )
        except openai.PermissionDeniedError as exc:
            raise LLMError(
                "LLM отказала в доступе (403). С сервера в РФ OpenAI доступен только через "
                "прокси — проверьте LLM_PROXY."
            ) from exc
        except openai.AuthenticationError as exc:
            raise LLMError("Неверный ключ LLM (LLM_API_KEY).") from exc
        except _RETRYABLE as exc:
            raise LLMError(
                "AI сейчас недоступен: связь не восстановилась за "
                f"{self.retry_budget:.0f} с. Попробуйте ещё раз позже."
            ) from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"Ошибка LLM: {exc}") from exc

        if not resp.choices:
            raise LLMError("LLM вернула пустой ответ")
        msg = resp.choices[0].message
        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                log.warning(
                    "Не-JSON аргументы инструмента %s: %r", tc.function.name, tc.function.arguments
                )
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return LLMResponse(content=msg.content, tool_calls=tool_calls, raw=resp)


def _retry_after(exc: BaseException) -> float | None:
    """Пауза из заголовка Retry-After (429/503), если сервер её подсказал."""
    response = getattr(exc, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return min(float(value), 60.0) if value else None
    except ValueError:
        return None
