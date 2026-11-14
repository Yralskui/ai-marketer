"""Проверки авторизации Mini App и изоляции компаний."""

import hmac
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.container import build_container
from app.scheduler.jobs import prepare_plan_drafts
from app.web.miniapp import MiniApp

BOT_TOKEN = "123456:local-placeholder"


def signed_user(user_id: int, *, age: int = 0) -> str:
    payload = {
        "auth_date": str(int(time.time()) - age),
        "user": json.dumps({"id": user_id, "first_name": f"User {user_id}"}),
    }
    secret = hmac.digest(b"WebAppData", BOT_TOKEN.encode(), "sha256")
    message = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    return urlencode({**payload, "hash": hmac.digest(secret, message.encode(), "sha256").hex()})


async def test_report_and_history_are_available_for_selected_company(settings, db):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    settings.publish_dry_run = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)
    headers = {"Authorization": "Telegram " + signed_user(71)}
    async with TestClient(TestServer(app)) as client:
        base = "/miniapp/api/"
        response = await client.post(base + "profiles", headers=headers, json={"name": "Студия"})
        company = (await response.json())["id"]
        response = await client.post(
            base + "chat",
            headers=headers,
            json={"company": company, "text": "Напиши пост про стекло"},
        )
        assert response.status == 200
        response = await client.get(base + "state", headers=headers)
        post = (await response.json())["posts"][0]
        response = await client.post(
            base + "post",
            headers=headers,
            json={
                "company": company,
                "id": post["id"],
                "version": post["version"],
                "action": "publish",
            },
        )
        assert response.status == 200
        response = await client.get(base + "history", headers=headers)
        assert (await response.json())["posts"][0]["id"] == post["id"]
        response = await client.get(base + "report?days=30", headers=headers)
        report = await response.json()
        assert "Опубликовано: 1" in report["text"]
        assert report["chart"].startswith("iVBORw0KGgo")
        response = await client.get(base + "diagnostics", headers=headers)
        assert (await response.json())["counts"]["published"] == 1
        await client.post(base + "profiles", headers=headers, json={"name": "Второй профиль"})
        response = await client.get(base + "history", headers=headers)
        assert (await response.json())["posts"] == []


async def test_accept_publishes_and_chat_revision_keeps_draft(settings, db):
    from unittest.mock import AsyncMock

    from app.db.models import Platform
    from app.db.repositories import ChannelRepo
    from app.publishers.base import PublishResult

    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    publisher = AsyncMock()
    publisher.publish.return_value = PublishResult("555", "https://t.me/test_channel/555")
    container.publishing.publishers[Platform.TELEGRAM] = publisher
    app = web.Application()
    MiniApp(container).register(app)
    base = "/miniapp/api/"
    headers = {"Authorization": "Telegram " + signed_user(76)}
    async with TestClient(TestServer(app)) as client:
        response = await client.get(base + "state", headers=headers)
        company = (await response.json())["active"]
        async with db.sessionmaker.begin() as session:
            await ChannelRepo(session).upsert(company, Platform.TELEGRAM, "@test_channel")
        response = await client.post(
            base + "chat",
            headers=headers,
            json={"company": company, "text": "Напиши пост про стекло"},
        )
        post_id = (await response.json())["draft_id"]
        response = await client.post(
            base + "chat",
            headers=headers,
            json={"company": company, "text": f"Правка поста №{post_id}: Сделай текст короче"},
        )
        assert (await response.json())["draft_id"] == post_id
        response = await client.get(base + "state", headers=headers)
        (post,) = (await response.json())["posts"]
        payload = {
            "company": company,
            "id": post_id,
            "version": post["version"],
            "action": "approve",
        }
        response = await client.post(base + "post", headers=headers, json=payload)
        assert await response.json() == {
            "ok": True,
            "simulated": False,
            "url": "https://t.me/test_channel/555",
        }
        response = await client.get(base + "state", headers=headers)
        state = await response.json()
        assert state["posts"] == []
        (preview,) = state["chat_posts"]
        assert preview["id"] == post_id
        assert preview["status"] == "published"
        assert preview["text"] == post["text"]
        response = await client.post(base + "post", headers=headers, json=payload)
        assert response.status == 409
        publisher.publish.assert_awaited_once()
        response = await client.get(f"{base}versions?id={post_id}", headers=headers)
        timeline = (await response.json())["timeline"]
        assert [row["type"] for row in timeline] == ["request", "revision", "post_published"]
        assert timeline[-1]["data"] == ""


