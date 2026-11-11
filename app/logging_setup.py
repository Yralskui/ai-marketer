"""Настройка логирования."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Логи в stdout в едином формате. Шумные библиотеки приглушаем."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "aiosqlite", "apscheduler.executors", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
