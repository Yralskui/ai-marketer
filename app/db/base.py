"""Базовые классы ORM, движок и фабрика сессий."""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from pathlib import Path

from sqlalchemy import DateTime, MetaData, inspect, text, update
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from app.utils.time import utcnow

# Явные имена ограничений — чтобы потом без боли подключить Alembic
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """created_at / updated_at во всех таблицах (naive UTC)."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


def str_enum(enum_cls: type[Enum]) -> SAEnum:
    """Enum хранится как VARCHAR со значением (не именем) — проще миграции и отладка."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class Database:
    """Движок + фабрика сессий. Один экземпляр на приложение."""

    def __init__(self, url: str, echo: bool = False) -> None:
        kwargs: dict = {}
        if url.startswith("sqlite"):
            if ":memory:" in url:
                # Одна общая in-memory БД на все соединения (для тестов)
                kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
            else:
                _ensure_sqlite_dir(url)
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, **kwargs)
        self.sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def create_all(self) -> None:
        """Создаёт таблицы (до появления миграций).

        Новые таблицы создаёт `create_all`. Новые колонки в существующих таблицах SQLite
        добавляются автоматически (`ALTER TABLE ADD COLUMN` + значение по умолчанию), чтобы
        после изменения моделей не приходилось удалять базу с данными. Переименования и
        удаления колонок так не делаются — для них нужны миграции (Alembic, B9).
        """
        from app.db import models  # noqa: F401  — регистрирует модели в metadata

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if self.engine.dialect.name == "sqlite":
                await conn.run_sync(_add_missing_columns)
            missing = await conn.run_sync(_missing_columns)
        if missing:
            raise SchemaOutdatedError(
                "Схема БД устарела, нет колонок: "
                + ", ".join(missing)
                + ". Миграций пока нет: удалите файл базы (по умолчанию data/app.db) "
                "и запустите снова."
            )

    async def dispose(self) -> None:
        await self.engine.dispose()


class SchemaOutdatedError(RuntimeError):
    """Файл БД создан старой версией кода, и недостающие колонки не добавить автоматически."""


def _add_missing_columns(sync_conn) -> None:  # type: ignore[no-untyped-def]
    """SQLite: добавляет недостающие колонки и индексы, заполняя значения по умолчанию."""
    log = logging.getLogger(__name__)
    inspector = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        added = False
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(
                text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}')
            )
            default = column.default
            if default is not None and (default.is_scalar or default.is_callable):
                value = default.arg if default.is_scalar else default.arg(None)
                sync_conn.execute(update(table).values({column.name: value}))
            log.warning("БД: добавлена колонка %s.%s", table.name, column.name)
            added = True
        if added:
            for index in table.indexes:
                index.create(sync_conn, checkfirst=True)


def _missing_columns(sync_conn) -> list[str]:  # type: ignore[no-untyped-def]
    inspector = inspect(sync_conn)
    missing = []
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        missing += [f"{table.name}.{c.name}" for c in table.columns if c.name not in existing]
    return missing


def _ensure_sqlite_dir(url: str) -> None:
    path = url.split(":///", 1)[-1]
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
