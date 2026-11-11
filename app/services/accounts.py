"""Пользователи, их бизнесы и каналы по умолчанию."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Business, Platform, User
from app.db.repositories import BusinessRepo, ChannelRepo, UserRepo


async def ensure_user_and_business(
    session: AsyncSession, settings: Settings, telegram_id: int, name: str = ""
) -> tuple[User, Business]:
    """Пользователь + его бизнес (один на владельца) + Telegram-канал из .env."""
    user = await UserRepo(session).get_or_create(telegram_id, name)
    businesses = BusinessRepo(session)
    business = await businesses.get_by_owner(user.id)
    if business is None:
        business = await businesses.create(user.id)
    if settings.telegram_channel_id:
        await ChannelRepo(session).upsert(
            business.id, Platform.TELEGRAM, settings.telegram_channel_id.strip()
        )
    return user, business
