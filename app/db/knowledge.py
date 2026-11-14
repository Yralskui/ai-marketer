"""Материалы профиля, очередь обработки и индекс."""

from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

EMBEDDING_DIMENSIONS = 1536


class ProfileDocument(TimestampMixin, Base):
    __tablename__ = "profile_documents"
    __table_args__ = (UniqueConstraint("business_id", "digest"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    digest: Mapped[str] = mapped_column(String(64))
    original_key: Mapped[str] = mapped_column(String(255))
    size: Mapped[int]
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("document_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("profile_documents.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    markdown_key: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(
        JSON().with_variant(Vector(EMBEDDING_DIMENSIONS), "postgresql")
    )


class MiniUserState(Base):
    __tablename__ = "mini_user_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    selected_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), nullable=True)
    default_created: Mapped[bool] = mapped_column(default=False)


class PendingPostAction(Base):
    __tablename__ = "pending_post_actions"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSON)


class ChatHistoryClear(Base):
    __tablename__ = "chat_history_clears"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    through_id: Mapped[int]


class HiddenGeneratedImage(Base):
    __tablename__ = "hidden_generated_images"

    revision_id: Mapped[int] = mapped_column(ForeignKey("post_revisions.id"), primary_key=True)


class PendingDraftOptions(TimestampMixin, Base):
    __tablename__ = "pending_draft_options"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    token: Mapped[str] = mapped_column(String(16))
    stage: Mapped[str] = mapped_column(String(12))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
