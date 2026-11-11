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
    "generate_daily_drafts": "Автономные черновики по плану",
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
        **common,
    )
    if settings.enable_auto_drafts:
        scheduler.add_job(
            jobs.generate_daily_drafts_job,
            "cron",
            hour=9,
            minute=0,
            id="generate_daily_drafts",
            **common,
        )
    if settings.enable_weekly_report:
        scheduler.add_job(
            jobs.weekly_report_job,
            "cron",
            day_of_week="mon",
            hour=10,
            minute=0,
            id="weekly_report",
            **common,
        )
    return scheduler
