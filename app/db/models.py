"""Все ORM-модели проекта.

⚠️ Изменения моделей — только по согласованию с напарником (см. CLAUDE.md).
Даты — naive UTC (см. app/utils/time.py).

Связи (relationship) в async-режиме не подгружаются лениво: обращение к
незагруженной связи упадёт. Данные получайте через репозитории.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum

# ---------------------------------------------------------------- enums


class UserRole(StrEnum):
    # Представитель компании (клиент): работает только со своей компанией
    OWNER = "owner"
    # Администратор (поставщик услуги): из ALLOWED_USER_IDS, видит все компании
    ADMIN = "admin"
    MEMBER = "member"


class SourceType(StrEnum):
    TEXT = "text"
    DOCUMENT = "document"
    WEBSITE = "website"


class Platform(StrEnum):
    TELEGRAM = "telegram"
    VK = "vk"
    MAX = "max"


class PostStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"


class ContentPlanStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    ARCHIVED = "archived"


class ContentPlanItemStatus(StrEnum):
    PLANNED = "planned"
    DRAFTED = "drafted"
    PUBLISHED = "published"
    SKIPPED = "skipped"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class EventType(StrEnum):
    POST_CREATED = "post_created"
    POST_REVISED = "post_revised"
    POST_SUBMITTED = "post_submitted"
    POST_APPROVED = "post_approved"
    POST_REJECTED = "post_rejected"
    POST_SCHEDULED = "post_scheduled"
    POST_PUBLISHED = "post_published"
    POST_FAILED = "post_failed"
    PROFILE_UPDATED = "profile_updated"
    SOURCE_ADDED = "source_added"
    AGENT_ERROR = "agent_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    COMMENT_RECEIVED = "comment_received"
    COMMENT_REPLIED = "comment_replied"
    COMPANY_CREATED = "company_created"
    CLIENT_JOINED = "client_joined"
    CLIENT_REVOKED = "client_revoked"
    PHOTO_ADDED = "photo_added"
    CLIENT_RESTORED = "client_restored"
    CHANNEL_CHANGED = "channel_changed"


# ---------------------------------------------------------------- models


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[UserRole] = mapped_column(str_enum(UserRole), default=UserRole.OWNER)
    # Компания, с которой пользователь работает сейчас: у клиента — его компания,
    # у администратора — выбранная командой /companies
    business_id: Mapped[int | None] = mapped_column(
        ForeignKey("businesses.id", use_alter=True), nullable=True
    )
    # False — доступ отозван администратором
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    businesses: Mapped[list[Business]] = relationship(
        back_populates="owner", foreign_keys="Business.owner_id"
    )


class Business(TimestampMixin, Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Кто согласует посты и получает уведомления: клиент, а пока он не подключился —
    # администратор, создавший компанию
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), default="Мой бизнес")
    # Структура — app/services/business_profile.py::BusinessProfile
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Код для ссылки-приглашения t.me/<bot>?start=<код>; None — приглашения нет
    invite_code: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    # Настройки компании без отдельных колонок: ai_budget_rub, auto_reply_comments,
    # discussion_chat_id… Читать и менять — через app/services/accounts.py
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    owner: Mapped[User] = relationship(back_populates="businesses", foreign_keys=[owner_id])
    raw_sources: Mapped[list[Source]] = relationship(back_populates="business")
    channels: Mapped[list[Channel]] = relationship(back_populates="business")


class Source(TimestampMixin, Base):
    """Сырой материал о бизнесе: текст пользователя, документ, сайт."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    type: Mapped[SourceType] = mapped_column(str_enum(SourceType))
    origin: Mapped[str] = mapped_column(String(1024), default="")  # имя файла / URL
    text: Mapped[str] = mapped_column(Text, default="")

    business: Mapped[Business] = relationship(back_populates="raw_sources")


class Channel(TimestampMixin, Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    platform: Mapped[Platform] = mapped_column(str_enum(Platform))
    external_id: Mapped[str] = mapped_column(String(255))  # chat_id канала / id группы
    title: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped[Business] = relationship(back_populates="channels")


class Post(TimestampMixin, Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), nullable=True)
    platform: Mapped[Platform] = mapped_column(str_enum(Platform), default=Platform.TELEGRAM)
    topic: Mapped[str] = mapped_column(String(512), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[PostStatus] = mapped_column(
        str_enum(PostStatus), default=PostStatus.DRAFT, index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0)
    plan_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_plan_items.id"), nullable=True
    )

    revisions: Mapped[list[PostRevision]] = relationship(
        back_populates="post", order_by="PostRevision.id"
    )
    plan_item: Mapped[ContentPlanItem | None] = relationship(back_populates="post")


