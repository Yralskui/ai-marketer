"""Интерфейс LLM-провайдера.

⚠️ Стабильный интерфейс: меняется только по согласованию с напарником.

Формат сообщений — OpenAI Chat Completions (лингва франка для всех провайдеров):
    {"role": "system" | "user" | "assistant" | "tool", "content": "...",
     "tool_calls": [...], "tool_call_id": "..."}
Формат инструментов — OpenAI function tools:
    {"type": "function", "function": {"name", "description", "parameters"}}
Провайдер с другим форматом конвертирует сообщения внутри адаптера.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]


class LLMError(RuntimeError):
    """LLM недоступна или вернула мусор (после всех повторов)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        import json

        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None


class LLMProvider(ABC):
    """Базовый класс провайдера. Новый провайдер = новый файл + строка в factory.py."""

    name: str = "base"

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        task: str | None = None,
    ) -> LLMResponse:
        """Один запрос к модели.

        task — подсказка о типе задачи ("agent", "write_post", "revise_post",
        "extract_profile", ...). Реальные провайдеры используют её только для логов,
        mock-провайдер — чтобы выбрать шаблон ответа.
        json_mode — попросить модель вернуть JSON-объект.
        """

    async def complete(
        self, system: str, user: str, *, json_mode: bool = False, task: str | None = None
    ) -> str:
        """Удобная обёртка: один system + один user, без инструментов."""
        response = await self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=json_mode,
            task=task,
        )
        return (response.content or "").strip()
