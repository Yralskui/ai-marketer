"""Генерация и согласование контент-планов компании."""

from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.prompts import render_prompt
from app.db.models import (
    Business,
    ContentPlan,
    ContentPlanItem,
    ContentPlanItemStatus,
    ContentPlanStatus,
)
from app.db.repositories import PostRepo
from app.llm.base import LLMError, LLMProvider
from app.services.business_profile import profile_to_text
from app.utils.time import to_local, to_utc_naive, utcnow

Period = Literal["week", "month"]
DRAFT_LEAD = timedelta(days=1)


class PlanCopy(BaseModel):
    topic: str = Field(min_length=1, max_length=240)
    rubric: str = Field(max_length=100)

    @field_validator("topic", "rubric", mode="before")
    @classmethod
    def strip_whitespace(cls, value):
        return value.strip() if isinstance(value, str) else value


class PlanTopic(PlanCopy):
    day: int = Field(ge=0, le=29, strict=True)


class PlanOutline(BaseModel):
    items: list[PlanTopic] = Field(min_length=5, max_length=12)


async def generate_plan(
    session: AsyncSession,
    llm: LLMProvider,
    business: Business,
    *,
    period: Period,
    tz: tzinfo,
    hint: str | None = None,
    reference: str = "",
) -> ContentPlan:
    days, count = (7, 5) if period == "week" else (30, 12)
    start = to_local(utcnow(), tz).date() + timedelta(days=1)
    recent = await PostRepo(session).list_for_business(business.id, limit=15)
    prompt = render_prompt("content_plan")
    raw = await llm.complete(
        prompt,
        f"Дней: {days}\nТем: {count}\nПрофиль:\n{profile_to_text(business)}\n\n"
        f"Недавние темы: {', '.join(post.topic for post in recent)}\n"
        f"Пожелания: {(hint or '')[:2000]}\nМатериалы:\n{reference[:8000]}",
        json_mode=True,
        task="content_plan",
    )
    try:
        outline = PlanOutline.model_validate_json(raw)
    except ValidationError as exc:
        raise LLMError("Модель вернула некорректный контент-план") from exc
    topics = outline.items
    if (
        len(topics) != count
        or any(item.day >= days for item in topics)
        or len({item.day for item in topics}) != count
        or len({item.topic.casefold() for item in topics}) != count
    ):
        raise LLMError("Модель вернула повторяющиеся темы или некорректные даты")
    plan = ContentPlan(
        business_id=business.id,
        title="План на неделю" if period == "week" else "План на 30 дней",
        period_start=start,
        period_end=start + timedelta(days=days - 1),
        items=[
            ContentPlanItem(
                topic=item.topic,
                rubric=item.rubric,
                planned_at=to_utc_naive(
                    datetime.combine(start + timedelta(days=item.day), time(10), tz)
                ),
            )
            for item in sorted(topics, key=lambda item: item.day)
        ],
    )
    session.add(plan)
    await session.flush()
    return plan


async def recent_plans(session: AsyncSession, business_id: int) -> list[ContentPlan]:
    plans = await session.scalars(
        select(ContentPlan)
        .where(
            ContentPlan.business_id == business_id, ContentPlan.status != ContentPlanStatus.ARCHIVED
        )
        .options(selectinload(ContentPlan.items))
        .order_by(ContentPlan.id.desc())
        .limit(12)
    )
    return list(plans)


async def get_plan_for_update(
    session: AsyncSession, business_id: int, plan_id: int
) -> ContentPlan | None:
    return await session.scalar(
        select(ContentPlan)
        .where(ContentPlan.id == plan_id, ContentPlan.business_id == business_id)
        .options(selectinload(ContentPlan.items))
        .with_for_update()
    )


def approve_plan(plan: ContentPlan) -> None:
    if plan.status == ContentPlanStatus.APPROVED:
        return
    if plan.status not in {ContentPlanStatus.DRAFT, ContentPlanStatus.PENDING_APPROVAL}:
        raise ValueError("Этот план нельзя утвердить")
    if not any(item.planned_at and item.planned_at > utcnow() for item in plan.items):
        raise ValueError("Даты плана уже прошли")
    plan.status = ContentPlanStatus.APPROVED


def revise_plan(plan: ContentPlan, changes: list[PlanCopy]) -> None:
    if plan.status not in {ContentPlanStatus.DRAFT, ContentPlanStatus.PENDING_APPROVAL}:
        raise ValueError("Можно изменить только план на согласовании")
    if len(changes) != len(plan.items):
        raise ValueError("Количество тем изменилось. Обновите экран.")
    if len({item.topic.casefold() for item in changes}) != len(changes):
        raise ValueError("Темы должны различаться")
    for item, change in zip(plan.items, changes, strict=True):
        item.topic = change.topic
        item.rubric = change.rubric
    plan.updated_at = utcnow()


async def due_plan(session: AsyncSession, business_id: int, now: datetime) -> ContentPlan | None:
    """Блокирует план до записи черновика. Повторный запуск не обработает тот же пункт."""
    ready_item = (
        select(ContentPlanItem.id)
        .where(
            ContentPlanItem.plan_id == ContentPlan.id,
            ContentPlanItem.status == ContentPlanItemStatus.PLANNED,
            ContentPlanItem.planned_at <= now + DRAFT_LEAD,
        )
        .exists()
    )
    return await session.scalar(
        select(ContentPlan)
        .where(
            ContentPlan.business_id == business_id,
            ContentPlan.status == ContentPlanStatus.APPROVED,
            ready_item,
        )
        .options(selectinload(ContentPlan.items))
        .order_by(ContentPlan.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


async def has_current_plan(session: AsyncSession, business_id: int, today: date) -> bool:
    plan_id = await session.scalar(
        select(ContentPlan.id)
        .where(
            ContentPlan.business_id == business_id,
            ContentPlan.status == ContentPlanStatus.APPROVED,
            ContentPlan.period_end >= today,
        )
        .limit(1)
    )
    return plan_id is not None
