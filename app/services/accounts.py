"""Пользователи, компании, приглашения и каналы.

Роли:
- администратор (поставщик услуги) — Telegram ID из ALLOWED_USER_IDS. Создаёт компании,
  переключается между ними, видит все;
- клиент (представитель компании) — подключается по ссылке-приглашению и работает
  только со своей компанией.

Изоляция компаний держится на том, что хендлеры и инструменты получают ровно одну
`business` (из `resolve_account`), а все запросы к данным фильтруются по `business.id`.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Business, Channel, EventType, Platform, Post, User, UserRole
from app.db.repositories import (
    BusinessRepo,
    ChannelRepo,
    EventLogRepo,
    PostRepo,
    UserRepo,
)

DEFAULT_COMPANY_NAME = "Мой бизнес"
COMPANY_NAME_MAX = 255
# Deep-link payload Telegram: до 64 символов [A-Za-z0-9_-]
INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# @username публичного канала или числовой ID приватного (-100…)
CHANNEL_ID_RE = re.compile(r"^(@[A-Za-z][A-Za-z0-9_]{3,}|-100\d{5,})$")


class CompanyError(ValueError):
    """Ошибка операции с компанией — текст показывается пользователю."""


@dataclass
class Account:
    user: User
    business: Business
    # True — клиент только что подключился по приглашению
    joined: bool = False

    @property
    def is_admin(self) -> bool:
        return is_admin(self.user)


def is_admin(user: User) -> bool:
    return user.role == UserRole.ADMIN


def new_invite_code() -> str:
    return secrets.token_urlsafe(12)


def invite_link(bot_username: str, business: Business) -> str:
    return f"https://t.me/{bot_username}?start={business.invite_code}"


# ---------------------------------------------------------------- кто пишет боту


async def resolve_account(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int,
    name: str = "",
    *,
    start_payload: str | None = None,
) -> Account | None:
    """Пользователь и его текущая компания. None — доступа нет.

    `start_payload` — параметр из `/start <код>`: по нему клиент подключается к компании.
    """
    if telegram_id in settings.allowed_ids:
        user, business = await ensure_user_and_business(session, settings, telegram_id, name)
        return Account(user, business)

    users = UserRepo(session)
    user = await users.get_by_telegram_id(telegram_id)

    if start_payload and INVITE_CODE_RE.match(start_payload):
        business = await BusinessRepo(session).get_by_invite(start_payload)
        if business is not None:
            user = await _join_company(session, user, telegram_id, name, business)
            return Account(user, business, joined=True)

    # Администратор, которого убрали из ALLOWED_USER_IDS, доступ теряет
    if user is None or not user.is_active or is_admin(user) or user.business_id is None:
        return None
    business = await BusinessRepo(session).get(user.business_id)
    if business is None:
        return None
    if name and user.name != name:
        user.name = name
    return Account(user, business)


async def ensure_user_and_business(
    session: AsyncSession, settings: Settings, telegram_id: int, name: str = ""
) -> tuple[User, Business]:
    """Администратор и его текущая компания.

    При первом входе создаётся компания по умолчанию, и к ней привязывается канал
    из TELEGRAM_CHANNEL_ID. Каналы остальных компаний задаются командой /channel.
    """
    user = await UserRepo(session).get_or_create(telegram_id, name, role=UserRole.ADMIN)
    user.role = UserRole.ADMIN
    user.is_active = True
    businesses = BusinessRepo(session)

    business = await businesses.get(user.business_id) if user.business_id else None
    if business is None:
        business = await businesses.get_by_owner(user.id)
        if business is None:
            business = await businesses.create(
                user.id, DEFAULT_COMPANY_NAME, invite_code=new_invite_code()
            )
            if settings.telegram_channel_id:
                await ChannelRepo(session).upsert(
                    business.id, Platform.TELEGRAM, settings.telegram_channel_id.strip()
                )
        user.business_id = business.id
        await session.flush()
    return user, business


async def _join_company(
    session: AsyncSession, user: User | None, telegram_id: int, name: str, business: Business
) -> User:
    if user is None:
        user = User(telegram_id=telegram_id, name=name, role=UserRole.OWNER)
        session.add(user)
    user.role = UserRole.OWNER
    user.is_active = True
    user.business_id = business.id
    if name:
        user.name = name
    await session.flush()

    # Пока клиента не было, посты согласовывал администратор — теперь клиент
    owner = await UserRepo(session).get(business.owner_id)
    if owner is None or is_admin(owner):
        business.owner_id = user.id
    await EventLogRepo(session).log(
        EventType.CLIENT_JOINED,
        f"Клиент {name or telegram_id} подключился к компании",
        business_id=business.id,
        payload={"telegram_id": telegram_id},
    )
    await session.flush()
    return user


# ---------------------------------------------------------------- компании (администратор)


@dataclass
class CompanyInfo:
    business: Business
    clients: list[User]
    channel: Channel | None
    is_current: bool


async def create_company(session: AsyncSession, admin: User, name: str) -> Business:
    """Новая компания; администратор сразу переключается на неё."""
    _require_admin(admin)
    name = " ".join(name.split())[:COMPANY_NAME_MAX]
    if not name:
        raise CompanyError("Укажите название: /new_company Кофейня «Зерно»")
    business = await BusinessRepo(session).create(admin.id, name, invite_code=new_invite_code())
    admin.business_id = business.id
    await EventLogRepo(session).log(
        EventType.COMPANY_CREATED, f"Создана компания «{name}»", business_id=business.id
    )
    await session.flush()
    return business


async def list_companies(session: AsyncSession, admin: User) -> list[CompanyInfo]:
    _require_admin(admin)
    users = UserRepo(session)
    channels = ChannelRepo(session)
    result = []
    for business in await BusinessRepo(session).list_all():
        clients = [u for u in await users.list_by_business(business.id) if u.role == UserRole.OWNER]
        result.append(
            CompanyInfo(
                business=business,
                clients=clients,
                channel=await channels.get_active(business.id, Platform.TELEGRAM),
                is_current=business.id == admin.business_id,
            )
        )
    return result


async def switch_company(session: AsyncSession, admin: User, business_id: int) -> Business:
    _require_admin(admin)
    business = await BusinessRepo(session).get(business_id)
    if business is None:
        raise CompanyError(f"Компании #{business_id} нет. Список — /companies")
    admin.business_id = business.id
    await session.flush()
    return business


async def regenerate_invite(session: AsyncSession, admin: User, business: Business) -> Business:
    """Новый код приглашения; старая ссылка перестаёт работать."""
    _require_admin(admin)
    business.invite_code = new_invite_code()
    await session.flush()
    return business


async def set_client_access(
    session: AsyncSession, admin: User, business: Business, client_id: int, active: bool
) -> User:
    """Отключает или возвращает доступ клиента компании.

    При отключении меняется и код приглашения, чтобы клиент не вернулся по старой ссылке.
    """
    _require_admin(admin)
    client = await UserRepo(session).get(client_id)
    if client is None or client.business_id != business.id or is_admin(client):
        raise CompanyError("Такого клиента у текущей компании нет.")
    client.is_active = active
    if not active:
        business.invite_code = new_invite_code()
    await EventLogRepo(session).log(
        EventType.CLIENT_RESTORED if active else EventType.CLIENT_REVOKED,
        f"{'Возвращён' if active else 'Отключён'} доступ: {client.name or client.telegram_id}",
        business_id=business.id,
        payload={"admin_id": admin.id, "client_id": client.id},
    )
    await session.flush()
    return client


async def list_clients(session: AsyncSession, admin: User, business: Business) -> list[User]:
    _require_admin(admin)
    users = await UserRepo(session).list_by_business(business.id)
    return [u for u in users if u.role == UserRole.OWNER]


def _require_admin(user: User) -> None:
    if not is_admin(user):
        raise CompanyError("Эта команда доступна только администратору.")


# ---------------------------------------------------------------- каналы и посты


def normalize_channel_id(raw: str) -> str:
    """'@name', 't.me/name', '-100…' → идентификатор для Bot API."""
    value = raw.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.lower().startswith(prefix):
            value = "@" + value[len(prefix) :].strip("/")
    if value and not value.startswith(("@", "-")) and not value.isdigit():
        value = "@" + value
    if not CHANNEL_ID_RE.match(value):
        raise CompanyError(
            "Не похоже на канал. Укажите @username канала или его ID вида -100XXXXXXXXXX."
        )
    return value


async def set_channel(
    session: AsyncSession, business: Business, raw_channel: str, user: User
) -> Channel:
    """Telegram-канал компании (один активный на платформу)."""
    external_id = normalize_channel_id(raw_channel)
    channel = await ChannelRepo(session).upsert(business.id, Platform.TELEGRAM, external_id)
    await EventLogRepo(session).log(
        EventType.CHANNEL_CHANGED,
        f"Канал Telegram: {external_id}",
        business_id=business.id,
        payload={"user_id": user.id},
    )
    await session.flush()
    return channel


async def get_accessible_post(
    session: AsyncSession, user: User, business: Business, post_id: int
) -> Post | None:
    """Пост текущей компании. Администратору доступны посты любой компании —
    он может согласовать черновик, пришедший по другой компании."""
    post = await PostRepo(session).get(post_id)
    if post is None:
        return None
    if post.business_id == business.id or is_admin(user):
        return post
    return None


async def business_of(session: AsyncSession, post: Post, current: Business) -> Business:
    """Компания поста: обычно текущая, у администратора может быть другая."""
    if post.business_id == current.id:
        return current
    business = await BusinessRepo(session).get(post.business_id)
    assert business is not None  # FK гарантирует
    return business
