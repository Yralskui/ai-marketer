"""Инструмент создания контент-плана для активной компании."""

from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.agent.registry import tool
from app.db.publications import PlanOrigin
from app.services import content_plan
from app.utils.time import to_local


class CreatePlanArgs(BaseModel):
    period: content_plan.Period = Field("week", description="Период плана")
    hint: str | None = Field(None, max_length=2000, description="Пожелания клиента к темам")


@tool(
    "create_content_plan",
    "Составить черновик контент-плана на неделю или 30 дней. Пользователь утверждает план в Mini App.",
)
async def create_content_plan(ctx: AgentContext, args: CreatePlanArgs) -> dict:
    plan = await content_plan.generate_plan(
        ctx.session,
        ctx.llm,
        ctx.business,
        period=args.period,
        tz=ctx.settings.tz,
        hint=args.hint,
        reference=ctx.reference,
    )
    if ctx.request_id is not None:
        ctx.session.add(PlanOrigin(plan_id=plan.id, request_id=ctx.request_id))
    return {
        "plan_id": plan.id,
        "status": plan.status.value,
        "items": [
            {
                "date": to_local(item.planned_at, ctx.settings.tz).date().isoformat(),
                "topic": item.topic,
                "rubric": item.rubric,
            }
            for item in plan.items
        ],
    }
