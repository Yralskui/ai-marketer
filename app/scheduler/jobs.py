"""Фоновые задачи.

Функции принимают зависимости явно (удобно тестировать), а обёртки `*_job(container)`
вызываются планировщиком.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.prompts import render_prompt
from app.db.models import Business, ContentPlanItemStatus, PostStatus
from app.db.publications import PlanOrigin
from app.db.repositories import BusinessRepo, PostRepo, UserRepo
from app.llm.base import LLMProvider
from app.services import approval, content_plan, posts, reports, social_metrics
from app.services.business_profile import get_profile, profile_to_text
from app.services.knowledge_search import retrieve
from app.services.notifier import Notifier
from app.services.post_history import RevisionSource
from app.services.posts import PublishNotAllowedError
from app.services.publishing import PublishingService
from app.services.usage import usage_scope
from app.utils.time import to_local, utcnow

if TYPE_CHECKING:
    from app.container import AppContainer

log = logging.getLogger(__name__)

ReportSender = Callable[[int, reports.Report], Awaitable[None]]


async def publish_due_posts(
    sessionmaker: async_sessionmaker[AsyncSession],
    publishing: PublishingService,
    notifier: Notifier | None = None,
    now: datetime | None = None,
    admin_chat_ids: Iterable[int] = (),
) -> int:
    """Публикует запланированные посты, время которых наступило. Возвращает число успешных.

    Результат сообщается ответственному за компанию; о сбое — ещё и администраторам.
    """
    now = now or utcnow()
    published = 0
    async with sessionmaker() as session:
        due = await PostRepo(session).list_due(now)
        for post in due:
            try:
                async with publishing.post_lock(post.id):
                    await session.refresh(post)
                    if (
                        post.status != PostStatus.SCHEDULED
                        or post.scheduled_at is None
                        or post.scheduled_at > now
                    ):
                        continue
                    outcome = await publishing.publish(session, post)
            except PublishNotAllowedError:
                log.error("Пост #%s в расписании, но не одобрен — пропускаю", post.id)
                continue
            if outcome.skipped:
                continue
            chat_id = await _owner_chat_id(session, post.business_id)
            recipients = [chat_id] if chat_id else []
            if outcome.ok:
                published += 1
                text = f"✅ Запланированный пост #{post.id} опубликован."
                if post.external_url:
                    text += f"\n{post.external_url}"
            else:
                text = (
                    f"⚠️ Не удалось опубликовать пост #{post.id}: {html.escape(outcome.error or '')}"
                )
                recipients += [a for a in admin_chat_ids if a not in recipients]
            for recipient in recipients if notifier else []:
                try:
                    await notifier.send_text(recipient, text)
                except Exception:
                    log.exception("Не удалось уведомить %s о посте #%s", recipient, post.id)
    if due:
        log.info("publish_due_posts: к публикации %d, опубликовано %d", len(due), published)
    return published


async def generate_daily_drafts(
    sessionmaker: async_sessionmaker[AsyncSession], container: AppContainer
) -> int:
    """Готовит по одной новой теме и оставляет её на согласование."""
    async with sessionmaker() as session:
        business_ids = [item.id for item in await BusinessRepo(session).list_all()]
    created = 0
    for business_id in business_ids:
        async with sessionmaker() as session:
            business = await BusinessRepo(session).get(business_id)
            if (
                business is None
                or (business.settings or {}).get("miniapp_archived")
                or not get_profile(business).is_filled()
            ):
                continue
            chat_id = await _owner_chat_id(session, business.id)
            if chat_id is None:
                continue
            try:
                async with usage_scope(session, business.id):
                    today = to_local(utcnow(), container.settings.tz).date()
                    if await content_plan.has_current_plan(session, business.id, today):
                        continue
                    recent = await PostRepo(session).list_for_business(business.id, limit=15)
                    system = render_prompt(
                        "suggest_topic",
                        profile=profile_to_text(business),
                        recent="\n".join(post.topic for post in recent),
                    )
                    topic = (
                        await container.llm.complete(system, "Предложи тему.", task="suggest_topic")
                    ).strip()[:240]
                    if not topic or any(
                        topic.casefold() == post.topic.casefold() for post in recent
                    ):
                        continue
                    reference = await retrieve(session, container.settings, business.id, topic)
                    post = await posts.create_draft(
                        session,
                        container.llm,
                        container.images,
                        business,
                        topic,
                        with_image=True,
                        reference=reference,
                        source=RevisionSource("scheduler"),
                        image_model=container.settings.image_model,
                    )
                    await approval.submit_for_approval(session, post, container.notifier, chat_id)
                    created += 1
            except Exception:
                log.exception("Автономный черновик не создан для компании %s", business_id)
    return created


async def prepare_plan_drafts(container: AppContainer, now: datetime | None = None) -> int:
    """Готовит до одного черновика из утверждённого плана на компанию за запуск."""
    now = now or utcnow()
    async with container.db.sessionmaker() as session:
        business_ids = [business.id for business in await BusinessRepo(session).list_all()]
    created = 0
    for business_id in business_ids:
        async with container.db.sessionmaker() as session:
            try:
                business = await session.get(Business, business_id, with_for_update=True)
                if business is None or (business.settings or {}).get("miniapp_archived"):
                    continue
                plan = await content_plan.due_plan(session, business.id, now)
                if plan is None:
                    continue
                item = next(
                    item
                    for item in plan.items
                    if item.status == ContentPlanItemStatus.PLANNED
                    and item.planned_at is not None
                    and item.planned_at <= now + content_plan.DRAFT_LEAD
                )
                chat_id = await _owner_chat_id(session, business.id)
                async with usage_scope(session, business.id):
                    reference = await retrieve(session, container.settings, business.id, item.topic)
                    origin = await session.get(PlanOrigin, plan.id)
                    post = await posts.create_draft(
                        session,
                        container.llm,
                        container.images,
                        business,
                        item.topic,
                        platform=item.platform,
                        with_image=True,
                        plan_item_id=item.id,
                        source=RevisionSource(
                            "plan", request_id=origin.request_id if origin else None
                        ),
                        reference=reference,
                        image_model=container.settings.image_model,
                    )
                    item.status = ContentPlanItemStatus.DRAFTED
                    await approval.submit_for_approval(session, post, None, None)
                created += 1
                if container.notifier is not None and chat_id is not None:
                    await container.notifier.send_draft(chat_id, post)
            except Exception:
                log.exception("Не удалось обработать пункт контент-плана компании %s", business_id)
    return created


async def weekly_report(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMProvider,
    send_report: ReportSender,
    *,
    tz: tzinfo | None = None,
    days: int = 7,
    out_dir: Path | None = None,
) -> int:
    """Отчёт за `days` дней каждой компании — ответственному. Возвращает число отправленных."""
    sent = 0
    async with sessionmaker() as session:
        for business in await BusinessRepo(session).list_all():
            chat_id = await _owner_chat_id(session, business.id)
            if not chat_id:
                continue
            try:
                async with usage_scope(session, business.id):
                    report = await reports.build_report(
                        session, llm, business, days=days, tz=tz, out_dir=out_dir
                    )
                await session.commit()
                await send_report(chat_id, report)
                sent += 1
            except Exception:
                log.exception("Отчёт для компании #%s не отправлен", business.id)
    if sent:
        log.info("weekly_report: отправлено отчётов: %d", sent)
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
            container.db.sessionmaker,
            container.publishing,
            container.notifier,
            admin_chat_ids=sorted(container.settings.allowed_ids),
        )
    except Exception:
        log.exception("Сбой задачи publish_due_posts")


async def generate_daily_drafts_job(container: AppContainer) -> None:
    try:
        await generate_daily_drafts(container.db.sessionmaker, container)
    except Exception:
        log.exception("Сбой задачи generate_daily_drafts")


async def weekly_report_job(container: AppContainer) -> None:
    if container.bot is None:
        return
    from app.bot.notifier import send_report

    bot = container.bot

    async def sender(chat_id: int, report: reports.Report) -> None:
        await send_report(bot, chat_id, report)

    try:
        await weekly_report(
            container.db.sessionmaker,
            container.llm,
            sender,
            tz=container.settings.tz,
        )
    except Exception:
        log.exception("Сбой задачи weekly_report")


async def collect_social_metrics(container: AppContainer) -> None:
    await social_metrics.collect_hour(container.db.sessionmaker, container.analytics_sources)
