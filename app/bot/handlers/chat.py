"""Принимает задачи текстом и направляет управление в Mini App."""

from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import AgentContext
from app.agent.harness import active_draft_options_for, answer, choose_draft_option
from app.bot.keyboards import miniapp_keyboard
from app.container import AppContainer
from app.db.knowledge import MiniUserState
from app.db.models import User
from app.llm.base import LLMError
from app.services.accounts import accessible_profiles
from app.services.usage import BudgetExceededError, usage_scope
from app.web.miniapp import MiniApp

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
    if not profiles:
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(
                sql_text("SELECT pg_advisory_xact_lock(:user_key)"),
                {"user_key": -db_user.telegram_id},
            )
        selection = await session.get(MiniUserState, db_user.id, populate_existing=True)
        selection, _ = await MiniApp(container).initialize_profiles(session, db_user, selection)
        await session.commit()
        profiles = await accessible_profiles(session, db_user)
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
        previous = by_id.get(data.get("dialog_profile"))
        if previous and await active_draft_options_for(session, db_user.id, previous.id):
            business = previous
        else:
            business = (by_id.get(selection.selected_id) if selection else None) or previous
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
        interactive_drafts=True,
        storage=container.storage,
        session_factory=container.db.sessionmaker,
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
    await send_result(message, session, ctx, container, result, selection)


@router.callback_query(F.data.regexp(r"^draftopt:[0-9]+:[0-9a-f]{12}:(?:[a-z_]{1,12}|[1-5])$"))
async def on_draft_option(
    callback: CallbackQuery, session: AsyncSession, db_user: User, container: AppContainer, bot: Bot
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer()
        return
    _, profile_id, token, choice = callback.data.split(":")
    business = next(
        (
            profile
            for profile in await accessible_profiles(session, db_user)
            if profile.id == int(profile_id)
        ),
        None,
    )
    if business is None:
        await callback.answer("Профиль недоступен.", show_alert=True)
        return
    await callback.answer()
    ctx = AgentContext(
        session,
        db_user,
        business,
        container.settings,
        container.llm,
        container.images,
        research_llm=container.research_llm,
        chat_id=message.chat.id,
        interactive_drafts=True,
        storage=container.storage,
        session_factory=container.db.sessionmaker,
    )
    try:
        async with (
            ChatActionSender.typing(bot=bot, chat_id=message.chat.id),
            usage_scope(session, business.id),
        ):
            result = await choose_draft_option(ctx, token, choice)
    except (BudgetExceededError, ValueError) as exc:
        await message.answer(escape(str(exc)))
        return
    except LLMError:
        await message.answer("ИИ временно недоступен. Попробуйте ещё раз.")
        return
    await session.commit()
    await message.edit_reply_markup(reply_markup=None)
    selection = await session.get(MiniUserState, db_user.id)
    await send_result(message, session, ctx, container, result, selection)


async def send_result(message, session, ctx, container, result, selection):
    reply = escape(result["text"])
    if question := result.get("draft_options"):
        await session.commit()
        digits = []
        buttons = []
        for option in question["options"]:
            button = InlineKeyboardButton(
                text=option["label"],
                callback_data=f"draftopt:{ctx.business_id}:{question['token']}:{option['id']}",
            )
            if option["id"].isdigit():
                digits.append(button)
            else:
                buttons.append([button])
        if digits:
            buttons.insert(0, digits)
        if question.get("stage") in {"pick_image", "pick_logo"}:
            if url := container.settings.telegram_miniapp_url:
                buttons.extend(miniapp_keyboard(url).inline_keyboard)
            reply += "\nВ Mini App откройте «Профиль» → ✎ у компании. Номера видны в «Файлах профиля» и «Контенте»."
        await message.answer(reply, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        return
    if result.get("proposal") or ctx.created_post_ids:
        route = (
            "«Главная» → «Запрос из чата»"
            if result.get("proposal") and not ctx.created_post_ids
            else "«Главная» → «Новые предложения» или вкладка «Чат»"
        )
        profile = (
            "Выберите профиль «" + escape(ctx.business.name) + "». "
            if selection is None or selection.selected_id != ctx.business_id
            else ""
        )
        instructions = "\nВ Mini App: " + profile + route + "."
        await open_app(
            message,
            container,
            escape(ctx.business.name) + "\n" + reply + instructions,
        )
    elif (
        "главной" in reply.casefold()
        or "мини" in reply.casefold()
        or "mini app" in reply.casefold()
        or "редакторе профиля" in reply.casefold()
    ):
        await open_app(message, container, reply)
    else:
        await message.answer(reply)


@router.message()
async def attachments(message: Message, container: AppContainer) -> None:
    await open_app(message, container, "Материалы можно загрузить в профиле компании.")


async def open_app(message: Message, container: AppContainer, text: str) -> None:
    url = container.settings.telegram_miniapp_url
    await message.answer(text, reply_markup=miniapp_keyboard(url) if url else None)
