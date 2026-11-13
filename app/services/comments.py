"""Комментарии подписчиков: черновик ответа от AI, согласование и автоответ.

Комментарии в Telegram живут в группе обсуждения, привязанной к каналу. Бот должен
быть администратором этой группы, тогда он видит все сообщения.

Правила (п. 3.4 ТЗ «автоматическая отправка ответов регулируется отдельными
разрешениями»):
- по умолчанию ответ уходит только после кнопки «Отправить» у ответственного;
- автоответ включает администратор (`/autoreply on`) для конкретной компании;
- даже с автоответом жалобы, негатив и вопросы о деньгах отправляются человеку.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import render_prompt
from app.db.models import Business, Comment, CommentStatus, EventType, Post, User
from app.db.repositories import BusinessRepo, ChannelRepo, CommentRepo, EventLogRepo
from app.llm.base import LLMError, LLMProvider
from app.services.business_profile import parse_json_object, profile_to_text
from app.utils.time import utcnow

log = logging.getLogger(__name__)

MAX_REPLY_CHARS = 1000

# Страховка поверх решения LLM: такие комментарии всегда видит человек
NEEDS_HUMAN_PATTERNS: list[tuple[str, str]] = [
    (r"жалоб|ужас|отврат|кошмар|обман|разочар|хамств|грубо|плохо|не работает|сломал", "негатив"),
    (r"верн(и|ите)\s+деньги|возврат|компенсац|претензи|суд|юрист", "возврат или претензия"),
    (r"мой заказ|номер заказа|где (мой|наш) заказ|не пришёл|не доставили", "заказ клиента"),
]


class CommentReplier(Protocol):
    """Отправляет ответ под комментарием. Реализация — в слое бота."""

    async def reply(self, chat_id: int, message_id: int, text: str) -> int:
        """Возвращает ID отправленного сообщения."""
        ...


@dataclass
class ReplyDraft:
    text: str
    needs_human: bool
    reason: str = ""


class CommentActionError(ValueError):
    """Действие с комментарием недоступно — текст показывается пользователю."""


# ---------------------------------------------------------------- настройки компании


def auto_reply_enabled(business: Business) -> bool:
    return bool((business.settings or {}).get("auto_reply_comments", False))


def set_auto_reply(business: Business, enabled: bool) -> None:
    business.settings = {**(business.settings or {}), "auto_reply_comments": enabled}


def discussion_chat_id(business: Business) -> int | None:
    value = (business.settings or {}).get("discussion_chat_id")
    return int(value) if value is not None else None


def set_discussion_chat(business: Business, chat_id: int) -> None:
    if discussion_chat_id(business) != chat_id:
        business.settings = {**(business.settings or {}), "discussion_chat_id": chat_id}


# ---------------------------------------------------------------- чья это группа


async def business_for_channel(
    session: AsyncSession, channel_chat_id: int, channel_username: str | None
) -> tuple[Business, int] | None:
    """Компания и ID канала в БД по чату Telegram-канала."""
    channels = await ChannelRepo(session).find_telegram(channel_chat_id, channel_username)
    if not channels:
        return None
    channel = channels[0]
    business = await BusinessRepo(session).get(channel.business_id)
    return (business, channel.id) if business else None


async def business_for_group(session: AsyncSession, group_chat_id: int) -> Business | None:
    for business in await BusinessRepo(session).list_all():
        if discussion_chat_id(business) == group_chat_id:
            return business
    return None


# ---------------------------------------------------------------- ответ


def _needs_human(text: str) -> str | None:
    lowered = text.lower()
    for pattern, reason in NEEDS_HUMAN_PATTERNS:
        if re.search(pattern, lowered):
            return reason
    return None


def _mock_reply(author: str, text: str) -> str:
    greeting = f"{author}, спасибо" if author else "Спасибо"
    if "?" in text:
        return f"{greeting} за вопрос! 🙌 Подробности подскажем в личных сообщениях."
    return f"{greeting} за комментарий! 🙌 Рады, что вы с нами."


async def draft_reply(
    llm: LLMProvider, business: Business, comment: Comment, post: Post | None
) -> ReplyDraft:
    """Черновик ответа. LLM решает, нужен ли человек; эвристика страхует сверху."""
    forced = _needs_human(comment.text)
    if llm.name == "mock":
        return ReplyDraft(
            _mock_reply(comment.author_name, comment.text), forced is not None, forced or ""
        )

    system = render_prompt(
        "reply_comment",
        profile=profile_to_text(business),
        post=(post.text if post else "— (пост не найден)")[:3000],
    )
    user = f"Автор: {comment.author_name or 'подписчик'}\nКомментарий: {comment.text}"
    raw = await llm.complete(system, user, json_mode=True, task="reply_comment")
    try:
        data = parse_json_object(raw)
        text = str(data.get("reply") or "").strip()
        needs_human = bool(data.get("needs_human")) or forced is not None
        reason = forced or str(data.get("reason") or "")
    except (LLMError, ValueError, TypeError):
        text, needs_human, reason = raw.strip(), True, "AI вернул ответ не в том формате"
    if not text:
        return ReplyDraft(_mock_reply(comment.author_name, comment.text), True, "пустой ответ AI")
    return ReplyDraft(text[:MAX_REPLY_CHARS], needs_human, reason[:255])


async def process_comment(
    session: AsyncSession,
    llm: LLMProvider,
    business: Business,
    comment: Comment,
    post: Post | None,
    replier: CommentReplier | None,
) -> Comment:
    """Сохраняет комментарий, готовит ответ и — если разрешено — отправляет его сам."""
    comment.business_id = business.id
    comment.post_id = post.id if post else None
    await CommentRepo(session).add(comment)
    await EventLogRepo(session).log(
        EventType.COMMENT_RECEIVED,
        comment.text[:500],
        business_id=business.id,
        post_id=comment.post_id,
    )
    try:
        draft = await draft_reply(llm, business, comment, post)
    except LLMError as exc:
        log.warning("Не удалось подготовить ответ на комментарий #%s: %s", comment.id, exc)
        draft = ReplyDraft(_mock_reply(comment.author_name, comment.text), True, str(exc)[:255])

    comment.reply_text = draft.text
    comment.needs_human_reason = draft.reason or None
    comment.status = CommentStatus.PENDING
    await session.flush()

    if auto_reply_enabled(business) and not draft.needs_human and replier is not None:
        await send_reply(session, comment, draft.text, replier, user=None, auto=True)
    return comment


async def send_reply(
    session: AsyncSession,
    comment: Comment,
    text: str,
    replier: CommentReplier,
    *,
    user: User | None,
    auto: bool = False,
) -> Comment:
    """Публикует ответ под комментарием. Повторное нажатие ничего не отправит."""
    if comment.status != CommentStatus.PENDING:
        raise CommentActionError("На этот комментарий уже ответили или его пропустили.")
    text = text.strip()[:MAX_REPLY_CHARS]
    if not text:
        raise CommentActionError("Пустой ответ отправить нельзя.")
    # Сначала «занимаем» комментарий и коммитим — до обращения к Telegram
    comment.status = CommentStatus.SENT
    comment.reply_text = text
    await session.commit()
    try:
        comment.reply_message_id = await replier.reply(comment.chat_id, comment.message_id, text)
    except Exception:
        comment.status = CommentStatus.PENDING
        await session.commit()
        raise
    comment.replied_at = utcnow()
    comment.auto_replied = auto
    await EventLogRepo(session).log(
        EventType.COMMENT_REPLIED,
        text[:500],
        business_id=comment.business_id,
        post_id=comment.post_id,
        payload={"auto": auto, "user_id": user.id if user else None},
    )
    await session.commit()
    return comment


async def skip(session: AsyncSession, comment: Comment) -> Comment:
    if comment.status != CommentStatus.PENDING:
        raise CommentActionError("На этот комментарий уже ответили или его пропустили.")
    comment.status = CommentStatus.SKIPPED
    await session.flush()
    return comment
