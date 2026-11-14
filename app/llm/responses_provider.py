"""Responses API для моделей, которым он нужен для инструментов."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from openai import OpenAIError

from app.config import Settings
from app.llm.base import LLMError, LLMProvider, LLMResponse, Message, ToolCall
from app.llm.routes import call_with_routes, openai_clients


class ResponsesProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY не задан")
        self.model = settings.llm_model_light or settings.llm_model
        # Через LLM_PROXY и с повторами, как у OpenAIProvider
        self.clients = openai_clients(settings)
        self.client = self.clients[0]
        self.retry_budget = settings.llm_retry_budget_seconds

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        task: str | None = None,
    ) -> LLMResponse:
        items: list[dict] = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message["tool_call_id"],
                        "output": message["content"],
                    }
                )
                continue
            if message.get("content"):
                items.append({"role": role, "content": message["content"]})
            for call in message.get("tool_calls", ()):
                function = call["function"]
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": function["name"],
                        "arguments": function["arguments"],
                    }
                )
        options: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "reasoning": {"effort": "low"},
            "store": False,
        }
        if tools:
            options["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
        if json_mode:
            options["text"] = {"format": {"type": "json_object"}}
        try:
            response = await call_with_routes(
                self.clients,
                lambda client: client.responses.create(**options),
                budget_seconds=self.retry_budget,
            )
            calls = [
                ToolCall(item.call_id, item.name, json.loads(item.arguments))
                for item in response.output
                if item.type == "function_call"
            ]
        except (OpenAIError, ValueError) as exc:
            raise LLMError("Не удалось получить ответ OpenAI") from exc
        usage = response.usage
        raw = (
            SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=usage.input_tokens, completion_tokens=usage.output_tokens
                )
            )
            if usage
            else None
        )
        return LLMResponse(response.output_text, calls, raw)
