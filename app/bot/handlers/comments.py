"""Комментарии под постами: приём из группы обсуждения, согласование ответа, /comments.

Роутер подключается ПЕРВЫМ: все сообщения из групп обрабатываются только здесь и
дальше не идут (остальные хендлеры рассчитаны на личный чат с ботом).
"""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, MessageOriginChannel, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import CommentCb, cancel_keyboard, comment_keyboard
from app.bot.states import CommentStates
from app.container import AppContainer
from app.db.models import Business, Comment, CommentStatus, Platform, User
from app.db.repositories import ChannelRepo, CommentRepo, PostRepo, UserRepo
from app.services import accounts, comments
from app.services.comments import CommentActionError
from app.services.usage import usage_scope

log = logging.getLogger(__name__)
router = Router(name="comments")

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


class BotCommentReplier:
    """Ответ под комментарием от имени бота."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def reply(self, chat_id: int, message_id: int, text: str) -> int:
        msg = await self.bot.send_message(
            chat_id,
            text,
            parse_mode=None,
            reply_parameters=ReplyParameters(message_id=message_id),
        )
        return msg.message_id


# ---------------------------------------------------------------- группа обсуждения


def _channel_origin(message: Message) -> MessageOriginChannel | None:
    """Пост канала, к которому относится сообщение в группе обсуждения."""
    for candidate in (message, message.reply_to_message):
        if candidate is not None and isinstance(candidate.forward_origin, MessageOriginChannel):
            return candidate.forward_origin
    return None


@router.message(F.chat.type.in_(GROUP_TYPES))
async def on_group_message(
    message: Message, session: AsyncSession, container: AppContainer, bot: Bot
) -> None:
    origin = _channel_origin(message)
    found = None
    if origin is not None:
        found = await comments.business_for_channel(session, origin.chat.id, origin.chat.username)
        if found is not None:
            comments.set_discussion_chat(found[0], message.chat.id)

    # Пост канала, автоматически пересланный в группу, — не комментарий
    if message.is_automatic_forward or message.sender_chat is not None:
        return
    user = message.from_user
    text = (message.text or message.caption or "").strip()
    if user is None or user.is_bot or not text:
        return

    business = found[0] if found else await comments.business_for_group(session, message.chat.id)
    if business is None:
        log.info("Комментарий в группе %s без привязанной компании — пропускаю", message.chat.id)
        return
    owner = await UserRepo(session).get(business.owner_id)
    staff = set(container.settings.allowed_ids) | ({owner.telegram_id} if owner else set())
    if user.id in staff:
        return  # свои сообщения в обсуждении не обрабатываем

    post = None
    if origin is not None and found is not None:
        post = await PostRepo(session).find_published(found[1], str(origin.message_id))

    comment = Comment(
        chat_id=message.chat.id,
        message_id=message.message_id,
        author_id=user.id,
        author_name=user.full_name,
        text=text[:4000],
    )
    replier = BotCommentReplier(bot)
    async with usage_scope(session, business.id):
        comment = await comments.process_comment(
            session, container.llm, business, comment, post, replier
        )
    await session.commit()

    if owner is None:
        return
    try:
        if comment.status == CommentStatus.SENT:
            await bot.send_message(owner.telegram_id, texts.comment_auto_notice(comment))
        else:
            await bot.send_message(
                owner.telegram_id,
                texts.comment_card(comment, business.name, post),
                reply_markup=comment_keyboard(comment.id),
            )
    except Exception:
        log.exception("Не удалось отправить карточку комментария #%s владельцу", comment.id)


# ---------------------------------------------------------------- кнопки под карточкой


async def _load(
    callback: CallbackQuery, session: AsyncSession, db_user: User, business: Business, cid: int
) -> Comment | None:
    comment = await CommentRepo(session).get(cid)
    if comment is None or (comment.business_id != business.id and not accounts.is_admin(db_user)):
        await callback.answer(texts.COMMENT_NOT_FOUND, show_alert=True)
        return None
    return comment


async def _drop_keyboard(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            log.debug("Не удалось убрать клавиатуру", exc_info=True)


@router.callback_query(CommentCb.filter(F.action == "send"))
async def on_send(
    callback: CallbackQuery,
    callback_data: CommentCb,
    session: AsyncSession,
    db_user: User,
    business: Business,
    bot: Bot,
) -> None:
    comment = await _load(callback, session, db_user, business, callback_data.comment_id)
    if comment is None:
        return
    try:
        await comments.send_reply(
            session, comment, comment.reply_text or "", BotCommentReplier(bot), user=db_user
        )
    except CommentActionError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception as exc:
        log.warning("Ответ на комментарий #%s не отправлен: %s", comment.id, exc)
        await callback.answer(texts.COMMENT_SEND_FAILED, show_alert=True)
        return
    await callback.answer(texts.COMMENT_SENT)
    await _drop_keyboard(callback)


@router.callback_query(CommentCb.filter(F.action == "edit"))
async def on_edit(
    callback: CallbackQuery,
    callback_data: CommentCb,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
) -> None:
    comment = await _load(callback, session, db_user, business, callback_data.comment_id)
    if comment is None or not isinstance(callback.message, Message):
        return
    if comment.status != CommentStatus.PENDING:
        await callback.answer(texts.COMMENT_ALREADY_DONE, show_alert=True)
        return
    await state.set_state(CommentStates.waiting_reply)
    await state.update_data(comment_id=comment.id)
    await callback.answer()
    await callback.message.answer(texts.ASK_COMMENT_REPLY, reply_markup=cancel_keyboard())


@router.message(CommentStates.waiting_reply, F.text, ~F.text.startswith("/"))
async def on_own_reply(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    bot: Bot,
) -> None:
    data = await state.get_data()
    await state.clear()
    comment = await CommentRepo(session).get(data.get("comment_id", 0))
    if comment is None or (comment.business_id != business.id and not accounts.is_admin(db_user)):
        await message.answer(texts.COMMENT_NOT_FOUND)
        return
    try:
        await comments.send_reply(
            session, comment, message.text or "", BotCommentReplier(bot), user=db_user
        )
    except CommentActionError as exc:
        await message.answer(escape(str(exc)))
        return
    except Exception:
        await message.answer(texts.COMMENT_SEND_FAILED)
        return
    await message.answer(texts.COMMENT_SENT)


@router.callback_query(CommentCb.filter(F.action == "skip"))
async def on_skip(
    callback: CallbackQuery,
    callback_data: CommentCb,
    session: AsyncSession,
    db_user: User,
    business: Business,
) -> None:
    comment = await _load(callback, session, db_user, business, callback_data.comment_id)
    if comment is None:
        return
    try:
        await comments.skip(session, comment)
    except CommentActionError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.COMMENT_SKIPPED)
    await _drop_keyboard(callback)


# ---------------------------------------------------------------- /comments, /autoreply


async def _ensure_discussion(bot: Bot, session: AsyncSession, business: Business) -> int | None:
    """Группа обсуждения: из настроек или по linked_chat_id канала."""
    chat_id = comments.discussion_chat_id(business)
    if chat_id is not None:
        return chat_id
    channel = await ChannelRepo(session).get_active(business.id, Platform.TELEGRAM)
    if channel is None:
        return None
    try:
        chat = await bot.get_chat(channel.external_id)
    except Exception:
        return None
    if chat.linked_chat_id:
        comments.set_discussion_chat(business, chat.linked_chat_id)
    return chat.linked_chat_id


@router.message(Command("comments"))
async def cmd_comments(
    message: Message, session: AsyncSession, business: Business, bot: Bot
) -> None:
    group = await _ensure_discussion(bot, session, business)
    items = await CommentRepo(session).list_for_business(business.id, limit=10)
    await message.answer(
        texts.comments_overview(
            business.name,
            auto=comments.auto_reply_enabled(business),
            group_connected=group is not None,
            items=items,
        )
    )


@router.message(Command("autoreply"))
async def cmd_autoreply(
    message: Message, command: CommandObject, db_user: User, business: Business
) -> None:
    if not accounts.is_admin(db_user):
        await message.answer(texts.ADMIN_ONLY)
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off", "вкл", "выкл"):
        await message.answer(texts.AUTOREPLY_USAGE)
        return
    enabled = arg in ("on", "вкл")
    comments.set_auto_reply(business, enabled)
    await message.answer(
        (texts.AUTOREPLY_ON if enabled else texts.AUTOREPLY_OFF).format(
            company=escape(business.name)
        )
    )
