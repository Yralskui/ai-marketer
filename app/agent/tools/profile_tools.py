"""Инструменты: профиль бизнеса."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.agent.registry import EmptyArgs, tool
from app.services import business_profile


@tool(
    "get_business_profile",
    "Получить профиль бизнеса: название, описание, продукты, аудитория, УТП, тон, запреты.",
)
async def get_business_profile(ctx: AgentContext, args: EmptyArgs) -> dict:
    profile = business_profile.get_profile(ctx.business)
    return {"filled": profile.is_filled(), "profile": profile.model_dump()}


class UpdateProfileArgs(BaseModel):
    name: str | None = Field(None, description="Название бизнеса")
    description: str | None = Field(None, description="Чем занимается бизнес")
    products: list[str] | None = Field(None, description="Полный список продуктов/услуг")
    audience: str | None = Field(None, description="Целевая аудитория")
    usp: str | None = Field(None, description="Уникальное торговое предложение")
    tone: str | None = Field(None, description="Тон общения")
    restrictions: list[str] | None = Field(None, description="Полный список запретов")


@tool(
    "update_business_profile",
    "Обновить поля профиля бизнеса, когда клиент сообщает о себе новые факты или "
    "правила. Передавай только изменяемые поля; списки передавай целиком.",
)
async def update_business_profile(ctx: AgentContext, args: UpdateProfileArgs) -> dict:
    updates = args.model_dump(exclude_none=True)
    if not updates:
        return {"error": "Не передано ни одного поля для обновления"}
    profile = await business_profile.update_profile_fields(ctx.session, ctx.business, updates)
    return {"updated": sorted(updates), "profile": profile.model_dump()}
