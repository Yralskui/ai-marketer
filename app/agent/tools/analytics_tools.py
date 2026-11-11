"""Инструменты: аналитика и отчёты (поток B). Пока — «в разработке»."""

from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.agent.registry import tool
from app.services import analytics, reports


class PeriodArgs(BaseModel):
    days: int = Field(7, ge=1, le=90, description="За сколько последних дней")


@tool("get_analytics", "Показать статистику публикаций: охваты, реакции, лучшие посты.")
async def get_analytics(ctx: AgentContext, args: PeriodArgs) -> dict:
    summary = await analytics.get_summary(ctx.session, ctx.business, days=args.days)
    return asdict(summary)


@tool("make_report", "Подготовить отчёт о работе SMM за период (текст и график).")
async def make_report(ctx: AgentContext, args: PeriodArgs) -> dict:
    report = await reports.build_report(ctx.session, ctx.llm, ctx.business, days=args.days)
    return {"text": report.text, "has_chart": report.chart_path is not None}
