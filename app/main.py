"""Точка входа.

python -m app.main          — запустить бота (polling) и планировщик
python -m app.main --check  — проверить конфиг, БД, агента и планировщик без Telegram
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.config import Settings, get_settings
from app.db.base import SchemaOutdatedError
from app.logging_setup import setup_logging

log = logging.getLogger("app")


async def run_check(settings: Settings) -> int:
    """Инициализирует всё, кроме подключения к Telegram, и выходит."""
    from aiogram.utils.token import TokenValidationError, validate_token

    from app.bot.setup import create_bot, create_dispatcher
    from app.container import build_container
    from app.scheduler.setup import create_scheduler

    problems: list[str] = []
    bot = None
    if settings.bot_token:
        try:
            validate_token(settings.bot_token)
            bot = create_bot(settings)
        except TokenValidationError:
            problems.append("BOT_TOKEN имеет неверный формат")
    else:
        problems.append("BOT_TOKEN не задан — бот не запустится")
    if not settings.allowed_ids:
        problems.append("ALLOWED_USER_IDS пуст — бот никого не пустит")
    if not settings.telegram_channel_id:
        problems.append("TELEGRAM_CHANNEL_ID не задан — публиковать будет некуда")

    container = build_container(settings, bot)
    try:
        await container.db.create_all()
        create_dispatcher(container)
        scheduler = create_scheduler(container)
        scheduler.start(paused=True)
        jobs = [job.id for job in scheduler.get_jobs()]
        scheduler.shutdown(wait=False)
    finally:
        await container.db.dispose()
        if bot is not None:
            await bot.session.close()

    print("Проверка конфигурации:")
    print(f"  БД:            {settings.effective_database_url}")
    print(f"  LLM:           {container.llm.name}")
    print(f"  Картинки:      {container.images.name}")
    print(f"  Dry-run:       {settings.publish_dry_run}")
    print(f"  Часовой пояс:  {settings.timezone}")
    print(f"  Инструменты:   {', '.join(container.registry.names())}")
    print(f"  Задачи:        {', '.join(jobs)}")
    for problem in problems:
        print(f"  ⚠ {problem}")
    print("OK: приложение инициализируется без ошибок")
    return 0


async def run_bot(settings: Settings) -> None:
    from aiogram.types import BotCommandScopeChat

    from app.bot.setup import ADMIN_COMMANDS, BOT_COMMANDS, create_bot, create_dispatcher
    from app.container import build_container
    from app.scheduler.setup import create_scheduler

    bot = create_bot(settings)
    container = build_container(settings, bot)
    await container.db.create_all()
    dp = create_dispatcher(container)
    scheduler = create_scheduler(container)
    container.scheduler = scheduler

    if not settings.allowed_ids:
        log.warning("ALLOWED_USER_IDS пуст: бот ответит каждому его ID, но никого не пустит")
    try:
        me = await bot.me()
        log.info(
            "Бот @%s запущен. LLM=%s, dry_run=%s",
            me.username,
            container.llm.name,
            settings.publish_dry_run,
        )
        await bot.set_my_commands(BOT_COMMANDS)
        await _set_bot_profile(bot)
        for admin_id in settings.allowed_ids:
            try:
                await bot.set_my_commands(
                    ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
                )
            except Exception:  # администратор ещё не писал боту — чат недоступен
                log.info("Не удалось задать меню команд для %s", admin_id)
        scheduler.start()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await container.db.dispose()
        await bot.session.close()


async def _set_bot_profile(bot) -> None:  # type: ignore[no-untyped-def]
    """Текст в пустом чате и в профиле бота. Аватарку и картинку описания Bot API
    ставить не умеет — их загружают в @BotFather (см. README)."""
    from app.bot import texts

    try:
        await bot.set_my_description(texts.BOT_DESCRIPTION)
        await bot.set_my_short_description(texts.BOT_SHORT_DESCRIPTION)
    except Exception:
        log.warning("Не удалось обновить описание бота", exc_info=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Автономный AI-маркетолог")
    parser.add_argument("--check", action="store_true", help="проверить запуск без Telegram")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        if args.check:
            sys.exit(asyncio.run(run_check(settings)))
        if not settings.bot_token:
            log.error("BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните его.")
            sys.exit(1)
        asyncio.run(run_bot(settings))
    except SchemaOutdatedError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Остановлено")


if __name__ == "__main__":
    main()
