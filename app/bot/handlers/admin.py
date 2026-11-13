"""Посты (/posts с кнопками и фильтрами, /post N) и состояние (/status)."""

from __future__ import annotations

from contextlib import suppress
from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import (
    PostsFilterCb,
    PostViewCb,
    draft_keyboard,
    new_post_keyboard,
    posts_keyboard,
    published_keyboard,
)
from app.container import AppContainer
from app.db.models import Business, Platform, Post, PostStatus, User
from app.db.repositories import ChannelRepo, PostRepo
from app.scheduler.setup import JOB_TITLES
from app.services import accounts, usage
from app.services.posts import STATUS_TITLES
from app.utils.time import format_local

router = Router(name="admin")

# Фильтр списка постов -> статусы
FILTER_STATUSES: dict[str, set[PostStatus] | None] = {
    "all": None,
    "pending": {PostStatus.PENDING_APPROVAL, PostStatus.DRAFT, PostStatus.FAILED},
    "scheduled": {PostStatus.SCHEDULED, PostStatus.APPROVED},
    "published": {PostStatus.PUBLISHED},
}


async def _posts_view(
    session: AsyncSession, business: Business, container: AppContainer, status: str
) -> tuple[str, object]:
    items = await PostRepo(session).list_for_business(business.id, limit=30)
    wanted = FILTER_STATUSES.get(status)
    if wanted is not None:
        items = [p for p in items if PostStatus(p.status) in wanted]
    items = items[:12]
    if not items:
        text = texts.POSTS_EMPTY if status == "all" else texts.POSTS_FILTER_EMPTY
        return text, posts_keyboard([], status)
    buttons = [
        (p.id, f"#{p.id} {STATUS_TITLES[PostStatus(p.status)].split()[0]} {p.topic or p.text}")
        for p in items
    ]
    return texts.posts_list(items, container.settings.tz), posts_keyboard(buttons, status)


@router.message(Command("posts"))
async def cmd_posts(
    message: Message, session: AsyncSession, business: Business, container: AppContainer
) -> None:
    text, keyboard = await _posts_view(session, business, container, "all")
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(PostsFilterCb.filter())
async def on_posts_filter(
    callback: CallbackQuery,
    callback_data: PostsFilterCb,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
) -> None:
    text, keyboard = await _posts_view(session, business, container, callback_data.status)
    await callback.answer()
    if isinstance(callback.message, Message):
        # Тот же текст и клавиатура — Telegram не даёт «изменить» на то же, это не ошибка
        with suppress(Exception):
            await callback.message.edit_text(text, reply_markup=keyboard)


async def show_post(
    message: Message, session: AsyncSession, business: Business, container: AppContainer, post: Post
) -> None:
    """Карточка поста с подходящими кнопками."""
    channel = await ChannelRepo(session).get_active(business.id, Platform(post.platform))
    keyboard = None
    if post.status == PostStatus.PUBLISHED:
        keyboard = published_keyboard(post.id, post.external_url)
    elif post.status != PostStatus.REJECTED:
        keyboard = draft_keyboard(post.id)
    await message.answer(
        texts.draft_card(
            post, channel=channel.external_id if channel else None, tz=container.settings.tz
        ),
        reply_markup=keyboard,
    )


@router.callback_query(PostViewCb.filter())
async def on_post_view(
    callback: CallbackQuery,
    callback_data: PostViewCb,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
) -> None:
    post = await PostRepo(session).get_for_business(callback_data.post_id, business.id)
    if post is None:
        await callback.answer(texts.POST_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await show_post(callback.message, session, business, container, post)


@router.message(Command("post"))
async def cmd_post(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    business: Business,
    container: AppContainer,
) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer(texts.POST_USAGE)
        return
    post = await PostRepo(session).get_for_business(int(arg), business.id)
    if post is None:
        await message.answer(texts.POST_NOT_FOUND)
        return
    await show_post(message, session, business, container, post)


# ---------------------------------------------------------------- состояние


async def _channel_text(session: AsyncSession, business: Business, container: AppContainer) -> str:
    channel = await ChannelRepo(session).get_active(business.id, Platform.TELEGRAM)
    if channel is None:
        return "не подключён — кнопка «📣 Канал»"
    text = f"<code>{escape(channel.title or channel.external_id)}</code>"
    publisher = container.publishing.get_publisher(Platform.TELEGRAM)
    if publisher is not None:
        check = await publisher.check_channel(channel)
        text += f" — {'✅' if check.ok else '❌'} {escape(check.message)}"
    return text


@router.message(Command("status"))
async def cmd_status(
    message: Message,
    session: AsyncSession,
    db_user: User,
    business: Business,
    container: AppContainer,
) -> None:
    """Клиенту — понятное «Состояние», администратору — полная диагностика."""
    settings = container.settings
    channel_text = await _channel_text(session, business, container)
    counts = await PostRepo(session).count_by_status(business.id)

    if not accounts.is_admin(db_user):
        spent = await usage.month_spend(session, business.id, settings.tz)
        budget = usage.get_budget(business)
        cost = f"{spent:.2f} ₽" + (f" из {budget:.0f} ₽" if budget is not None else "")
        await message.answer(
            texts.client_status(
                company=business.name, channel=channel_text, counts=counts, month_cost=cost
            ),
            reply_markup=new_post_keyboard(),
        )
        return

    jobs: list[tuple[str, str]] = []
    if container.scheduler is not None:
        for job in container.scheduler.get_jobs():
            next_run = format_local(job.next_run_time, settings.tz) if job.next_run_time else "—"
            jobs.append((JOB_TITLES.get(job.id, job.id), next_run))

    await message.answer(
        texts.status_report(
            company=f"{escape(business.name)} (#{business.id})",
            llm=container.llm.name,
            model=settings.llm_model if container.llm.name != "mock" else "заглушка",
            images=container.images.name,
            dry_run=container.publishing.dry_run,
            channel=channel_text,
            counts=counts,
            jobs=jobs,
            tools=len(container.registry),
        )
    )