async def test_plan_approval_generates_draft_once(settings, db, images):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    container.images = images
    app = web.Application()
    MiniApp(container).register(app)
    async with TestClient(TestServer(app)) as client:

        async def request(action, data=None, user_id=41):
            async with client.request(
                "GET" if data is None else "POST",
                f"/miniapp/api/{action}",
                json=data,
                headers={"Authorization": "Telegram " + signed_user(user_id)},
            ) as response:
                return response.status, await response.json()

        _, created = await request("profiles", {"name": "Стекло"})
        company = created["id"]
        assert (
            await request(
                "chat",
                {"company": company, "text": "Составь контент-план на неделю"},
            )
        )[0] == 200
        _, state = await request("state")
        (plan,) = state["plans"]
        assert len(plan["items"]) == 5
        assert state["posts"] == []
        first_date = datetime.fromisoformat(plan["items"][0]["when"]).replace(tzinfo=None)
        check_time = first_date - timedelta(hours=1)
        assert await prepare_plan_drafts(container, now=check_time) == 0
        approve = {
            "company": company,
            "id": plan["id"],
            "action": "approve",
            "version": plan["version"],
        }
        assert (await request("plan", approve, user_id=42))[0] == 404
        changes = [{"topic": item["topic"], "rubric": item["rubric"]} for item in plan["items"]]
        changes[0]["topic"] = "Новая тема первого поста"
        assert (await request("plan", {**approve, "action": "revise", "items": changes}))[0] == 200
        assert (await request("plan", approve))[0] == 409
        approve["version"] = (await request("state"))[1]["plans"][0]["version"]
        assert (await request("plan", approve))[0] == 200
        assert await prepare_plan_drafts(container, now=check_time) == 1
        assert await prepare_plan_drafts(container, now=check_time) == 0
        _, state = await request("state")
        (post,) = state["posts"]
        assert post["status"] == "pending_approval"
        assert post["topic"] == changes[0]["topic"]
        assert post["when"] is None
        assert post["suggested_at"] == plan["items"][0]["when"]
        assert state["plans"][0]["items"][0]["status"] == "drafted"
        _, detail = await request(f"versions?id={post['id']}")
        assert detail["plan_item_id"]
        assert detail["versions"][0]["source"] == "plan"
        assert detail["versions"][0]["request"] == "Составь контент-план на неделю"
        assert (
            await request(
                "plan",
                {**approve, "action": "archive", "version": state["plans"][0]["version"]},
            )
        )[0] == 200
        assert (await request("state"))[1]["plans"] == []
        assert len((await request("state"))[1]["posts"]) == 1


async def test_company_management_updates_only_active_owner_profile(settings, db):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)

    async with TestClient(TestServer(app)) as client:
        owner = {"Authorization": "Telegram " + signed_user(89)}
        stranger = {"Authorization": "Telegram " + signed_user(90)}
        api = "/miniapp/api/"
        async with client.post(
            api + "profiles", headers=owner, json={"name": "Студия"}
        ) as response:
            company = (await response.json())["id"]
        budget = {"company": company, "action": "budget", "value": 100}
        async with client.post(api + "settings", headers=stranger, json=budget) as response:
            assert response.status == 404
        async with client.post(api + "settings", headers=owner, json=budget) as response:
            assert response.status == 200
        async with client.post(
            api + "settings",
            headers=owner,
            json={"company": company, "action": "auto_reply", "enabled": True},
        ) as response:
            assert response.status == 200
        async with client.get(api + "state", headers=owner) as response:
            data = (await response.json())["management"]
            assert data["budget"] == 100 and data["auto_reply"] is True
        async with client.post(api + "select", headers=owner, json={"id": None}) as response:
            assert response.status == 200
        async with client.post(api + "settings", headers=owner, json=budget) as response:
            assert response.status == 403


