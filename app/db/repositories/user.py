from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, UserRole


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        result = await self.session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def get_or_create(
        self, telegram_id: int, name: str = "", role: UserRole = UserRole.OWNER
    ) -> User:
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id, name=name, role=role)
            self.session.add(user)
            await self.session.flush()
        elif name and user.name != name:
            user.name = name
        return user
