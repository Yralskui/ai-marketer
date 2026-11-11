"""Базовые классы ORM, движок и фабрика сессий."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from sqlalchemy import DateTime, MetaData
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
        """Создаёт таблицы (до появления миграций)."""
        from app.db import models  # noqa: F401  — регистрирует модели в metadata

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


def _ensure_sqlite_dir(url: str) -> None:
    path = url.split(":///", 1)[-1]
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
