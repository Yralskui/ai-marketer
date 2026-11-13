"""Кнопки черновика: опубликовать / запланировать / изменить / отклонить.

Чтобы чат не засорялся, карточка черновика после действия обновляется на месте:
«✅ Опубликован» с кнопками «Открыть в канале» и «Удалить», «❌ Отклонён» и т.п.
Правка и время — быстрыми кнопками, а свой вариант можно просто написать.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import (
    QUICK_EDITS,
    PostCb,
    PublishedCb,
    QuickEditCb,
    TimeCb,
    delete_confirm_keyboard,
    published_keyboard,
    quick_edit_keyboard,
    retry_keyboard,
    schedule_keyboard,
)
from app.bot.states import ApprovalStates
from app.container import AppContainer
from app.db.models import Business, EventType, Platform, Post, PostStatus, User
from app.db.repositories import ChannelRepo, EventLogRepo
from app.llm.base import LLMError
from app.services import accounts, approval, brand, posts
from app.services.posts import STATUS_TITLES, InvalidTransitionError
from app.utils.time import (
    DateParseError,
    format_local,
    parse_schedule_time,
    quick_schedule_options,
)

log = logging.getLogger(__name__)
router = Router(name="approval")

# Из каких статусов доступна кнопка
EDITABLE = {
    PostStatus.DRAFT,
    PostStatus.PENDING_APPROVAL,
    PostStatus.APPROVED,
    PostStatus.SCHEDULED,
    PostStatus.FAILED,
}
ACTION_STATUSES: dict[str, set[PostStatus]] = {
    "publish": {PostStatus.PENDING_APPROVAL, PostStatus.FAILED},
    "schedule": {PostStatus.PENDING_APPROVAL, PostStatus.FAILED, PostStatus.SCHEDULED},
    "edit": EDITABLE,
    "reject": {PostStatus.DRAFT, PostStatus.PENDING_APPROVAL, PostStatus.FAILED},
    "brandfix": EDITABLE,
}


async def _load(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    business: Business,
    post_id: int,
    allowed: set[PostStatus],
) -> Post | None:
    post = await accounts.get_accessible_post(session, db_user, business, post_id)
    if post is None:
        await callback.answer(texts.POST_NOT_FOUND, show_alert=True)
        return None
    if post.status not in allowed:
        await callback.answer(
            texts.POST_WRONG_STATUS.format(id=post.id, status=STATUS_TITLES[post.status]),
            show_alert=True,
        )
        return None
    return post


async def _edit(
    message: Message | None, text: str, keyboard: InlineKeyboardMarkup | None = None
) -> bool:
    """Обновляет сообщение на месте. False — не получилось (удалено, старое и т.п.)."""
    if not isinstance(message, Message):
        return False
    try:
        if message.photo:
            await message.edit_caption(caption=text[:1024], reply_markup=keyboard)
        else:
            await message.edit_text(text, reply_markup=keyboard)
        return True
    except Exception:
        log.debug("Не удалось обновить сообщение", exc_info=True)
        return False


async def _drop_keyboard(message: Message | None) -> None:
    if isinstance(message, Message):
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            log.debug("Не удалось убрать клавиатуру", exc_info=True)


async def _channel_name(session: AsyncSession, post: Post) -> str | None:
    repo = ChannelRepo(session)
    channel = await repo.get(post.channel_id) if post.channel_id else None
    channel = channel or await repo.get_active(post.business_id, Platform(post.platform))
    return channel.title or channel.external_id if channel else None


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
    post = await _load(
        callback, session, db_user, business, callback_data.post_id, ACTION_STATUSES["publish"]
    )
    if post is None or not isinstance(callback.message, Message):
        return
    await callback.answer(texts.PUBLISHING)
    await _drop_keyboard(callback.message)
    outcome = await approval.approve_and_publish(session, post, db_user, container.publishing)
    if outcome.ok:
        dry = outcome.result is not None and outcome.result.dry_run
        card = texts.published_card(post, await _channel_name(session, post), dry)
        if post.external_url:
            card += f"\n{escape(post.external_url)}"
        keyboard = None if dry else published_keyboard(post.id, post.external_url)
        if not await _edit(callback.message, card, keyboard):
            await callback.message.answer(card, reply_markup=keyboard)
        return
    if outcome.skipped:
        return
    await callback.message.answer(
        texts.PUBLISH_FAILED_SHORT.format(id=post.id, error=escape(outcome.error or "")),
        reply_markup=retry_keyboard(post.id),
    )


# ---------------------------------------------------------------- удалить из канала


@router.callback_query(PublishedCb.filter())
async def on_published_action(
    callback: CallbackQuery,
    callback_data: PublishedCb,
    session: AsyncSession,
    db_user: User,
    business: Business,
    bot: Bot,
) -> None:
    post = await _load(
        callback, session, db_user, business, callback_data.post_id, {PostStatus.PUBLISHED}
    )
    if post is None or not isinstance(callback.message, Message):
        return
    if callback_data.action == "delete":
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=delete_confirm_keyboard(post.id))
        await callback.message.answer(texts.DELETE_CONFIRM.format(id=post.id))
        return
    if callback_data.action == "delete_no":
        await callback.answer("Оставляем")
        await callback.message.edit_reply_markup(
            reply_markup=published_keyboard(post.id, post.external_url)
        )
        return

    # delete_yes
    channel = await ChannelRepo(session).get(post.channel_id) if post.channel_id else None
    try:
        if channel is not None and post.external_id and post.external_id.isdigit():
            await bot.delete_message(channel.external_id, int(post.external_id))
    except Exception as exc:
        log.warning("Пост #%s не удалён из канала: %s", post.id, exc)
        await callback.answer(texts.DELETE_FAILED, show_alert=True)
        return
    post.external_url = None
    post.error = texts.POST_DELETED_MARK
    await EventLogRepo(session).log(
        EventType.POST_DELETED,
        f"Удалён из канала пользователем {db_user.telegram_id}",
        business_id=post.business_id,
        post_id=post.id,
    )
    await callback.answer(texts.DELETED_OK.format(id=post.id))
    await _edit(callback.message, texts.closed_card(post, "🗑"), None)


# ---------------------------------------------------------------- запланировать


@router.callback_query(PostCb.filter(F.action == "schedule"))
async def on_schedule(
    callback: CallbackQuery,
    callback_data: PostCb,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
) -> None:
    post = await _load(
        callback, session, db_user, business, callback_data.post_id, ACTION_STATUSES["schedule"]
    )
    if post is None or not isinstance(callback.message, Message):
        return
    # Можно нажать готовое время или написать своё — ждём оба варианта
    await state.set_state(ApprovalStates.waiting_schedule_time)
    await state.update_data(post_id=post.id, draft_message_id=callback.message.message_id)
    await callback.answer()
    tz = container.settings.tz
    options = [(label, int(when.timestamp())) for label, when in quick_schedule_options(tz)]
    await callback.message.answer(
        texts.ASK_SCHEDULE_QUICK.format(id=post.id, tz=container.settings.timezone),
        reply_markup=schedule_keyboard(post.id, options),
    )


async def _apply_schedule(
    session: AsyncSession,
    post: Post,
    when: datetime,
    db_user: User,
    container: AppContainer,
    bot: Bot,
    chat_id: int,
    draft_message_id: int | None,
) -> str:
    await approval.schedule(session, post, when, db_user)
    if draft_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=draft_message_id, reply_markup=None
            )
        except Exception:
            log.debug("Не удалось убрать клавиатуру черновика", exc_info=True)
    return texts.SCHEDULED_OK.format(
        id=post.id, when=format_local(post.scheduled_at, container.settings.tz)
    )


@router.callback_query(TimeCb.filter())
async def on_quick_time(
    callback: CallbackQuery,
    callback_data: TimeCb,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    post = await _load(
        callback, session, db_user, business, callback_data.post_id, ACTION_STATUSES["schedule"]
    )
    if post is None or not isinstance(callback.message, Message):
        return
    data = await state.get_data()
    when = datetime.fromtimestamp(callback_data.ts, tz=UTC)
    try:
        text = await _apply_schedule(
            session,
            post,
            when,
            db_user,
            container,
            bot,
            callback.message.chat.id,
            data.get("draft_message_id") if data.get("post_id") == post.id else None,
        )
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    if not await _edit(callback.message, text, None):
        await callback.message.answer(text)


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
    post = await accounts.get_accessible_post(session, db_user, business, data.get("post_id", 0))
    if post is None:
        await state.clear()
        await message.answer(texts.POST_NOT_FOUND)
        return
    try:
        when = parse_schedule_time(message.text or "", container.settings.tz)
        text = await _apply_schedule(
            session,
            post,
            when,
            db_user,
            container,
            bot,
            message.chat.id,
            data.get("draft_message_id"),
        )
    except (DateParseError, ValueError) as exc:
        await message.answer(texts.SCHEDULE_PARSE_ERROR.format(error=escape(str(exc))))
        return
    await state.clear()
    await message.answer(text)


# ---------------------------------------------------------------- изменить


@router.callback_query(PostCb.filter(F.action == "edit"))
async def on_edit(
    callback: CallbackQuery,
    callback_data: PostCb,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
) -> None:
    post = await _load(callback, session, db_user, business, callback_data.post_id, EDITABLE)
    if post is None or not isinstance(callback.message, Message):
        return
    # Можно нажать готовую правку или написать свою — ждём оба варианта
    await state.set_state(ApprovalStates.waiting_revision)
    await state.update_data(post_id=post.id)
    await callback.answer()
    await _drop_keyboard(callback.message)
    await callback.message.answer(
        texts.ASK_QUICK_EDIT.format(id=post.id), reply_markup=quick_edit_keyboard(post.id)
    )


async def _revise(
    message: Message,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
    post: Post,
    instruction: str,
) -> None:
    progress = await message.answer(texts.REVISING)
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            post_business = await accounts.business_of(session, post, business)
            await posts.revise(session, container.llm, post_business, post, instruction)
        except (InvalidTransitionError, LLMError) as exc:
            await progress.edit_text(escape(str(exc)))
            return
    try:
        await progress.delete()
    except Exception:
        log.debug("Не удалось удалить сообщение о правке", exc_info=True)
    await approval.submit_for_approval(session, post, container.notifier, message.chat.id)


@router.callback_query(QuickEditCb.filter())
async def on_quick_edit(
    callback: CallbackQuery,
    callback_data: QuickEditCb,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    post = await _load(callback, session, db_user, business, callback_data.post_id, EDITABLE)
    if post is None or not isinstance(callback.message, Message):
        return
    label, instruction = QUICK_EDITS.get(callback_data.code, ("", ""))
    if not instruction:
        await callback.answer()
        return
    await state.clear()
    await callback.answer(label)
    await _edit(callback.message, f"✏️ Пост #{post.id}: {escape(label)}", None)
    await _revise(callback.message, session, business, container, bot, post, instruction)


@router.message(ApprovalStates.waiting_revision, F.text, ~F.text.startswith("/"))
async def on_revision_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    data = await state.get_data()
    post = await accounts.get_accessible_post(session, db_user, business, data.get("post_id", 0))
    await state.clear()
    if post is None:
        await message.answer(texts.POST_NOT_FOUND)
        return
    await _revise(message, session, business, container, bot, post, message.text or "")


@router.callback_query(PostCb.filter(F.action == "brandfix"))
async def on_brand_fix(
    callback: CallbackQuery,
    callback_data: PostCb,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    """Переписать пост так, чтобы он прошёл проверку бренд-гида."""
    post = await _load(callback, session, db_user, business, callback_data.post_id, EDITABLE)
    if post is None or not isinstance(callback.message, Message):
        return
    post_business = await accounts.business_of(session, post, business)
    issues = brand.check_text(post.text, brand.get_guide(post_business))
    if not issues:
        await callback.answer("Нарушений бренд-гида нет")
        return
    await callback.answer("Исправляю")
    await _drop_keyboard(callback.message)
    instruction = "Исправь текст по бренд-гиду, остальное не меняй: " + "; ".join(issues)
    await _revise(callback.message, session, business, container, bot, post, instruction)


# ---------------------------------------------------------------- отклонить


@router.callback_query(PostCb.filter(F.action == "reject"))
async def on_reject(
    callback: CallbackQuery,
    callback_data: PostCb,
    session: AsyncSession,
    db_user: User,
    business: Business,
) -> None:
    post = await _load(
        callback, session, db_user, business, callback_data.post_id, ACTION_STATUSES["reject"]
    )
    if post is None or not isinstance(callback.message, Message):
        return
    await approval.reject(session, post, db_user)
    await callback.answer(texts.REJECTED_OK.format(id=post.id))
    if not await _edit(callback.message, texts.closed_card(post, "❌ Отклонён ·"), None):
        await callback.message.answer(texts.REJECTED_OK.format(id=post.id))
