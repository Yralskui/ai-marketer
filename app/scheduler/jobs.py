"""Фоновые задачи.

Функции принимают зависимости явно (удобно тестировать), а обёртки `*_job(container)`
вызываются планировщиком.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Business
from app.db.repositories import BusinessRepo, PostRepo, UserRepo
from app.services import content_plan, reports
from app.services.notifier import Notifier
from app.services.posts import PublishNotAllowedError
from app.services.publishing import PublishingService
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.container import AppContainer

log = logging.getLogger(__name__)


async def publish_due_posts(
    sessionmaker: async_sessionmaker[AsyncSession],
    publishing: PublishingService,
    notifier: Notifier | None = None,
    now: datetime | None = None,
) -> int:
    """Публикует запланированные посты, время которых наступило. Возвращает число успешных."""
    now = now or utcnow()
    published = 0
    async with sessionmaker() as session:
        due = await PostRepo(session).list_due(now)
        for post in due:
            try:
                outcome = await publishing.publish(session, post)
            except PublishNotAllowedError:
                log.error("Пост #%s в расписании, но не одобрен — пропускаю", post.id)
                continue
            chat_id = await _owner_chat_id(session, post.business_id)
            if outcome.ok:
                published += 1
                text = f"✅ Запланированный пост #{post.id} опубликован."
                if post.external_url:
                    text += f"\n{post.external_url}"
            else:
                text = (
                    f"⚠️ Не удалось опубликовать пост #{post.id}: "
                    f"{html.escape(outcome.error or '')}"
                )
            if notifier and chat_id:
                try:
                    await notifier.send_text(chat_id, text)
                except Exception:
                    log.exception("Не удалось уведомить владельца о посте #%s", post.id)
    if due:
        log.info("publish_due_posts: к публикации %d, опубликовано %d", len(due), published)
    return published


async def generate_daily_drafts(
    sessionmaker: async_sessionmaker[AsyncSession], container: AppContainer
) -> int:
    """Автономно готовит черновики по одобренному контент-плану.

    TODO(поток A): для каждого бизнеса взять content_plan.due_items(), на каждый пункт
    вызвать posts.create_draft(plan_item_id=...) и approval.submit_for_approval(...).
    """
    async with sessionmaker() as session:
        for business in await BusinessRepo(session).list_all():
            try:
                await content_plan.due_items(session, business)
            except NotImplementedError as exc:
                log.info("generate_daily_drafts: %s", exc)
                return 0
    return 0


async def weekly_report(
    sessionmaker: async_sessionmaker[AsyncSession], container: AppContainer
) -> int:
    """Еженедельный отчёт владельцу. TODO(поток B): отправка текста и графика."""
    sent = 0
    async with sessionmaker() as session:
        for business in await BusinessRepo(session).list_all():
            try:
                report = await reports.build_report(session, container.llm, business, days=7)
            except NotImplementedError as exc:
                log.info("weekly_report: %s", exc)
                return 0
            chat_id = await _owner_chat_id(session, business.id)
            if container.notifier and chat_id:
                await container.notifier.send_text(chat_id, html.escape(report.text))
                sent += 1
    return sent


async def _owner_chat_id(session: AsyncSession, business_id: int) -> int | None:
    business: Business | None = await BusinessRepo(session).get(business_id)
    if business is None:
        return None
    owner = await UserRepo(session).get(business.owner_id)
    return owner.telegram_id if owner else None


# ---------------------------------------------------------------- обёртки для планировщика


async def publish_due_posts_job(container: AppContainer) -> None:
    try:
        await publish_due_posts(
            container.db.sessionmaker, container.publishing, container.notifier
        )
    except Exception:
        log.exception("Сбой задачи publish_due_posts")


async def generate_daily_drafts_job(container: AppContainer) -> None:
    try:
        await generate_daily_drafts(container.db.sessionmaker, container)
    except Exception:
        log.exception("Сбой задачи generate_daily_drafts")


async def weekly_report_job(container: AppContainer) -> None:
    try:
        await weekly_report(container.db.sessionmaker, container)
    except Exception:
        log.exception("Сбой задачи weekly_report")
