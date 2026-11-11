"""Кнопки черновика: опубликовать / запланировать / изменить / отклонить."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import PostCb, cancel_keyboard
from app.bot.states import ApprovalStates
from app.container import AppContainer
from app.db.models import Business, Post, PostStatus, User
from app.db.repositories import PostRepo
from app.llm.base import LLMError
from app.services import approval, posts
from app.services.posts import STATUS_TITLES, InvalidTransitionError
from app.utils.time import DateParseError, format_local, parse_schedule_time

log = logging.getLogger(__name__)
router = Router(name="approval")

# Из каких статусов доступна кнопка
ACTION_STATUSES: dict[str, set[PostStatus]] = {
    "publish": {PostStatus.PENDING_APPROVAL, PostStatus.FAILED},
    "schedule": {PostStatus.PENDING_APPROVAL, PostStatus.FAILED, PostStatus.SCHEDULED},
    "edit": {
        PostStatus.DRAFT,
        PostStatus.PENDING_APPROVAL,
        PostStatus.APPROVED,
        PostStatus.SCHEDULED,
        PostStatus.FAILED,
    },
    "reject": {PostStatus.DRAFT, PostStatus.PENDING_APPROVAL, PostStatus.FAILED},
}


async def _load_post(
    callback: CallbackQuery, callback_data: PostCb, session: AsyncSession, business: Business
) -> Post | None:
    post = await PostRepo(session).get_for_business(callback_data.post_id, business.id)
    if post is None:
        await callback.answer(texts.POST_NOT_FOUND, show_alert=True)
        return None
    if post.status not in ACTION_STATUSES[callback_data.action]:
        await callback.answer(
            texts.POST_WRONG_STATUS.format(id=post.id, status=STATUS_TITLES[post.status]),
            show_alert=True,
        )
        return None
    return post


async def _drop_keyboard(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # сообщение могли удалить или уже отредактировать
            log.debug("Не удалось убрать клавиатуру", exc_info=True)


# ---------------------------------------------------------------- опубликовать


@router.callback_query(PostCb.filter(F.action == "publish"))
async def on_publish(
    callback: CallbackQuery,
    callback_data: PostCb,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
) -> None:
    post = await _load_post(callback, callback_data, session, business)
    if post is None or not isinstance(callback.message, Message):
        return
    await callback.answer(texts.PUBLISHING)
    await _drop_keyboard(callback)
    outcome = await approval.approve_and_publish(session, post, db_user, container.publishing)
    if outcome.ok:
        dry = outcome.result is not None and outcome.result.dry_run
        text = (texts.PUBLISHED_DRY_RUN if dry else texts.PUBLISHED_OK).format(id=post.id)
        if post.external_url:
            text += f"\n{escape(post.external_url)}"
    else:
        text = texts.PUBLISH_FAILED.format(id=post.id, error=escape(outcome.error or ""))
    await callback.message.answer(text)


# ---------------------------------------------------------------- запланировать


@router.callback_query(PostCb.filter(F.action == "schedule"))
async def on_schedule(
    callback: CallbackQuery,
    callback_data: PostCb,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
) -> None:
    post = await _load_post(callback, callback_data, session, business)
    if post is None or not isinstance(callback.message, Message):
        return
    await state.set_state(ApprovalStates.waiting_schedule_time)
    await state.update_data(post_id=post.id, draft_message_id=callback.message.message_id)
    await callback.answer()
    await callback.message.answer(
        texts.ASK_SCHEDULE_TIME.format(id=post.id, tz=container.settings.timezone),
        reply_markup=cancel_keyboard(),
    )


@router.message(ApprovalStates.waiting_schedule_time, F.text, ~F.text.startswith("/"))
async def on_schedule_time(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    data = await state.get_data()
    post = await PostRepo(session).get_for_business(data.get("post_id", 0), business.id)
    if post is None:
        await state.clear()
        await message.answer(texts.POST_NOT_FOUND)
        return
    tz = container.settings.tz
    try:
        when = parse_schedule_time(message.text or "", tz)
        await approval.schedule(session, post, when, db_user)
    except (DateParseError, ValueError) as exc:
        await message.answer(texts.SCHEDULE_PARSE_ERROR.format(error=escape(str(exc))))
        return
    await state.clear()
    if data.get("draft_message_id"):
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=data["draft_message_id"], reply_markup=None
            )
        except Exception:
            log.debug("Не удалось убрать клавиатуру черновика", exc_info=True)
    await message.answer(
        texts.SCHEDULED_OK.format(id=post.id, when=format_local(post.scheduled_at, tz))
    )


# ---------------------------------------------------------------- изменить


@router.callback_query(PostCb.filter(F.action == "edit"))
async def on_edit(
    callback: CallbackQuery,
    callback_data: PostCb,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
) -> None:
    post = await _load_post(callback, callback_data, session, business)
    if post is None or not isinstance(callback.message, Message):
        return
    await state.set_state(ApprovalStates.waiting_revision)
    await state.update_data(post_id=post.id)
    await callback.answer()
    await _drop_keyboard(callback)
    await callback.message.answer(
        texts.ASK_REVISION.format(id=post.id), reply_markup=cancel_keyboard()
    )


@router.message(ApprovalStates.waiting_revision, F.text, ~F.text.startswith("/"))
async def on_revision_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    data = await state.get_data()
    post = await PostRepo(session).get_for_business(data.get("post_id", 0), business.id)
    await state.clear()
    if post is None:
        await message.answer(texts.POST_NOT_FOUND)
        return
    await message.answer(texts.REVISING)
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            await posts.revise(session, container.llm, business, post, message.text or "")
        except (InvalidTransitionError, LLMError) as exc:
            await message.answer(escape(str(exc)))
            return
    await approval.submit_for_approval(session, post, container.notifier, message.chat.id)


# ---------------------------------------------------------------- отклонить


@router.callback_query(PostCb.filter(F.action == "reject"))
async def on_reject(
    callback: CallbackQuery,
    callback_data: PostCb,
    session: AsyncSession,
    db_user: User,
    business: Business,
) -> None:
    post = await _load_post(callback, callback_data, session, business)
    if post is None or not isinstance(callback.message, Message):
        return
    await approval.reject(session, post, db_user)
    await callback.answer()
    await _drop_keyboard(callback)
    await callback.message.answer(texts.REJECTED_OK.format(id=post.id))
