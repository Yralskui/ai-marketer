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
        assert a["profiles"][0]["name"] == b["profiles"][0]["name"] == "Салаватстекло"
        assert a["active"] != b["active"]
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
        assert (await request(1, "DELETE", "profile", {"company": new_id}))[0] == 200
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
        response = await client.get(base + "post-pool", headers=headers)
        post_id = (await response.json())["posts"][0]["id"]
        for action, extra in [
            ("approve", {}),
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
        one, two, three = (await response.json())["versions"]
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
