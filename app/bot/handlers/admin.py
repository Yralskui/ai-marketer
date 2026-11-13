"""/posts, /post N, /status."""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import draft_keyboard
from app.container import AppContainer
from app.db.models import Business, Platform, PostStatus
from app.db.repositories import ChannelRepo, PostRepo
from app.scheduler.setup import JOB_TITLES
from app.utils.time import format_local

router = Router(name="admin")


@router.message(Command("posts"))
async def cmd_posts(
    message: Message, session: AsyncSession, business: Business, container: AppContainer
) -> None:
    items = await PostRepo(session).list_for_business(business.id, limit=15)
    await message.answer(texts.posts_list(items, container.settings.tz))


@router.message(Command("post"))
async def cmd_post(
    message: Message, command: CommandObject, session: AsyncSession, business: Business
) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer(texts.POST_USAGE)
        return
    post = await PostRepo(session).get_for_business(int(arg), business.id)
    if post is None:
        await message.answer(texts.POST_NOT_FOUND)
        return
    keyboard = None
    if post.status not in (PostStatus.PUBLISHED, PostStatus.REJECTED):
        keyboard = draft_keyboard(post.id)
    await message.answer(texts.draft_card(post), reply_markup=keyboard)


@router.message(Command("status"))
async def cmd_status(
    message: Message, session: AsyncSession, business: Business, container: AppContainer
) -> None:
    settings = container.settings
    channel_text = "не задан — подключите командой /channel"
    channel = await ChannelRepo(session).get_active(business.id, Platform.TELEGRAM)
    publisher = container.publishing.get_publisher(Platform.TELEGRAM)
    if channel is not None:
        channel_text = f"<code>{escape(channel.external_id)}</code>"
        if publisher is not None:
            check = await publisher.check_channel(channel)
            channel_text += f" — {'✅' if check.ok else '❌'} {escape(check.message)}"

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
            counts=await PostRepo(session).count_by_status(business.id),
            jobs=jobs,
            tools=len(container.registry),
        )
    )
