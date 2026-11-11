"""Провайдер для OpenAI и любых OpenAI-совместимых API."""

from __future__ import annotations

import json
import logging
from typing import Any

import openai
from openai import AsyncOpenAI

from app.llm.base import LLMError, LLMProvider, LLMResponse, Message, ToolCall
from app.utils.retry import retry_async

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
    ) -> None:
        if not api_key:
            raise ValueError("LLM_API_KEY не задан, а LLM_PROVIDER=openai")
        # Повторы делаем сами (retry_async), встроенные выключаем
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
        self.model = model
        self.max_retries = max_retries
        self.temperature = temperature

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
        try:
            resp = await retry_async(
                lambda: self.client.chat.completions.create(**kwargs),
                retries=self.max_retries,
                retry_on=_RETRYABLE,
            )
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
                log.warning("Не-JSON аргументы инструмента %s: %r", tc.function.name, tc.function.arguments)
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return LLMResponse(content=msg.content, tool_calls=tool_calls, raw=resp)
