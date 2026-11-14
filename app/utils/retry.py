"""Повтор асинхронных вызовов с экспоненциальной паузой."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Вызывает `func()`; при исключениях из `retry_on` повторяет до `retries` раз.

    Пауза: base_delay * 2^попытка + небольшой случайный разброс.
    """
    attempt = 0
    while True:
        try:
            return await func()
        except retry_on as exc:
            if attempt >= retries:
                raise
            delay = min(max_delay, base_delay * (2**attempt)) + random.uniform(0, 0.3)
            log.warning("Ошибка %r, повтор %d/%d через %.1f с", exc, attempt + 1, retries, delay)
            await asyncio.sleep(delay)
            attempt += 1
