"""Бренд-гид компании: просмотр, загрузка брендбука или правил, готовые шаблоны."""

from __future__ import annotations

import io
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from app.bot import texts
from app.bot.keyboards import BrandCb, brand_keyboard, cancel_keyboard
from app.bot.states import BrandStates
from app.container import AppContainer
from app.db.models import Business, User
from app.ingestion.documents import (
    MAX_DOCUMENT_BYTES,
    UnsupportedDocumentError,
    extract_text_async,
)
from app.services import accounts, brand
from app.services.brand_presets import PRESETS

log = logging.getLogger(__name__)
router = Router(name="brand")


def _view(business: Business) -> str:
    guide = brand.get_guide(business)
    body = brand.guide_to_text(guide)
    if not body:
        return texts.BRAND_EMPTY.format(company=escape(business.name))
    return texts.BRAND_HEADER.format(company=escape(business.name)) + escape(body)[:3500]


@router.message(Command("brand"))
async def cmd_brand(message: Message, db_user: User, business: Business) -> None:
    admin = accounts.is_admin(db_user)
    await message.answer(
        _view(business),
        reply_markup=brand_keyboard(
            [(code, title) for code, (title, _) in PRESETS.items()] if admin else [],
            admin=admin,
            filled=brand.get_guide(business).is_filled(),
        ),
    )


@router.callback_query(BrandCb.filter())
async def on_brand_action(
    callback: CallbackQuery,
    callback_data: BrandCb,
    state: FSMContext,
    db_user: User,
    business: Business,
) -> None:
    if not accounts.is_admin(db_user):
        await callback.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if callback_data.action == "upload":
        await state.set_state(BrandStates.waiting_material)
        await callback.message.answer(texts.BRAND_ASK_MATERIAL, reply_markup=cancel_keyboard())
    elif callback_data.action == "clear":
        brand.set_guide(business, None)
        await callback.message.answer(texts.BRAND_CLEARED.format(company=escape(business.name)))
    elif callback_data.action == "preset" and callback_data.value in PRESETS:
        title, preset = PRESETS[callback_data.value]
        merged = brand.get_guide(business).merged_with(preset)
        brand.set_guide(business, merged)
        await callback.message.answer(
            texts.BRAND_PRESET_APPLIED.format(preset=escape(title), company=escape(business.name))
        )
        await callback.message.answer(_view(business))


async def _save_from_text(
    message: Message, business: Business, container: AppContainer, bot: Bot, source: str
) -> None:
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        extracted = await brand.extract_guide(container.llm, source)
    if not extracted.is_filled():
        await message.answer(texts.BRAND_NOTHING_FOUND)
        return
    brand.set_guide(business, brand.get_guide(business).merged_with(extracted))
    await message.answer(texts.BRAND_SAVED)
    await message.answer(_view(business))


@router.message(BrandStates.waiting_material, F.document)
async def on_brand_document(
    message: Message,
    state: FSMContext,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    await state.clear()
    doc = message.document
    if doc is None:
        return
    if (doc.file_size or 0) > MAX_DOCUMENT_BYTES:
        await message.answer(texts.ONBOARDING_DOC_TOO_BIG)
        return
    buf = io.BytesIO()
    await bot.download(doc.file_id, destination=buf)
    try:
        source = await extract_text_async(doc.file_name or "brandbook.pdf", buf.getvalue())
    except UnsupportedDocumentError as exc:
        await message.answer(texts.ONBOARDING_DOC_ERROR.format(error=escape(str(exc))))
        return
    if not source.strip():
        # Брендбуки часто свёрстаны картинками — текста в PDF может не быть
        await message.answer(texts.BRAND_NO_TEXT_IN_PDF)
        return
    await _save_from_text(message, business, container, bot, source)


@router.message(BrandStates.waiting_material, F.text, ~F.text.startswith("/"))
async def on_brand_text(
    message: Message,
    state: FSMContext,
    business: Business,
    container: AppContainer,
    bot: Bot,
) -> None:
    await state.clear()
    await _save_from_text(message, business, container, bot, message.text or "")


async def menu_brand(message: Message, db_user: User, business: Business) -> None:
    """Для кнопки меню «🎨 Бренд-гид»."""
    await cmd_brand(message, db_user, business)
