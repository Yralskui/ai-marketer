"""Учёт расходов на AI и месячный лимит компании (п. 2 и 5 ТЗ: контроль расходов)."""

from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import app.agent.tools  # noqa: F401  — регистрация инструментов
from app.agent.context import AgentContext
from app.agent.core import Agent, AgentError
from app.llm.base import LLMProvider, LLMResponse
from app.llm.mock_provider import MockLLMProvider
from app.services import accounts, usage
from app.services.usage import (
    BudgetExceededError,
    MeteredImages,
    MeteredLLM,
    Prices,
    UsageRecorder,
    usage_scope,
)

TZ = ZoneInfo("Asia/Yekaterinburg")
PRICES = Prices(input_per_1m=100.0, output_per_1m=400.0, image=5.0)


class PaidLLM(LLMProvider):
    """Платный провайдер: возвращает точное число токенов, как OpenAI."""

    name = "openai"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls += 1
        raw = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))
        return LLMResponse(content="ответ", raw=raw)


def metered(inner: LLMProvider, alerts: list[str] | None = None) -> MeteredLLM:
    async def alert(text: str) -> None:
        if alerts is not None:
            alerts.append(text)

    return MeteredLLM(inner, UsageRecorder(PRICES, TZ, alert=alert), model="gpt-4o-mini")


async def test_paid_call_is_recorded_with_exact_tokens(session, owner):
    _, business = owner
    llm = metered(PaidLLM())
    async with usage_scope(session, business.id):
        await llm.complete("system", "user", task="write_post")

    summary = await usage.usage_summary(session, business.id, usage.month_start_utc(TZ))
    assert summary.llm_calls == 1
    assert (summary.prompt_tokens, summary.completion_tokens) == (1000, 500)
    assert summary.cost_rub == pytest.approx((1000 * 100 + 500 * 400) / 1e6)
    assert not summary.estimated


async def test_mock_is_free_but_tokens_are_estimated(session, owner):
    _, business = owner
    llm = metered(MockLLMProvider())
    async with usage_scope(session, business.id):
        await llm.complete("Ты маркетолог", "Тема: осеннее меню", task="write_post")

    summary = await usage.usage_summary(session, business.id, usage.month_start_utc(TZ))
    assert summary.llm_calls == 1 and summary.tokens > 0
    assert summary.cost_rub == 0 and summary.estimated


async def test_images_are_metered(session, owner, images):
    _, business = owner

    class PaidImages(type(images)):
        name = "openai-images"

    paid = PaidImages(images.output_dir)
    metered_images = MeteredImages(paid, UsageRecorder(PRICES, TZ))
    async with usage_scope(session, business.id):
        path = await metered_images.generate("кофе и круассан")
    assert path.exists()

    summary = await usage.usage_summary(session, business.id, usage.month_start_utc(TZ))
    assert summary.images == 1 and summary.cost_rub == pytest.approx(5.0)


async def test_budget_blocks_calls_before_spending(session, owner):
    _, business = owner
    inner = PaidLLM()
    llm = metered(inner)
    usage.set_budget(business, 0.2)  # одного вызова (0.3 ₽) хватит, чтобы исчерпать

    async with usage_scope(session, business.id):
        await llm.complete("s", "u")
        with pytest.raises(BudgetExceededError, match="/budget"):
            await llm.complete("s", "u")
    assert inner.calls == 1  # второй запрос к модели не ушёл


async def test_budget_warning_sent_once(session, owner):
    _, business = owner
    alerts: list[str] = []
    llm = metered(PaidLLM(), alerts)
    usage.set_budget(business, 1.0)  # 80% = 0.8 ₽, вызов стоит 0.3 ₽

    async with usage_scope(session, business.id):
        for _ in range(3):
            await llm.complete("s", "u")
    assert len(alerts) == 1 and "из 1.00 ₽" in alerts[0]


async def test_budget_is_per_company(session, settings, owner):
    admin, company_a = owner
    company_b = await accounts.create_company(session, admin, "Вторая")
    llm = metered(PaidLLM())
    usage.set_budget(company_a, 0.1)

    async with usage_scope(session, company_a.id):
        await llm.complete("s", "u")
    async with usage_scope(session, company_b.id):
        await llm.complete("s", "u")  # у B лимита нет — работает
        await llm.complete("s", "u")

    since = usage.month_start_utc(TZ)
    assert (await usage.usage_summary(session, company_a.id, since)).llm_calls == 1
    assert (await usage.usage_summary(session, company_b.id, since)).llm_calls == 2


async def test_agent_stops_with_budget_error(session, owner, settings, images, notifier):
    user, business = owner
    usage.set_budget(business, 0.2)
    llm = metered(PaidLLM())
    async with usage_scope(session, business.id):
        await llm.complete("s", "u")  # исчерпали лимит

        ctx = AgentContext(
            session=session,
            user=user,
            business=business,
            settings=settings,
            llm=llm,
            images=images,
            notifier=notifier,
        )
        with pytest.raises(AgentError) as exc_info:
            await Agent().run(ctx, "Напиши пост про кофе")
    assert isinstance(exc_info.value.__cause__, BudgetExceededError)


async def test_outside_scope_nothing_is_written(session, owner):
    _, business = owner
    await metered(PaidLLM()).complete("s", "u")
    summary = await usage.usage_summary(session, business.id, usage.month_start_utc(TZ))
    assert summary.llm_calls == 0
