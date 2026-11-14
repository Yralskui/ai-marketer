"""Принимает задачи текстом и направляет управление в Mini App."""

from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import AgentContext
from app.agent.harness import answer
from app.bot.keyboards import miniapp_keyboard
from app.container import AppContainer
from app.db.knowledge import MiniUserState, PendingPostAction
from app.db.models import User
from app.llm.base import LLMError
from app.services.accounts import accessible_profiles
from app.services.usage import BudgetExceededError, usage_scope

router = Router(name="chat")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message, container: AppContainer) -> None:
    # Старые ссылки и подсказки /app по-прежнему ведут в рабочее пространство.
    await open_app(message, container, "Для управления откройте Mini App.")


@router.message(F.text)
async def on_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: User,
    container: AppContainer,
    bot: Bot,
) -> None:
    text = (message.text or "").strip()
    if not text or len(text) > 4000:
        await message.answer("Напишите задачу длиной до 4000 символов.")
        return
    data = await state.get_data()
    pending = data.get("pending_task")
    profiles = await accessible_profiles(session, db_user)
    selection = await session.get(MiniUserState, db_user.id)
    by_id = {profile.id: profile for profile in profiles}
    named = [
        profile
        for profile in profiles
        if profile.name.casefold() in text.casefold() or (pending and text == str(profile.id))
    ]
    business = named[0] if len(named) == 1 else None
    if pending and business is not None:
        text = pending
    elif not named and not pending:
        business = by_id.get(data.get("dialog_profile"))
        if business is None and selection:
            business = by_id.get(selection.selected_id)
        if business is None and len(profiles) == 1:
            business = profiles[0]
    if business is None:
        if not profiles:
            await open_app(message, container, "Сначала создайте профиль компании в Mini App.")
            return
        if not pending:
            await state.update_data(pending_task=text)
        options = "\n".join(f"{profile.id} · {escape(profile.name)}" for profile in profiles[:20])
        await message.answer("Для какой компании задача? Напишите название или номер.\n" + options)
        return

    await state.update_data(pending_task=None, dialog_profile=business.id)
    ctx = AgentContext(
        session,
        db_user,
        business,
        container.settings,
        container.llm,
        container.images,
        chat_id=message.chat.id,
        research_llm=container.research_llm,
    )
    try:
        async with (
            ChatActionSender.typing(bot=bot, chat_id=message.chat.id),
            usage_scope(session, business.id),
        ):
            turns = data.get("turns_since_reset")
            history_limit = 6 if turns is None else min(6, turns * 2)
            result = await answer(ctx, text, history_limit=history_limit)
    except BudgetExceededError as exc:
        await message.answer(escape(str(exc)))
        return
    except LLMError:
        await message.answer("ИИ временно недоступен. Попробуйте ещё раз.")
        return
    if turns is not None:
        await state.update_data(turns_since_reset=turns + 1)
    reply = escape(result["text"])
    if proposal := result.get("proposal"):
        await session.merge(
            PendingPostAction(
                user_id=db_user.id,
                business_id=business.id,
                proposal=proposal,
            )
        )
        await session.commit()
    if result.get("proposal") or ctx.created_post_ids:
        instructions = "\nПроверьте действие в Mini App."
        if selection is None or selection.selected_id != business.id:
            instructions = "\nВ Mini App выберите эту компанию и откройте «Главная»."
        await open_app(
            message,
            container,
            escape(business.name) + "\n" + reply + instructions,
        )
    else:
        await message.answer(reply)


@router.message()
async def attachments(message: Message, container: AppContainer) -> None:
    await open_app(message, container, "Материалы можно загрузить в профиле компании.")


async def open_app(message: Message, container: AppContainer, text: str) -> None:
    url = container.settings.telegram_miniapp_url
    await message.answer(text, reply_markup=miniapp_keyboard(url) if url else None)
