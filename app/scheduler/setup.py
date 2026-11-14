"""Создание планировщика и регистрация задач."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.scheduler import jobs

if TYPE_CHECKING:
    from app.container import AppContainer

JOB_TITLES = {
    "publish_due_posts": "Публикация запланированных постов",
    "prepare_plan_drafts": "Черновики по утверждённым планам",
    "generate_daily_drafts": "Автономные предложения тем",
    "collect_social_metrics": "Почасовые метрики соцсетей",
    "weekly_report": "Еженедельный отчёт",
}


def create_scheduler(container: AppContainer) -> AsyncIOScheduler:
    settings = container.settings
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    common = {"args": [container], "max_instances": 1, "coalesce": True, "replace_existing": True}

    scheduler.add_job(
        jobs.publish_due_posts_job,
        "interval",
        seconds=settings.publish_check_interval_seconds,
        id="publish_due_posts",
        next_run_time=datetime.now(settings.tz),
        misfire_grace_time=60,
        **common,
    )
    scheduler.add_job(
        jobs.prepare_plan_drafts,
        "interval",
        minutes=5,
        id="prepare_plan_drafts",
        next_run_time=datetime.now(settings.tz),
        **common,
    )
    if container.analytics_sources:
        scheduler.add_job(
            jobs.collect_social_metrics,
            "cron",
            minute=0,
            id="collect_social_metrics",
            next_run_time=datetime.now(settings.tz),
            misfire_grace_time=300,
            **common,
        )
    if settings.enable_auto_drafts:
        auto_trigger = (
            {"trigger": "interval", "minutes": settings.auto_drafts_interval_minutes}
            if settings.auto_drafts_interval_minutes > 0
            else {"trigger": "cron", "hour": 9, "minute": 0}
        )
        scheduler.add_job(
            jobs.generate_daily_drafts_job,
            id="generate_daily_drafts",
            **auto_trigger,
            **common,
        )
    if settings.enable_weekly_report:
        if settings.report_interval_minutes > 0:
            # Режим демо: отчёт каждые N минут — видно, что задача работает сама
            trigger: dict = {"trigger": "interval", "minutes": settings.report_interval_minutes}
        else:
            trigger = {"trigger": "cron", "day_of_week": "mon", "hour": 10, "minute": 0}
        scheduler.add_job(jobs.weekly_report_job, id="weekly_report", **trigger, **common)
    return scheduler
