"""Несколько компаний: приглашения, роли и изоляция данных (критерии ТЗ 9 и 11)."""

from __future__ import annotations

import pytest

import app.agent.tools  # noqa: F401  — регистрация инструментов
from app.agent.context import AgentContext
from app.agent.core import Agent
from app.db.models import Platform, PostStatus, UserRole
from app.db.repositories import AgentMessageRepo, ChannelRepo, PostRepo
from app.services import accounts, approval, posts
from app.services.accounts import CompanyError, resolve_account
from app.services.publishing import PublishingService
from tests.conftest import FakePublisher

ADMIN_ID = 111
CLIENT_A = 201
CLIENT_B = 202


@pytest.fixture
async def two_companies(session, settings, owner):
    """Компания A — по умолчанию у администратора (канал из .env), B — новая с клиентом."""
    admin, company_a = owner
    company_b = await accounts.create_company(session, admin, "Цветы «Роза»")
    client_b = await resolve_account(
        session, settings, CLIENT_B, "Клиент Б", start_payload=company_b.invite_code
    )
    await session.commit()
    return admin, company_a, company_b, client_b.user


async def test_admin_gets_default_company_with_env_channel(session, settings):
    account = await resolve_account(session, settings, ADMIN_ID, "Админ")
    assert account is not None and account.is_admin
    assert account.user.role == UserRole.ADMIN
    assert account.user.business_id == account.business.id
    channel = await ChannelRepo(session).get_active(account.business.id, Platform.TELEGRAM)
    assert channel is not None and channel.external_id == "@test_channel"


async def test_client_joins_by_invite(session, settings, two_companies):
    _admin, _, company_b, client = two_companies
    assert client.role == UserRole.OWNER
    assert client.business_id == company_b.id
    # Согласует посты теперь клиент, а не администратор
    assert company_b.owner_id == client.id

    # Повторный вход — без кода, по сохранённой привязке
    again = await resolve_account(session, settings, CLIENT_B, "Клиент Б")
    assert again is not None and again.business.id == company_b.id and not again.joined


async def test_strangers_are_denied(session, settings, two_companies):
    assert await resolve_account(session, settings, CLIENT_A, "Чужой") is None
    assert (
        await resolve_account(session, settings, CLIENT_A, "Чужой", start_payload="wrong-code-123")
        is None
    )


async def test_regenerated_invite_invalidates_old_link(session, settings, two_companies):
    admin, _, company_b, _ = two_companies
    old_code = company_b.invite_code
    await accounts.regenerate_invite(session, admin, company_b)
    assert company_b.invite_code != old_code
    assert await resolve_account(session, settings, CLIENT_A, "А", start_payload=old_code) is None


async def test_admin_removed_from_allowlist_loses_access(session, settings, owner):
    settings.allowed_user_ids = ""
    assert await resolve_account(session, settings, ADMIN_ID, "Бывший админ") is None


async def test_only_admin_manages_companies(session, two_companies):
    *_, client = two_companies
    with pytest.raises(CompanyError):
        await accounts.create_company(session, client, "Чужая")
    with pytest.raises(CompanyError):
        await accounts.list_companies(session, client)


async def test_client_cannot_reach_other_company_post(session, llm, images, two_companies):
    admin, company_a, company_b, client = two_companies
    post_a = await posts.create_draft(session, llm, images, company_a, "кофе")

    assert await accounts.get_accessible_post(session, client, company_b, post_a.id) is None
    assert await PostRepo(session).list_for_business(company_b.id) == []
    # Администратор может согласовать пост любой компании
    assert await accounts.get_accessible_post(session, admin, company_b, post_a.id) is post_a


async def test_agent_sees_only_own_company(session, settings, llm, images, notifier, two_companies):
    _admin, company_a, company_b, client = two_companies
    post_a = await posts.create_draft(session, llm, images, company_a, "секретная акция кофейни")

    ctx = AgentContext(
        session=session,
        user=client,
        business=company_b,
        settings=settings,
        llm=llm,
        images=images,
        notifier=notifier,
    )
    result = await Agent().run(ctx, "Покажи мои посты")
    assert result.tool_calls == ["list_posts"]
    assert f"#{post_a.id}" not in result.text
    assert "секретная" not in result.text


async def test_admin_history_is_split_by_company(
    session, settings, llm, images, notifier, two_companies
):
    admin, company_a, company_b, _ = two_companies

    def ctx_for(business):
        return AgentContext(
            session=session,
            user=admin,
            business=business,
            settings=settings,
            llm=llm,
            images=images,
            notifier=notifier,
        )

    await Agent().run(ctx_for(company_a), "Покажи мои посты")
    repo = AgentMessageRepo(session)
    assert await repo.last_n(admin.id, 50, business_id=company_b.id) == []
    assert await repo.last_n(admin.id, 50, business_id=company_a.id)


async def test_company_without_channel_does_not_publish_to_other(
    session, llm, images, notifier, two_companies
):
    """У новой компании канала нет — пост не уходит в канал компании A."""
    _admin, _, company_b, client = two_companies
    publisher = FakePublisher()
    service = PublishingService({Platform.TELEGRAM: publisher})

    post = await posts.create_draft(session, llm, images, company_b, "букеты к 8 марта")
    await approval.submit_for_approval(session, post, notifier, client.telegram_id)
    outcome = await approval.approve_and_publish(session, post, client, service)

    assert not outcome.ok
    assert post.status == PostStatus.FAILED
    assert publisher.published == []


async def test_company_channel_is_used_for_its_posts(session, llm, images, notifier, two_companies):
    _admin, company_a, company_b, client = two_companies
    await accounts.set_channel(session, company_b, "https://t.me/roza_flowers", client)
    channel_b = await ChannelRepo(session).get_active(company_b.id, Platform.TELEGRAM)
    channel_a = await ChannelRepo(session).get_active(company_a.id, Platform.TELEGRAM)
    assert channel_b.external_id == "@roza_flowers"
    assert channel_a.external_id == "@test_channel"

    post = await posts.create_draft(session, llm, images, company_b, "тюльпаны")
    assert post.channel_id == channel_b.id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@my_channel", "@my_channel"),
        ("my_channel", "@my_channel"),
        ("t.me/my_channel", "@my_channel"),
        ("https://t.me/my_channel/", "@my_channel"),
        ("-1001234567890", "-1001234567890"),
    ],
)
def test_normalize_channel_id(raw, expected):
    assert accounts.normalize_channel_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "@a", "12345", "hello world", "-42"])
def test_normalize_channel_id_rejects_garbage(raw):
    with pytest.raises(CompanyError):
        accounts.normalize_channel_id(raw)


async def test_outdated_schema_gives_clear_error(tmp_path):
    """Старый файл БД без новых колонок → понятная ошибка вместо падения на запросе."""
    import sqlite3

    from app.db.base import Database, SchemaOutdatedError

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("create table users (id integer primary key, telegram_id integer)")
    db = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    with pytest.raises(SchemaOutdatedError, match=r"users\.business_id"):
        await db.create_all()
    await db.dispose()
