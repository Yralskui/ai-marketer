"""Расходы на AI: /costs (сводка за месяц) и /budget (лимит компании)."""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.container import AppContainer
from app.db.models import Business, User
from app.db.repositories import BusinessRepo
from app.services import accounts, usage
from app.utils.time import format_local

router = Router(name="costs")


@router.message(Command("costs"))
async def cmd_costs(
    message: Message,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
) -> None:
    """Администратору — все компании, клиенту — только своя."""
    tz = container.settings.tz
    since = usage.month_start_utc(tz)
    is_admin = accounts.is_admin(db_user)
    companies = await BusinessRepo(session).list_all() if is_admin else [business]

    lines = []
    estimated = False
    for company in companies:
        summary = await usage.usage_summary(session, company.id, since)
        estimated |= summary.estimated
        lines.append(
            texts.cost_line(
                company.id,
                company.name,
                calls=summary.llm_calls,
                tokens=summary.tokens,
                images=summary.images,
                cost=summary.cost_rub,
                budget=usage.get_budget(company),
                estimated=summary.estimated,
                current=is_admin and company.id == business.id,
            )
        )
    text = texts.COSTS_HEADER.format(since=format_local(since, tz)) + "\n".join(lines)
    if estimated:
        text += texts.COSTS_ESTIMATED
    if is_admin:
        text += texts.COSTS_FOOTER_ADMIN
    await message.answer(text)


@router.message(Command("budget"))
async def cmd_budget(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
) -> None:
    if not accounts.is_admin(db_user):
        await message.answer(texts.ADMIN_ONLY)
        return
    arg = (command.args or "").strip().lower().replace(",", ".").rstrip("₽ р").strip()
    company = escape(business.name)
    if arg in ("off", "нет", "0"):
        usage.set_budget(business, None)
        await message.answer(texts.BUDGET_OFF.format(company=company))
        return
    try:
        value = float(arg)
    except ValueError:
        await message.answer(texts.BUDGET_USAGE)
        return
    if value <= 0:
        await message.answer(texts.BUDGET_USAGE)
        return
    usage.set_budget(business, value)
    spent = await usage.month_spend(session, business.id, container.settings.tz)
    await message.answer(texts.BUDGET_SET.format(company=company, budget=f"{value:g}", spent=spent))