async def test_profiles_are_private_and_default_is_copied(settings, db, tmp_path):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    template = tmp_path / "default.json"
    template.write_text(json.dumps({"name": "Салаватстекло", "visual_style": "Стекло"}))
    settings.default_profile_file = str(template)
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)

    async with TestClient(TestServer(app)) as client:

        async def request(user_id, method, action, data=None, auth=None):
            result = await client.request(
                method,
                f"/miniapp/api/{action}",
                json=data,
                headers={"Authorization": "Telegram " + (auth or signed_user(user_id))},
            )
            return result.status, await result.json()

        assert (await request(1, "GET", "state", auth=signed_user(1, age=50000)))[0] == 401
        status, a = await request(1, "GET", "state")
        assert status == 200
        _, b = await request(2, "GET", "state")
        assert a["profiles"][0]["name"] == b["profiles"][0]["name"] == "Салават Стекло"
        assert len(a["profiles"]) == len(b["profiles"]) == 1
        assert a["profiles"][0]["is_default"] and b["profiles"][0]["is_default"]
        assert a["active"] != b["active"]
        assert (await request(1, "DELETE", "profile", {"company": a["active"]}))[0] == 403
        assert (await request(1, "POST", "profile", {"company": a["active"], "action": "delete"}))[
            0
        ] == 403
        assert (await request(2, "POST", "select", {"id": a["active"]}))[0] == 404
        assert (await request(2, "POST", "chat", {"company": a["active"], "text": "покажи посты"}))[
            0
        ] == 404
        assert (await request(1, "POST", "profiles", {"name": "Альфа", "visual_style": "Синий"}))[
            0
        ] == 200
        _, state = await request(1, "GET", "state")
        new_id = state["active"]
        assert new_id != a["active"]
        assert (await request(1, "PATCH", "profile", {"company": new_id, "name": "Бета"}))[0] == 200
        assert (await request(1, "POST", "select", {"id": None}))[0] == 200
        assert (await request(1, "GET", "state"))[1]["active"] is None
        assert (await request(1, "POST", "profile", {"company": new_id, "action": "delete"}))[
            0
        ] == 200
        _, state = await request(1, "GET", "state")
        assert [profile["id"] for profile in state["profiles"]] == [a["active"]]


async def test_changed_post_requires_new_approval(settings, db):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    settings.publish_dry_run = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)
    headers = {"Authorization": "Telegram " + signed_user(41)}
    async with TestClient(TestServer(app)) as client:
        url = "/miniapp/api/"
        async with client.post(
            url + "profiles", headers=headers, json={"name": "Стекло"}
        ) as response:
            assert response.status == 200
            company = (await response.json())["id"]
        async with client.post(
            url + "chat",
            headers=headers,
            json={"company": company, "text": "Напиши пост про стекло"},
        ) as response:
            assert response.status == 200
        async with client.get(url + "state", headers=headers) as response:
            (post,) = (await response.json())["posts"]
        data = {"company": company, "id": post["id"], "version": post["version"]}
        async with client.post(
            url + "post",
            headers=headers,
            json={**data, "action": "revise", "instruction": "Сделай короче"},
        ) as response:
            assert response.status == 200
        async with client.post(
            url + "post", headers=headers, json={**data, "action": "publish"}
        ) as response:
            assert response.status == 409
        async with client.get(url + "state", headers=headers) as response:
            (current,) = (await response.json())["posts"]
            assert current["status"] == "pending_approval"
        async with client.post(
            url + "post",
            headers=headers,
            json={**data, "version": current["version"], "action": "publish"},
        ) as response:
            assert response.status == 200


