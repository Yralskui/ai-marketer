"""Инструменты: аналитика и отчёты (поток B)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.agent.registry import tool
from app.services import analytics, reports


class PeriodArgs(BaseModel):
    days: int = Field(7, ge=1, le=90, description="За сколько последних дней")


@tool(
    "get_analytics",
    "Статистика публикаций за период: число постов, реакции, комментарии, лучшие посты, "
    "расходы на AI. Просмотры недоступны.",
)
async def get_analytics(ctx: AgentContext, args: PeriodArgs) -> dict:
    summary = await analytics.get_summary(
        ctx.session, ctx.business, days=args.days, tz=ctx.settings.tz
    )
    return analytics.summary_for_llm(summary)


@tool(
    "make_report",
    "Отчёт о работе SMM за период: показатели, выводы, гипотезы и рекомендуемые действия.",
)
async def make_report(ctx: AgentContext, args: PeriodArgs) -> dict:
    report = await reports.build_report(
        ctx.session, ctx.llm, ctx.business, days=args.days, tz=ctx.settings.tz
    )
    return {"text": report.plain_text, "chart": "график — по кнопке «📈 Отчёт» или /report"}
