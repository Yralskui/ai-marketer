"""Контент-план — ЗАГЛУШКА с интерфейсом (поток A).

Задумка:
1. generate_plan: LLM по профилю бизнеса предлагает темы на период
   (рубрики, даты, платформы) -> ContentPlan + ContentPlanItem в статусе draft.
2. Клиент согласует план целиком (кнопки) -> approve_plan.
3. Планировщик (scheduler/jobs.py::generate_daily_drafts) по наступлению даты
   пункта создаёт черновик поста (posts.create_draft с plan_item_id) и шлёт на
   согласование.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business, ContentPlan, ContentPlanItem
from app.llm.base import LLMProvider


async def generate_plan(
    session: AsyncSession,
    llm: LLMProvider,
    business: Business,
    period: str = "week",
    hint: str | None = None,
) -> ContentPlan:
    """Создаёт черновик контент-плана на неделю (period='week') или месяц ('month')."""
    # TODO(поток A): промпт app/agent/prompts/content_plan.md, JSON-ответ, сохранение пунктов
    raise NotImplementedError("Контент-план пока в разработке — скоро научусь его составлять!")


async def approve_plan(session: AsyncSession, plan: ContentPlan) -> ContentPlan:
    """Согласование плана целиком."""
    # TODO(поток A)
    raise NotImplementedError("Согласование контент-плана пока в разработке")


async def due_items(session: AsyncSession, business: Business) -> list[ContentPlanItem]:
    """Пункты одобренного плана, по которым пора готовить черновик."""
    # TODO(поток A): для generate_daily_drafts
    raise NotImplementedError("Автономные черновики по плану пока в разработке")