async def test_post_pool_links_chat_photo_edits_and_approval(settings, db):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    settings.publish_dry_run = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)
    headers = {"Authorization": "Telegram " + signed_user(72)}
    async with TestClient(TestServer(app)) as client:
        base = "/miniapp/api/"
        response = await client.post(base + "profiles", headers=headers, json={"name": "Студия"})
        company = (await response.json())["id"]
        request = "Напиши пост про стекло с картинкой"
        response = await client.post(
            base + "chat", headers=headers, json={"company": company, "text": request}
        )
        assert response.status == 200
        draft_id = (await response.json())["draft_id"]
        response = await client.get(base + "post-pool", headers=headers)
        post_id = (await response.json())["posts"][0]["id"]
        assert post_id == draft_id
        response = await client.get(base + "state", headers=headers)
        assert (await response.json())["history"][-1]["draft_id"] == post_id
        for action, extra in [
            ("approve", {"when": (datetime.now().astimezone() + timedelta(days=1)).isoformat()}),
            ("revise", {"instruction": "Сделай текст короче"}),
            ("remove_photo", {}),
            ("publish", {}),
        ]:
            response = await client.get(base + "state", headers=headers)
            post = (await response.json())["posts"][0]
            response = await client.post(
                base + "post",
                headers=headers,
                json={
                    "company": company,
                    "id": post_id,
                    "version": post["version"],
                    "action": action,
                    **extra,
                },
            )
            assert response.status == 200, await response.json()
        response = await client.get(f"{base}versions?id={post_id}", headers=headers)
        details = await response.json()
        one, two, three = details["versions"]
        assert [event["type"] for event in details["timeline"]] == [
            "request",
            "post_scheduled",
            "revision",
            "revision",
            "simulation",
        ]
        assert one["request_id"] and one["request"] == request and one["image_prompt"]
        assert [event["type"] for event in one["decisions"]] == ["post_approved"]
        assert two["parent_id"] == one["id"] and two["image_prompt"] == one["image_prompt"]
        assert two["request"] == "Сделай текст короче"
        assert three["parent_id"] == two["id"] and not three["has_image"]
        assert [event["type"] for event in three["decisions"]] == [
            "post_approved",
            "post_published",
        ]
        response = await client.post(base + "profiles", headers=headers, json={"name": "Другая"})
        assert response.status == 200
        response = await client.get(f"{base}versions?id={post_id}", headers=headers)
        assert response.status == 404


async def test_metrics_calendar_and_profile_isolation(settings, db):
    from app.db.models import Platform
    from app.db.publications import ChannelSnapshot
    from app.db.repositories import ChannelRepo
    from app.utils.time import to_local, utcnow

    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)
    headers = {"Authorization": "Telegram " + signed_user(73)}
    async with TestClient(TestServer(app)) as client:
        base = "/miniapp/api/"
        response = await client.post(base + "profiles", headers=headers, json={"name": "Канал"})
        company = (await response.json())["id"]
        hour = utcnow().replace(minute=0, second=0, microsecond=0)
        async with db.sessionmaker.begin() as session:
            channel = await ChannelRepo(session).upsert(company, Platform.TELEGRAM, "@channel")
            session.add(ChannelSnapshot(channel_id=channel.id, hour=hour, subscribers=12))
        today = to_local(utcnow(), settings.tz).date()
        day = today.isoformat()
        response = await client.get(f"{base}metrics?from={day}&to={day}", headers=headers)
        assert response.status == 200
        payload = await response.json()
        assert payload["min_date"] == day
        assert payload["channels"][0]["points"][0][1] == 12
        yesterday = (today - timedelta(days=1)).isoformat()
        tomorrow = (today + timedelta(days=1)).isoformat()
        for start, end in [(yesterday, day), (day, tomorrow)]:
            response = await client.get(f"{base}metrics?from={start}&to={end}", headers=headers)
            assert response.status == 400
        await client.post(base + "profiles", headers=headers, json={"name": "Другой канал"})
        response = await client.get(base + "metrics?hours=168", headers=headers)
        assert (await response.json())["channels"] == []
        response = await client.get(base + "state", headers=headers)
        dashboard = (await response.json())["dashboard"]
        assert dashboard["subscribers"] == dashboard["views_7d"] == dashboard["published"] == 0


