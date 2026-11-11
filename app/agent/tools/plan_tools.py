"""Инструменты: контент-план (поток A). Сейчас сервис бросает NotImplementedError,
а реестр превращает это в ответ «функция в разработке»."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.agent.registry import tool
from app.services import content_plan


class CreatePlanArgs(BaseModel):
    period: Literal["week", "month"] = Field("week", description="Период плана")
    hint: str | None = Field(None, description="Пожелания клиента к темам")


@tool(
    "create_content_plan",
    "Составить контент-план на неделю или месяц и отправить его клиенту на согласование.",
)
async def create_content_plan(ctx: AgentContext, args: CreatePlanArgs) -> dict:
    plan = await content_plan.generate_plan(
        ctx.session, ctx.llm, ctx.business, period=args.period, hint=args.hint
    )
    return {"plan_id": plan.id, "status": plan.status.value}
