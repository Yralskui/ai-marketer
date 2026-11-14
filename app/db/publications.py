"""Происхождение версий и почасовые снимки соцсетей."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RevisionProvenance(Base):
    __tablename__ = "revision_provenance"

    revision_id: Mapped[int] = mapped_column(ForeignKey("post_revisions.id"), primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("post_revisions.id"))
    request_id: Mapped[int | None] = mapped_column(ForeignKey("agent_messages.id"))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(32))
    request_text: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(Text)
    image_source: Mapped[str | None] = mapped_column(String(32))
    image_prompt: Mapped[str | None] = mapped_column(Text)
    image_model: Mapped[str | None] = mapped_column(String(255))
    media_id: Mapped[int | None] = mapped_column(ForeignKey("media_files.id"))


class PlanOrigin(Base):
    __tablename__ = "plan_origins"

    plan_id: Mapped[int] = mapped_column(ForeignKey("content_plans.id"), primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("agent_messages.id"))


class PostEditorialSettings(Base):
    __tablename__ = "post_editorial_settings"

    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )
    style: Mapped[str] = mapped_column(String(20))


class RevisionDecision(Base):
    __tablename__ = "revision_decisions"

    event_id: Mapped[int] = mapped_column(ForeignKey("event_log.id"), primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("post_revisions.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class ChannelSnapshot(Base):
    __tablename__ = "channel_snapshots"

    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    subscribers: Mapped[int] = mapped_column(Integer)


class PostSnapshot(Base):
    __tablename__ = "post_snapshots"

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    views: Mapped[int | None] = mapped_column(Integer)
    forwards: Mapped[int | None] = mapped_column(Integer)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class PostCritique(Base):
    __tablename__ = "post_critiques"

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    result: Mapped[dict | None] = mapped_column(JSON)


class PostAttachments(Base):
    __tablename__ = "post_attachments"

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    items: Mapped[list] = mapped_column(JSON)


class RevisionAttachments(Base):
    __tablename__ = "revision_attachments"

    revision_id: Mapped[int] = mapped_column(ForeignKey("post_revisions.id"), primary_key=True)
    items: Mapped[list] = mapped_column(JSON)
