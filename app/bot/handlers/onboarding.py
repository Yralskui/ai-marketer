"""Онбординг: сбор материалов о бизнесе (текст, файлы, сайт) и /profile."""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from aiogram.utils.chat_action import ChatActionSender
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import OnboardingCb, onboarding_keyboard
from app.bot.states import OnboardingStates
from app.container import AppContainer
from app.db.models import Business, SourceType, User
from app.ingestion.documents import MAX_DOCUMENT_BYTES
from app.ingestion.knowledge import MATERIAL_EXTENSIONS
from app.ingestion.website import WebsiteFetchError, fetch_site_text, find_url, pages_to_text
from app.llm.base import LLMError
from app.services import business_profile
from app.services.document_uploads import (
    DocumentQuotaError,
    remaining_bytes,
    save_document,
    stage_document,
)

log = logging.getLogger(__name__)
router = Router(name="onboarding")


@router.message(Command("onboarding"))
async def cmd_onboarding(message: Message, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.collecting)
    await message.answer(texts.ONBOARDING_INTRO, reply_markup=onboarding_keyboard())


@router.message(Command("profile"))
async def cmd_profile(message: Message, business: Business) -> None:
    profile = business_profile.get_profile(business)
    if not profile.is_filled():
        await message.answer(texts.PROFILE_EMPTY)
        return
    await message.answer(texts.profile_card(profile))


@router.message(OnboardingStates.collecting, Command("done"))
async def cmd_done(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    await _finish(message, state, session, business, container, bot)


@router.callback_query(OnboardingStates.collecting, OnboardingCb.filter(F.action == "done"))
async def on_done(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await _finish(callback.message, state, session, business, container, bot)


@router.message(OnboardingStates.collecting, Command("upload"))
async def cmd_upload(
    message: Message, db_user: User, business: Business, container: AppContainer
) -> None:
    if not container.settings.upload_public_url:
        await message.answer(texts.UPLOAD_DISABLED)
        return
    token = container.upload_links.issue(db_user.id, business.id)
    link = f"{container.settings.upload_public_url}/upload#{token}"
    await message.answer(
        texts.UPLOAD_LINK.format(link=escape(link, quote=True)),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.message(OnboardingStates.collecting, F.document)
async def on_document(
    message: Message,
    session: AsyncSession,
    business: Business,
    db_user: User,
    bot: Bot,
    container: AppContainer,
) -> None:
    doc = message.document
    assert doc is not None
    filename = doc.file_name or "document"
    suffix = Path(filename).suffix.lower()
    if suffix not in MATERIAL_EXTENSIONS:
        await message.answer(texts.UPLOAD_UNSUPPORTED)
        return
    if doc.file_size and doc.file_size > await remaining_bytes(session, business.id):
        await message.answer(texts.UPLOAD_QUOTA)
        return
    if doc.file_size and doc.file_size > MAX_DOCUMENT_BYTES:
        await cmd_upload(message, db_user, business, container)
        return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        with stage_document(business.id) as path:
            try:
                await bot.download(doc, destination=path, timeout=60)
            except TelegramBadRequest as exc:
                if "file is too big" not in str(exc).lower():
                    raise
                await cmd_upload(message, db_user, business, container)
                return
            size = path.stat().st_size
            if size > MAX_DOCUMENT_BYTES:
                await cmd_upload(message, db_user, business, container)
                return
            if not size:
                await message.answer(texts.UPLOAD_EMPTY)
                return
            try:
                await save_document(session, business, filename, path, storage=container.storage)
            except DocumentQuotaError:
                await message.answer(texts.UPLOAD_QUOTA)
                return
    await message.answer(
        texts.ONBOARDING_DOC_SAVED.format(name=escape(filename), size=size),
        reply_markup=onboarding_keyboard(),
    )


@router.message(OnboardingStates.collecting, F.text, ~F.text.startswith("/"))
async def on_text(message: Message, session: AsyncSession, business: Business, bot: Bot) -> None:
    text = message.text or ""
    url = find_url(text)
    if url is None:
        await business_profile.add_source(session, business, SourceType.TEXT, "сообщение", text)
        await message.answer(texts.ONBOARDING_TEXT_SAVED, reply_markup=onboarding_keyboard())
        return

    # Рядом со ссылкой может быть и рассказ — его тоже сохраняем
    rest = text.replace(url, "").strip()
    if len(rest) > 20:
        await business_profile.add_source(session, business, SourceType.TEXT, "сообщение", rest)

    await message.answer(texts.ONBOARDING_SITE_LOADING.format(url=escape(url)))
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            pages = await fetch_site_text(url)
        except WebsiteFetchError as exc:
            await message.answer(texts.ONBOARDING_SITE_ERROR.format(error=escape(str(exc))))
            return
    site_text = pages_to_text(pages)
    await business_profile.add_source(session, business, SourceType.WEBSITE, url, site_text)
    await message.answer(
        texts.ONBOARDING_SITE_SAVED.format(pages=len(pages), chars=len(site_text)),
        reply_markup=onboarding_keyboard(),
    )


async def _finish(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    await message.answer(texts.ONBOARDING_BUILDING)
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            profile = await business_profile.build_profile(session, container.llm, business)
        except business_profile.NoSourcesError:
            await message.answer(texts.ONBOARDING_NO_SOURCES)
            return
        except (LLMError, ValidationError) as exc:
            log.exception("Не удалось собрать профиль")
            await message.answer(texts.ONBOARDING_FAILED.format(error=escape(str(exc))))
            return
    await state.clear()
    await message.answer(texts.ONBOARDING_DONE.format(profile=texts.profile_card(profile)))
