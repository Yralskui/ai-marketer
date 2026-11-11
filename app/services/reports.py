"""Отчёты — ЗАГЛУШКА с интерфейсом (поток B).

Задумка: еженедельный отчёт = текст (LLM по AnalyticsSummary + EventLog) +
картинка-график (matplotlib/Pillow). Отправляется владельцу через Notifier
задачей scheduler/jobs.py::weekly_report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business
from app.llm.base import LLMProvider


@dataclass
class Report:
    text: str
    chart_path: Path | None = None


async def build_report(
    session: AsyncSession, llm: LLMProvider, business: Business, days: int = 7
) -> Report:
    """Отчёт за последние `days` дней."""
    # TODO(поток B)
    raise NotImplementedError("Отчёты пока в разработке")