class PostRevision(TimestampMixin, Base):
    """История версий текста поста. instruction=None — первая версия."""

    __tablename__ = "post_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)

    post: Mapped[Post] = relationship(back_populates="revisions")


class ContentPlan(TimestampMixin, Base):
    """Заготовка под модуль контент-плана (поток A)."""

    __tablename__ = "content_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[ContentPlanStatus] = mapped_column(
        str_enum(ContentPlanStatus), default=ContentPlanStatus.DRAFT
    )

    items: Mapped[list[ContentPlanItem]] = relationship(
        back_populates="plan", order_by="ContentPlanItem.planned_at"
    )


class ContentPlanItem(TimestampMixin, Base):
    """Пункт плана. Связь с постом — через Post.plan_item_id (одна сторона FK)."""

    __tablename__ = "content_plan_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("content_plans.id"), index=True)
    topic: Mapped[str] = mapped_column(String(512))
    planned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    platform: Mapped[Platform] = mapped_column(str_enum(Platform), default=Platform.TELEGRAM)
    rubric: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[ContentPlanItemStatus] = mapped_column(
        str_enum(ContentPlanItemStatus), default=ContentPlanItemStatus.PLANNED
    )

    plan: Mapped[ContentPlan] = relationship(back_populates="items")
    post: Mapped[Post | None] = relationship(back_populates="plan_item", uselist=False)


class AgentMessage(TimestampMixin, Base):
    """История диалога с агентом в формате OpenAI-сообщений."""

    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), nullable=True)
    role: Mapped[MessageRole] = mapped_column(str_enum(MessageRole))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"id": ..., "name": ..., "arguments": {...}}] — для role=assistant
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)


class EventLog(TimestampMixin, Base):
    """Журнал важных событий — база для аналитики и отчётов."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), nullable=True)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class UsageKind(StrEnum):
    LLM = "llm"
    IMAGE = "image"


class UsageRecord(TimestampMixin, Base):
    """Один платный вызов AI: запрос к LLM или генерация картинки. База учёта расходов."""

    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int | None] = mapped_column(
        ForeignKey("businesses.id"), nullable=True, index=True
    )
    kind: Mapped[UsageKind] = mapped_column(str_enum(UsageKind))
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    task: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    cost_rub: Mapped[float] = mapped_column(Float, default=0.0)
    # True — провайдер не вернул число токенов, оно оценено по длине текста
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)


class CommentStatus(StrEnum):
    NEW = "new"  # только пришёл, ответ ещё не готов
    PENDING = "pending"  # черновик ответа отправлен ответственному на согласование
    SENT = "sent"  # ответ опубликован под комментарием
    SKIPPED = "skipped"  # решили не отвечать


class Comment(TimestampMixin, Base):
    """Комментарий подписчика в группе обсуждения канала и наш ответ на него."""

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"), nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)  # группа обсуждения
    message_id: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    author_name: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[CommentStatus] = mapped_column(
        str_enum(CommentStatus), default=CommentStatus.NEW, index=True
    )
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Почему ответ не ушёл автоматически (жалоба, вопрос о деньгах…)
    needs_human_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reply_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_replied: Mapped[bool] = mapped_column(Boolean, default=False)


class PostMetric(TimestampMixin, Base):
    """Последние известные показатели опубликованного поста.

    Реакции присылает Telegram (апдейт message_reaction_count, бот — админ канала).
    Просмотры через Bot API недоступны, поэтому их здесь нет.
    """

    __tablename__ = "post_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), unique=True, index=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    reactions_total: Mapped[int] = mapped_column(Integer, default=0)
    # {"👍": 5, "🔥": 2}
    reactions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MediaFile(TimestampMixin, Base):
    """Фото компании из фотобанка: прислал клиент или администратор.

    Используется как картинка поста вместо сгенерированной. `description` — описание
    фото для нейросети (заполняет модель со зрением; пока может быть пустым).
    """

    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    path: Mapped[str] = mapped_column(String(1024))
    caption: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # file_unique_id Telegram — чтобы не сохранять одно и то же фото дважды
    telegram_unique_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