async def test_chat_progress_is_scoped_and_cleared(settings, db, monkeypatch):
    import asyncio
    from uuid import uuid4

    from app.web import miniapp

    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def answer(ctx, _message):
        ctx.update_progress("image")
        started.set()
        await finish.wait()
        return {"text": "Готово"}

    monkeypatch.setattr(miniapp, "answer", answer)
    base = "/miniapp/api/"
    headers = {"Authorization": "Telegram " + signed_user(74)}
    async with TestClient(TestServer(app)) as client:
        response = await client.get(base + "state", headers=headers)
        company = (await response.json())["active"]
        request_id = str(uuid4())
        progress = f"{base}chat-progress?company={company}&id={request_id}"
        task = asyncio.ensure_future(
            client.post(
                base + "chat",
                headers=headers,
                json={"company": company, "id": request_id, "text": "Сделай пост"},
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 3)
            response = await client.get(progress, headers=headers)
            assert await response.json() == {"stage": "image"}
            response = await client.get(
                progress, headers={"Authorization": "Telegram " + signed_user(75)}
            )
            assert response.status == 404
        finally:
            finish.set()
            response = await task
        assert await response.json() == {"text": "Готово"}
        response = await client.get(progress, headers=headers)
        assert await response.json() == {"stage": None}


async def test_manual_album_and_shared_critique_from_miniapp_and_chat(
    settings, db, monkeypatch, tmp_path
):
    from datetime import timedelta
    from io import BytesIO
    from unittest.mock import AsyncMock

    from PIL import Image

    from app.db.models import Post
    from app.db.publications import PostSnapshot
    from app.services import media
    from app.utils.time import utcnow

    monkeypatch.setattr("app.web.miniapp.IMAGES_DIR", tmp_path)
    monkeypatch.setattr(media, "UPLOADS_DIR", tmp_path)
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    settings.publish_dry_run = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    container.research_llm = AsyncMock()
    container.research_llm.complete.return_value = (
        '{"facts":"Есть замеры","hypothesis":"Гипотеза","next":"Проверка"}'
    )
    app = web.Application()
    MiniApp(container).register(app)
    headers = {"Authorization": "Telegram " + signed_user(74)}
    async with TestClient(TestServer(app)) as client:
        base = "/miniapp/api/"
        response = await client.post(base + "profiles", headers=headers, json={"name": "Студия"})
        company = (await response.json())["id"]
        response = await client.post(
            base + "chat",
            headers=headers,
            json={"company": company, "text": "Напиши пост про кофе"},
        )
        assert response.status == 200
        state = await (await client.get(base + "state", headers=headers)).json()
        post = state["posts"][0]
        buffer = BytesIO()
        Image.new("RGB", (20, 20)).save(buffer, "PNG")
        upload_headers = {
            **headers,
            "Content-Type": "application/octet-stream",
            "X-Filename": "coffee.png",
        }
        photo_ids = []
        for _ in range(2):
            response = await client.put(
                base + f"attachment?company={company}",
                headers=upload_headers,
                data=buffer.getvalue(),
            )
            assert response.status == 200
            photo_ids.append((await response.json())["id"])
        response = await client.post(
            base + "post",
            headers=headers,
            json={
                "company": company,
                "id": post["id"],
                "version": post["version"],
                "action": "edit_text",
                "text": "**Кофе** [Меню](https://example.org/menu)",
                "media": photo_ids,
            },
        )
        assert response.status == 200, await response.json()
        state = await (await client.get(base + "state", headers=headers)).json()
        post = state["posts"][0]
        assert len(post["media"]) == 2
        preview = await client.get(f"{base}preview?id={post['id']}&media=1", headers=headers)
        assert preview.status == 200
        assert (await preview.read()).startswith(b"\xff\xd8")
        response = await client.post(
            base + "post",
            headers=headers,
            json={
                "company": company,
                "id": post["id"],
                "version": post["version"],
                "action": "publish",
            },
        )
        assert response.status == 200
        async with db.sessionmaker.begin() as session:
            saved = await session.get(Post, post["id"])
            saved.published_at = utcnow() - timedelta(hours=3)
            session.add(
                PostSnapshot(post_id=saved.id, hour=utcnow() - timedelta(hours=1), views=15)
            )
        response = await client.post(
            base + "chat",
            headers=headers,
            json={
                "company": company,
                "text": f"Разбери пост №{post['id']}",
            },
        )
        assert "Гипотеза" in (await response.json())["text"]
        response = await client.post(
            base + "critique", headers=headers, json={"company": company, "id": post["id"]}
        )
        assert (await response.json())["critique"]["hypothesis"] == "Гипотеза"
        response = await client.get(f"{base}metrics?post={post['id']}", headers=headers)
        assert (await response.json())["critique"]["hypothesis"] == "Гипотеза"
        versions = await (
            await client.get(f"{base}versions?id={post['id']}", headers=headers)
        ).json()
        assert len(versions["versions"][-1]["media"]) == 2
        container.research_llm.complete.assert_awaited_once()


async def test_legacy_default_merges_empty_bootstrap_and_resolves_public_channel(settings, db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetChat

    from app.db.knowledge import MiniUserState
    from app.db.models import Platform
    from app.db.repositories import BusinessRepo, ChannelRepo, UserRepo

    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    async with db.sessionmaker.begin() as session:
        user = await UserRepo(session).get_or_create(991, "Тест")
        repo = BusinessRepo(session)
        legacy = await repo.create(user.id, "Мой бизнес")
        await ChannelRepo(session).upsert(legacy.id, Platform.TELEGRAM, "-1001234567890")
        old_default = await repo.create(user.id, "Салаватстекло")
        old_default.settings = {"miniapp_private": True, "default_placeholder": True}
        session.add(MiniUserState(user_id=user.id, selected_id=legacy.id, default_created=True))
        user.business_id = legacy.id
        default_id = old_default.id
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    container.bot = SimpleNamespace(
        get_chat=AsyncMock(
            return_value=SimpleNamespace(
                id=-1001234567890, type="channel", username="testglass", title="Канал"
            )
        ),
        me=AsyncMock(return_value=SimpleNamespace(id=123456)),
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="administrator", can_post_messages=True)
        ),
    )
    app = web.Application()
    MiniApp(container).register(app)
    async with TestClient(TestServer(app)) as client:
        headers = {"Authorization": "Telegram " + signed_user(991)}
        response = await client.get("/miniapp/api/state", headers=headers)
        assert response.status == 200
        state = await response.json()
        assert state["active"] == default_id
        (profile,) = state["profiles"]
        assert profile["name"] == "Салават Стекло" and profile["is_default"]
        assert profile["channel"] == "@testglass"
        response = await client.get("/miniapp/api/state", headers=headers)
        assert response.status == 200
        container.bot.get_chat.assert_awaited_once_with(-1001234567890)

        status_url = f"/miniapp/api/channel-status?company={default_id}"
        response = await client.get(status_url, headers=headers)
        assert await response.json() == {
            "ok": True,
            "message": "Подключено",
            "label": "@testglass",
        }
        container.bot.get_chat_member.assert_awaited_once_with(-1001234567890, 123456)

        container.bot.get_chat_member.return_value.can_post_messages = False
        response = await client.get(status_url, headers=headers)
        assert (await response.json())["message"] == "Нет права публиковать"

        container.bot.get_chat.side_effect = TelegramBadRequest(
            method=GetChat(chat_id=-1001234567890), message="chat not found"
        )
        response = await client.get(status_url, headers=headers)
        assert (await response.json())["message"] == "Канал не найден или недоступен боту"

        outsider = {"Authorization": "Telegram " + signed_user(992)}
        response = await client.get(status_url, headers=outsider)
        assert response.status == 404
