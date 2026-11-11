"""Цикл агента: LLM -> вызовы инструментов -> результаты -> LLM ... (с лимитом шагов)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agent.context import AgentContext
from app.agent.prompts import render_prompt
from app.agent.registry import ToolRegistry, default_registry
from app.db.models import AgentMessage, MessageRole
from app.db.repositories import AgentMessageRepo
from app.llm.base import LLMError, Message
from app.services.business_profile import profile_to_text

log = logging.getLogger(__name__)

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

STEP_LIMIT_ANSWER = (
    "Я выполнил несколько шагов, но не успел закончить. Уточните, пожалуйста, задачу."
)


class AgentError(RuntimeError):
    """Агент не смог ответить (LLM недоступна, таймаут)."""


@dataclass
class AgentResult:
    text: str
    steps: int
    tool_calls: list[str] = field(default_factory=list)
    created_post_ids: list[int] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        max_steps: int = 6,
        history_limit: int = 20,
        step_timeout: float = 90.0,
    ) -> None:
        self.registry = registry or default_registry
        self.max_steps = max_steps
        self.history_limit = history_limit
        self.step_timeout = step_timeout

    async def run(self, ctx: AgentContext, user_text: str) -> AgentResult:
        repo = AgentMessageRepo(ctx.session)
        history = await repo.last_n(ctx.user_id, self.history_limit)

        messages: list[Message] = [{"role": "system", "content": self._system_prompt(ctx)}]
        messages += _history_to_messages(history)
        messages.append({"role": "user", "content": user_text})
        await repo.add(ctx.user_id, MessageRole.USER, user_text, business_id=ctx.business_id)

        tools = self.registry.schemas()
        called: list[str] = []
        for step in range(1, self.max_steps + 1):
            try:
                response = await asyncio.wait_for(
                    ctx.llm.chat(messages, tools=tools, task="agent"), timeout=self.step_timeout
                )
            except (TimeoutError, LLMError) as exc:
                log.exception("Агент: ошибка LLM на шаге %d", step)
                raise AgentError(str(exc)) from exc

            if not response.tool_calls:
                text = (response.content or "").strip() or "Готово."
                await repo.add(ctx.user_id, MessageRole.ASSISTANT, text, business_id=ctx.business_id)
                return AgentResult(text, step, called, list(ctx.created_post_ids))

            # Ассистент попросил вызвать инструменты
            calls_payload = [tc.to_openai() for tc in response.tool_calls]
            messages.append(
                {"role": "assistant", "content": response.content, "tool_calls": calls_payload}
            )
            await repo.add(
                ctx.user_id,
                MessageRole.ASSISTANT,
                response.content,
                business_id=ctx.business_id,
                tool_calls=calls_payload,
            )
            for tc in response.tool_calls:
                log.info("Агент вызывает %s(%s)", tc.name, tc.arguments)
                called.append(tc.name)
                result = await self.registry.call(tc.name, tc.arguments, ctx)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                await repo.add(
                    ctx.user_id,
                    MessageRole.TOOL,
                    result,
                    business_id=ctx.business_id,
                    tool_call_id=tc.id,
                    name=tc.name,
                )

        await repo.add(
            ctx.user_id, MessageRole.ASSISTANT, STEP_LIMIT_ANSWER, business_id=ctx.business_id
        )
        return AgentResult(STEP_LIMIT_ANSWER, self.max_steps, called, list(ctx.created_post_ids))

    def _system_prompt(self, ctx: AgentContext) -> str:
        now = datetime.now(ctx.settings.tz)
        return render_prompt(
            "system",
            now=f"{WEEKDAYS[now.weekday()]}, {now:%d.%m.%Y %H:%M}",
            timezone=ctx.settings.timezone,
            profile=profile_to_text(ctx.business),
        )


def _history_to_messages(history: list[AgentMessage]) -> list[Message]:
    """История из БД -> сообщения OpenAI.

    Окно может начаться посреди цепочки assistant(tool_calls) -> tool: такие обрывки
    модель не примет, поэтому начинаем с первого сообщения пользователя.
    """
    start = next((i for i, m in enumerate(history) if m.role == MessageRole.USER), len(history))
    result: list[Message] = []
    for m in history[start:]:
        msg: dict[str, Any] = {"role": m.role.value, "content": m.content}
        if m.role == MessageRole.ASSISTANT and m.tool_calls:
            msg["tool_calls"] = m.tool_calls
        if m.role == MessageRole.TOOL:
            msg["tool_call_id"] = m.tool_call_id
        result.append(msg)
    return result
