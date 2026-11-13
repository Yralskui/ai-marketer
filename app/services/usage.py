"""Учёт расходов на AI по компаниям и месячный лимит.

Как это работает:
- `MeteredLLM` и `MeteredImages` оборачивают провайдеров в `container.py`. Каждый
  вызов записывается в `usage_records` с оценкой стоимости в рублях;
- к какой компании относится вызов, берётся из `usage_scope(session, business_id)`.
  Его открывает `DbSessionMiddleware` на каждое обновление Telegram; фоновые задачи
  оборачивают работу по компании так же. Вне scope вызов только пишется в лог;
- если у компании задан лимит (`ai_budget_rub`) и он исчерпан, обёртка бросает
  `BudgetExceededError` до обращения к модели — деньги не тратятся.

Токены берутся из ответа провайдера (`raw.usage` у OpenAI-совместимых), иначе
оцениваются по длине текста (~3 символа на токен), запись помечается `estimated`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Business, EventType, UsageKind, UsageRecord
from app.db.repositories import EventLogRepo
from app.images.base import ImageProvider
from app.llm.base import LLMError, LLMProvider, LLMResponse, Message
from app.utils.time import to_utc_naive, utcnow

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3
BUDGET_WARNING_SHARE = 0.8
FREE_PROVIDERS = {"mock"}

Alert = Callable[[str], Awaitable[None]]


class BudgetExceededError(LLMError):
    """Месячный лимит расходов компании исчерпан — AI-вызовы заблокированы."""


@dataclass
class UsageScope:
    session: AsyncSession
    business_id: int


_scope: ContextVar[UsageScope | None] = ContextVar("usage_scope", default=None)


@asynccontextmanager
async def usage_scope(session: AsyncSession, business_id: int) -> AsyncIterator[None]:
    """Все AI-вызовы внутри блока относятся к компании `business_id`."""
    token = _scope.set(UsageScope(session, business_id))
    try:
        yield
    finally:
        _scope.reset(token)


# ---------------------------------------------------------------- цены и лимиты


@dataclass(frozen=True)
class Prices:
    input_per_1m: float
    output_per_1m: float
    image: float

    @classmethod
    def from_settings(cls, settings: Settings) -> Prices:
        return cls(
            settings.llm_price_input_per_1m, settings.llm_price_output_per_1m, settings.image_price
        )

    def llm_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.input_per_1m + completion_tokens * self.output_per_1m) / 1e6


def get_budget(business: Business) -> float | None:
    value = (business.settings or {}).get("ai_budget_rub")
    return float(value) if value is not None else None


def set_budget(business: Business, value: float | None) -> None:
    settings = dict(business.settings or {})
    if value is None:
        settings.pop("ai_budget_rub", None)
    else:
        settings["ai_budget_rub"] = round(float(value), 2)
    settings.pop("budget_warned_month", None)
    business.settings = settings  # новый dict — чтобы SQLAlchemy заметил изменение JSON


def month_start_utc(tz: tzinfo, now: datetime | None = None) -> datetime:
    """Начало текущего календарного месяца в часовом поясе компании, в naive UTC."""
    local = (now or datetime.now(tz)).astimezone(tz)
    return to_utc_naive(local.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


# ---------------------------------------------------------------- сводки


@dataclass
class UsageSummary:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    images: int = 0
    cost_rub: float = 0.0
    estimated: bool = False

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


async def usage_summary(
    session: AsyncSession, business_id: int, since_utc: datetime
) -> UsageSummary:
    row = (
        await session.execute(
            select(
                func.count().filter(UsageRecord.kind == UsageKind.LLM),
                func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                func.coalesce(func.sum(UsageRecord.images), 0),
                func.coalesce(func.sum(UsageRecord.cost_rub), 0.0),
                func.coalesce(func.max(UsageRecord.estimated), False),
            ).where(UsageRecord.business_id == business_id, UsageRecord.created_at >= since_utc)
        )
    ).one()
    return UsageSummary(
        llm_calls=int(row[0]),
        prompt_tokens=int(row[1]),
        completion_tokens=int(row[2]),
        images=int(row[3]),
        cost_rub=float(row[4]),
        estimated=bool(row[5]),
    )


async def month_spend(session: AsyncSession, business_id: int, tz: tzinfo) -> float:
    return (await usage_summary(session, business_id, month_start_utc(tz))).cost_rub


# ---------------------------------------------------------------- запись


class UsageRecorder:
    """Пишет расходы в БД текущего scope и следит за лимитом компании."""

    def __init__(self, prices: Prices, tz: tzinfo, alert: Alert | None = None) -> None:
        self.prices = prices
        self.tz = tz
        self.alert = alert

    async def ensure_budget(self) -> None:
        scope = _scope.get()
        if scope is None:
            return
        business = await scope.session.get(Business, scope.business_id)
        budget = get_budget(business) if business else None
        if budget is None:
            return
        spent = await month_spend(scope.session, scope.business_id, self.tz)
        if spent >= budget:
            raise BudgetExceededError(
                f"Лимит расходов на AI для компании исчерпан: {spent:.2f} из {budget:.2f} ₽ "
                "за месяц. Администратор может поднять лимит командой /budget."
            )

    async def record(
        self,
        kind: UsageKind,
        *,
        provider: str,
        model: str = "",
        task: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        images: int = 0,
        estimated: bool = False,
    ) -> float:
        free = provider in FREE_PROVIDERS
        if kind == UsageKind.IMAGE:
            cost = 0.0 if free else images * self.prices.image
        else:
            cost = 0.0 if free else self.prices.llm_cost(prompt_tokens, completion_tokens)
        scope = _scope.get()
        if scope is None:
            log.debug("AI-вызов вне компании: %s %s, %.4f ₽", kind, task, cost)
            return cost
        scope.session.add(
            UsageRecord(
                business_id=scope.business_id,
                kind=kind,
                provider=provider,
                model=model,
                task=task or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                images=images,
                cost_rub=round(cost, 6),
                estimated=estimated,
            )
        )
        await scope.session.flush()
        await self._maybe_warn(scope)
        return cost

    async def _maybe_warn(self, scope: UsageScope) -> None:
        """Один раз в месяц предупреждает администратора о 80% лимита."""
        business = await scope.session.get(Business, scope.business_id)
        budget = get_budget(business) if business else None
        if business is None or budget is None or budget <= 0:
            return
        month = utcnow().strftime("%Y-%m")
        if (business.settings or {}).get("budget_warned_month") == month:
            return
        spent = await month_spend(scope.session, business.id, self.tz)
        if spent < budget * BUDGET_WARNING_SHARE:
            return
        business.settings = {**(business.settings or {}), "budget_warned_month": month}
        text = (
            f"💰 Компания «{business.name}» израсходовала {spent:.2f} ₽ из {budget:.2f} ₽ "
            "лимита на AI в этом месяце."
        )
        await EventLogRepo(scope.session).log(
            EventType.BUDGET_EXCEEDED, text, business_id=business.id
        )
        if self.alert is not None:
            try:
                await self.alert(text)
            except Exception:
                log.exception("Не удалось отправить предупреждение о лимите")


# ---------------------------------------------------------------- обёртки провайдеров


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN) if text else 0


def _prompt_text(messages: list[Message], tools: list[dict[str, Any]] | None) -> str:
    parts = [str(m.get("content") or "") for m in messages]
    if tools:
        parts.append(json.dumps(tools, ensure_ascii=False))
    return "".join(parts)


class MeteredLLM(LLMProvider):
    """LLM-провайдер с учётом расходов и проверкой лимита. Ведёт себя как обёрнутый."""

    def __init__(self, inner: LLMProvider, recorder: UsageRecorder, model: str = "") -> None:
        self.inner = inner
        self.recorder = recorder
        self.model = model
        self.name = inner.name

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        task: str | None = None,
    ) -> LLMResponse:
        await self.recorder.ensure_budget()
        response = await self.inner.chat(
            messages, tools, temperature=temperature, json_mode=json_mode, task=task
        )
        usage = getattr(response.raw, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        estimated = prompt is None or completion is None
        if estimated:
            answer = (response.content or "") + "".join(
                json.dumps(c.arguments, ensure_ascii=False) for c in response.tool_calls
            )
            prompt = _estimate_tokens(_prompt_text(messages, tools))
            completion = _estimate_tokens(answer)
        await self.recorder.record(
            UsageKind.LLM,
            provider=self.inner.name,
            model=self.model if self.inner.name not in FREE_PROVIDERS else "",
            task=task or "",
            prompt_tokens=int(prompt or 0),
            completion_tokens=int(completion or 0),
            estimated=estimated,
        )
        return response


class MeteredImages(ImageProvider):
    """Генератор картинок с учётом расходов и проверкой лимита."""

    def __init__(self, inner: ImageProvider, recorder: UsageRecorder) -> None:
        # super().__init__ не вызываем: папка уже создана обёрнутым провайдером
        self.inner = inner
        self.recorder = recorder
        self.output_dir = inner.output_dir
        self.name = inner.name

    async def generate(self, prompt: str, *, size: tuple[int, int] = (1024, 1024)) -> Path:
        await self.recorder.ensure_budget()
        path = await self.inner.generate(prompt, size=size)
        await self.recorder.record(UsageKind.IMAGE, provider=self.inner.name, images=1)
        return path
