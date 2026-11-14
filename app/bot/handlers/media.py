"""Свои фото: приём в фотобанк, пост с фото, замена картинки в черновике, /photos."""

from __future__ import annotations

import io
import logging
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import MediaCb, PostCb, cancel_keyboard, photo_keyboard, photos_keyboard
from app.bot.states import MediaStates
from app.container import AppContainer
from app.db.models import Business, PostStatus, User
from app.db.repositories import MediaRepo
from app.llm.base import LLMError
from app.services import accounts, approval, editorial, media
from app.services.media import MediaError
from app.services.posts import InvalidTransitionError

log = logging.getLogger(__name__)
router = Router(name="media")

# Из каких статусов можно заменить картинку
PHOTO_STATUSES = {
    PostStatus.DRAFT,
    PostStatus.PENDING_APPROVAL,
    PostStatus.APPROVED,
    PostStatus.SCHEDULED,
    PostStatus.FAILED,
}


async def _download(bot: Bot, message: Message) -> tuple[bytes, str]:
    photo = message.photo[-1]  # самое большое разрешение
    buf = io.BytesIO()
    await bot.download(photo.file_id, destination=buf)
    return buf.getvalue(), photo.file_unique_id


async def _make_post(
    message: Message,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
    media_id: int,
    topic: str,
) -> None:
    try:
        photo = await media.get_photo(session, business, media_id)
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            web_context = await editorial.verified_post_context(
                container.research_llm, container.settings.tz, topic or photo.caption or ""
            )
            post = await media.create_post_with_photo(
                session,
                container.llm,
                container.images,
                business,
                photo,
                topic,
                web_context=web_context,
            )
    except (MediaError, LLMError, editorial.UnverifiedPostFacts) as exc:
        await message.answer(escape(str(exc)))
        return
    await approval.submit_for_approval(session, post, container.notifier, message.chat.id)


# ---------------------------------------------------------------- замена картинки в черновике


@router.callback_query(PostCb.filter(F.action == "photo"))
async def on_replace_photo(
    callback: CallbackQuery,
    callback_data: PostCb,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
) -> None:
    post = await accounts.get_accessible_post(session, db_user, business, callback_data.post_id)
    if post is None:
        await callback.answer(texts.POST_NOT_FOUND, show_alert=True)
        return
    if post.status not in PHOTO_STATUSES:
        await callback.answer(texts.PHOTO_WRONG_STATUS.format(id=post.id), show_alert=True)
        return
    await state.set_state(MediaStates.waiting_replacement)
    await state.update_data(post_id=post.id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            texts.ASK_REPLACEMENT_PHOTO.format(id=post.id), reply_markup=cancel_keyboard()
        )


@router.message(MediaStates.waiting_replacement, F.photo)
async def on_replacement_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    data = await state.get_data()
    await state.clear()
    post = await accounts.get_accessible_post(session, db_user, business, data.get("post_id", 0))
    if post is None:
        await message.answer(texts.POST_NOT_FOUND)
        return
    post_business = await accounts.business_of(session, post, business)
    try:
        raw, unique_id = await _download(bot, message)
        photo, _ = await media.save_photo(
            session, post_business, raw, caption=message.caption or "", unique_id=unique_id
        )
        await media.replace_post_photo(session, post, photo)
    except (MediaError, InvalidTransitionError) as exc:
        await message.answer(escape(str(exc)))
        return
    await message.answer(texts.PHOTO_REPLACED.format(id=post.id))
    await approval.submit_for_approval(session, post, container.notifier, message.chat.id)


# ---------------------------------------------------------------- новое фото


@router.message(MediaStates.waiting_topic, F.text, ~F.text.startswith("/"))
async def on_topic_for_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    data = await state.get_data()
    await state.clear()
    await _make_post(
        message, session, business, container, bot, data.get("media_id", 0), message.text or ""
    )


@router.message(F.photo)
async def on_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    """Любое фото — в фотобанк. С подписью — сразу пост по подписи, без — спросим."""
    await state.clear()
    try:
        raw, unique_id = await _download(bot, message)
        photo, _ = await media.save_photo(
            session, business, raw, caption=message.caption or "", unique_id=unique_id
        )
    except MediaError as exc:
        await message.answer(escape(str(exc)))
        return
    if message.caption:
        await message.answer(texts.PHOTO_SAVED_MAKING_POST)
        await _make_post(message, session, business, container, bot, photo.id, message.caption)
        return
    await message.answer(texts.PHOTO_SAVED, reply_markup=photo_keyboard(photo.id))


@router.callback_query(MediaCb.filter(F.action == "post"))
async def on_post_from_photo(
    callback: CallbackQuery,
    callback_data: MediaCb,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
) -> None:
    try:
        await media.get_photo(session, business, callback_data.media_id)
    except MediaError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.set_state(MediaStates.waiting_topic)
    await state.update_data(media_id=callback_data.media_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.ASK_PHOTO_TOPIC, reply_markup=cancel_keyboard())


@router.callback_query(MediaCb.filter(F.action == "keep"))
async def on_keep_photo(callback: CallbackQuery) -> None:
    await callback.answer(texts.PHOTO_KEPT)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            log.debug("Не удалось убрать клавиатуру", exc_info=True)


# ---------------------------------------------------------------- /photos


@router.message(Command("photos"))
async def cmd_photos(message: Message, session: AsyncSession, business: Business, bot: Bot) -> None:
    items = [
        m
        for m in await MediaRepo(session).list_for_business(business.id, limit=10)
        if Path(m.path).exists()
    ]
    if not items:
        await message.answer(texts.PHOTOS_EMPTY)
        return
    album = [
        InputMediaPhoto(media=FSInputFile(m.path), caption=f"#{m.id} {m.caption}"[:1024])
        for m in items
    ]
    if album:
        await bot.send_media_group(message.chat.id, album)
    await message.answer(
        texts.PHOTOS_HEADER.format(company=escape(business.name), count=len(items)),
        reply_markup=photos_keyboard([(m.id, m.caption) for m in items[:6]]),
    )
