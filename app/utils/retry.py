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


async def retry_with_budget(
    func: Callable[[int], Awaitable[T]],
    *,
    budget_seconds: float,
    retry_on: tuple[type[BaseException], ...],
    base_delay: float = 1.0,
    max_delay: float = 15.0,
    delay_for: Callable[[BaseException], float | None] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] | None = None,
) -> T:
    """Повторяет `func(attempt)`, пока не выйдет бюджет времени — «стучимся до конца».

    - ошибки из `retry_on` повторяются с растущей паузой (base_delay·2^n, не больше max_delay);
    - `delay_for(exc)` может вернуть свою паузу (например, Retry-After при 429);
    - остальные ошибки пробрасываются сразу: повтор не поможет (неверный ключ и т.п.);
    - номер попытки передаётся в func — по нему можно сменить прокси.
    """
    import time

    now = clock or time.monotonic
    deadline = now() + budget_seconds
    attempt = 0
    while True:
        try:
            return await func(attempt)
        except retry_on as exc:
            delay = (delay_for(exc) if delay_for else None) or min(
                max_delay, base_delay * (2**attempt)
            )
            delay += random.uniform(0, 0.3)
            if now() + delay >= deadline:
                log.error(
                    "Бюджет повторов %.0f с исчерпан, последняя ошибка: %r", budget_seconds, exc
                )
                raise
            log.warning(
                "Временная ошибка %r, попытка %d, повтор через %.1f с", exc, attempt + 1, delay
            )
            await (sleep or asyncio.sleep)(delay)
            attempt += 1
