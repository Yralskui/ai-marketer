"""ВКонтакте: публикация, правка, подписчики и просмотры — на подделанном VK API.

Реального ключа в тестах нет: сервер ВК изображает `FakeVK` через httpx.MockTransport. Так
проверяются и порядок вызовов API, и повторы, и понятные ошибки.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from app.config import Settings, parse_vk_communities
from app.container import build_container
from app.db.models import Channel, Platform, Post, PostStatus
from app.db.repositories import ChannelRepo
from app.publishers.analytics import AnalyticsError
from app.publishers.base import Publisher, PublishError
from app.publishers.vk import VKPublisher, group_id_of, parse_community
from app.publishers.vk_analytics import VKAnalytics
from app.publishers.vk_api import VKClient, VKClients, VKError
from app.services import posts, social_metrics, vk_channel
from app.services.publishing import PublishingService
from app.utils.time import utcnow
from app.web.miniapp import MiniApp
from tests.test_miniapp import BOT_TOKEN, signed_user

GROUP = 777
TOKEN = "vk1.a.secret-test-token"
ERROR_27 = {
    "error": {
        "error_code": 27,
        "error_msg": "Group authorization failed: method is unavailable with group auth.",
    }
}
# Методы, которые ВК не пускает с ключом сообщества (подтверждено на живом ключе)
COMMUNITY_KEY_FORBIDDEN = {
    "photos.getWallUploadServer",
    "photos.saveWallPhoto",
    "wall.getById",
    "wall.get",
    "wall.edit",
    "wall.delete",
}


async def no_sleep(_seconds: float) -> None:
    return None


class FakeVK:
    """Минимальный двойник VK API: хранит записи стены и пишет журнал вызовов."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.wall: dict[int, dict] = {}
        self.members = 1234
        self.kind = "page"
        self.wall_state = 1
        self.fail: dict[str, list[dict | Exception]] = {}
        self.next_id = 100
        # Режим «ключ сообщества»: ровно то, что ВК ответил на живом ключе (проверено 14.11.2026)
        self.restricted = False
        # HTTP-статусы, которыми сервер загрузки ответит первыми (например, 405 как на живом ВК)
        self.upload_statuses: list[int] = []

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def params(self, method: str) -> dict[str, str]:
        return next(params for name, params in reversed(self.calls) if name == method)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "upload.vk.test":
            self.calls.append(("upload", {"path": request.url.path}))
            if self.upload_statuses:
                return httpx.Response(self.upload_statuses.pop(0), text="Not Allowed")
            return httpx.Response(200, json={"server": 5, "photo": '[{"x":1}]', "hash": "h"})
        method = request.url.path.rsplit("/", 1)[-1]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((method, form))
        if self.restricted and method in COMMUNITY_KEY_FORBIDDEN:
            return httpx.Response(200, json=ERROR_27)
        if method == "groups.getTokenPermissions":
            # Права отдаёт только ключ сообщества; токен пользователя получает ошибку 27
            if not self.restricted:
                return httpx.Response(200, json=ERROR_27)
            return httpx.Response(200, json={"response": {"mask": 1, "permissions": []}})
        queued = self.fail.get(method)
        if queued:
            outcome = queued.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(200, json=outcome)
        return httpx.Response(200, json=self.respond(method, form))

    def respond(self, method: str, form: dict[str, str]) -> dict:
        if method == "groups.getById":
            return {
                "response": {
                    "groups": [
                        {
                            "id": GROUP,
                            "name": "САЛАВАТСТЕКЛО",
                            "screen_name": "salavatsteklo",
                            "type": self.kind,
                            "members_count": self.members,
                            "wall": self.wall_state,
                        }
                    ]
                }
            }
        if method == "photos.getWallUploadServer":
            return {"response": {"upload_url": "https://upload.vk.test/upload"}}
        if method == "photos.saveWallPhoto":
            return {"response": [{"id": 55, "owner_id": -GROUP}]}
        if method == "wall.post":
            post_id, self.next_id = self.next_id, self.next_id + 1
            attachments = form.get("attachments", "")
            self.wall[post_id] = {
                "id": post_id,
                "text": form["message"],
                "views": {"count": 10 * post_id},
                "reposts": {"count": 2},
                "attachments": [
                    {"type": "photo", "photo": {"owner_id": -GROUP, "id": int(a.split("_")[1])}}
                    if a.startswith("photo")
                    else {"type": "link", "link": {"url": a}}
                    for a in attachments.split(",")
                    if a
                ],
            }
            return {"response": {"post_id": post_id}}
        if method == "wall.getById":
            ids = [int(ref.rsplit("_", 1)[1]) for ref in form["posts"].split(",")]
            return {"response": {"items": [self.wall[i] for i in ids if i in self.wall]}}
        if method == "wall.edit":
            self.wall[int(form["post_id"])]["text"] = form["message"]
            return {"response": 1}
        return {"error": {"error_code": 100, "error_msg": f"unknown method {method}"}}


@pytest.fixture
def vk() -> FakeVK:
    return FakeVK()


@pytest.fixture
def clients(vk: FakeVK) -> VKClients:
    return VKClients({GROUP: TOKEN}, transport=vk.transport(), sleep=no_sleep)


@pytest.fixture
def publisher(clients: VKClients) -> VKPublisher:
    return VKPublisher(clients)


def channel() -> Channel:
    return Channel(id=1, business_id=1, platform=Platform.VK, external_id=str(GROUP))


def post(**kwargs) -> Post:
    defaults = {"id": 7, "business_id": 1, "platform": Platform.VK, "text": "Привет, ВК!"}
    return Post(**{**defaults, **kwargs})


# ----------------------------------------------------------------------------- адреса и настройки


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://vk.com/salavatsteklo", "salavatsteklo"),
        ("vk.com/club777", 777),
        ("https://m.vk.com/public777?from=x", 777),
        ("https://vk.ru/event777/", 777),
        ("club777", 777),
        ("-777", 777),
        ("777", 777),
        ("@salavatsteklo", "salavatsteklo"),
        ("wall-777_12", 777),
    ],
)
def test_parse_community(raw, expected):
    assert parse_community(raw) == expected


def test_parse_community_rejects_garbage():
    with pytest.raises(VKError):
        parse_community("не сообщество!")


def test_vk_communities_setting():
    assert parse_vk_communities("") == {}
    assert parse_vk_communities("777:tok, -888:tok2 ,") == {777: "tok", 888: "tok2"}
    with pytest.raises(ValueError, match="VK_COMMUNITIES") as error:
        parse_vk_communities("777")
    assert "tok" not in str(error.value)
    with pytest.raises(ValueError):
        Settings(_env_file=None, vk_communities="abc:tok")
    assert Settings(_env_file=None, vk_communities="777:tok").vk_tokens == {777: "tok"}


def test_group_id_of_channel():
    assert group_id_of(channel()) == GROUP
    with pytest.raises(PublishError):
        group_id_of(Channel(external_id="@public"))


# ----------------------------------------------------------------------------- клиент API


async def test_client_retries_rate_limit_then_succeeds(vk):
    vk.fail["wall.post"] = [{"error": {"error_code": 6, "error_msg": "Too many requests"}}] * 2
    client = VKClient(TOKEN, transport=vk.transport(), sleep=no_sleep)
    assert (await client.call("wall.post", {"message": "x"}))["post_id"] == 100
    assert vk.methods().count("wall.post") == 3


async def test_client_gives_up_after_retries(vk):
    vk.fail["x.y"] = [{"error": {"error_code": 9, "error_msg": "Flood control"}}] * 9
    client = VKClient(TOKEN, max_retries=2, transport=vk.transport(), sleep=no_sleep)
    with pytest.raises(VKError) as error:
        await client.call("x.y")
    assert error.value.code == 9 and vk.methods().count("x.y") == 3


async def test_client_does_not_resend_writes_after_network_error(vk):
    vk.fail["wall.post"] = [httpx.ConnectError("boom")]
    client = VKClient(TOKEN, transport=vk.transport(), sleep=no_sleep)
    with pytest.raises(VKError) as error:
        await client.call("wall.post", {"message": "x"})
    assert error.value.network and vk.methods().count("wall.post") == 1


async def test_client_resends_reads_after_network_error(vk):
    vk.fail["groups.getById"] = [httpx.ConnectError("boom")]
    client = VKClient(TOKEN, transport=vk.transport(), sleep=no_sleep)
    await client.call("groups.getById", {"group_id": GROUP}, idempotent=True)
    assert vk.methods().count("groups.getById") == 2


async def test_client_translates_access_errors(vk):
    vk.fail["wall.post"] = [{"error": {"error_code": 5, "error_msg": "User authorization failed"}}]
    client = VKClient(TOKEN, transport=vk.transport(), sleep=no_sleep)
    with pytest.raises(VKError, match="VK_COMMUNITIES"):
        await client.call("wall.post")


async def test_token_is_sent_but_never_in_errors(vk):
    vk.fail["wall.post"] = [{"error": {"error_code": 100, "error_msg": "bad"}}]
    client = VKClient(TOKEN, transport=vk.transport(), sleep=no_sleep)
    with pytest.raises(VKError) as error:
        await client.call("wall.post", {"message": "x"})
    assert vk.params("wall.post")["access_token"] == TOKEN
    assert TOKEN not in str(error.value)


# ----------------------------------------------------------------------------- публикация


async def test_publish_text_post(publisher, vk):
    result = await publisher.publish(post(), channel())
    assert result.external_id == "100"
    assert result.url == f"https://vk.com/wall-{GROUP}_100"
    sent = vk.params("wall.post")
    assert sent["owner_id"] == f"-{GROUP}" and sent["from_group"] == "1"
    assert sent["message"] == "Привет, ВК!" and sent["guid"] == "aimkt-7"
    assert "attachments" not in sent
    assert vk.methods() == ["wall.post"]


async def test_publish_post_with_photo(publisher, vk, tmp_path):
    image = tmp_path / "card.png"
    Image.new("RGB", (8, 8)).save(image)
    result = await publisher.publish(post(image_path=str(image)), channel())
    assert vk.methods() == [
        "photos.getWallUploadServer",
        "upload",
        "photos.saveWallPhoto",
        "wall.post",
    ]
    assert vk.params("photos.getWallUploadServer")["group_id"] == str(GROUP)
    assert vk.params("photos.saveWallPhoto")["hash"] == "h"
    assert vk.params("wall.post")["attachments"] == f"photo-{GROUP}_55"
    assert vk.wall[int(result.external_id)]["attachments"]


async def test_missing_image_file_publishes_text_only(publisher, vk):
    await publisher.publish(post(image_path="/no/such/file.png"), channel())
    assert vk.methods() == ["wall.post"]


async def test_unexpected_photo_error_stops_publication(publisher, vk, tmp_path):
    """Сбой картинки не из-за прав (например, плохой файл) — пост не публикуем без неё."""
    image = tmp_path / "card.png"
    Image.new("RGB", (8, 8)).save(image)
    vk.fail["photos.saveWallPhoto"] = [{"error": {"error_code": 100, "error_msg": "bad photo"}}]
    with pytest.raises(PublishError, match="картинк"):
        await publisher.publish(post(image_path=str(image)), channel())
    assert "wall.post" not in vk.methods()


async def test_community_key_cannot_show_a_picture_and_says_so(publisher, vk, tmp_path):
    """Живой ключ сообщества (14.11.2026): фото на стену — 27; «фото сообщений» ВК молча
    выбрасывает; вложение-ссылку отвергает; ссылка в тексте остаётся ссылкой. Значит, текст
    выходит как есть, без «обходных» вложений, а человеку объясняют, почему нет картинки."""
    vk.restricted = True
    image = tmp_path / "card.png"
    Image.new("RGB", (8, 8)).save(image)
    result = await publisher.publish(post(image_path=str(image)), channel())
    sent = vk.params("wall.post")
    assert sent["message"] == "Привет, ВК!" and "attachments" not in sent
    assert "без картинки" in result.note and "токен администратора" in result.note
    assert vk.methods() == ["photos.getWallUploadServer", "wall.post"]
    # следующий пост не тратит лишний запрос на заведомый отказ
    await publisher.publish(post(id=8, image_path=str(image)), channel())
    assert vk.methods().count("photos.getWallUploadServer") == 1


async def test_user_token_uses_native_photos(publisher, vk, tmp_path):
    image = tmp_path / "card.png"
    Image.new("RGB", (8, 8)).save(image)
    await publisher.publish(post(image_path=str(image)), channel())
    assert vk.params("wall.post")["attachments"] == f"photo-{GROUP}_55"  # настоящее фото


async def test_upload_retries_with_a_fresh_address_after_405(publisher, vk, tmp_path):
    """Сервер загрузки ВК иногда отвечает 405 — берём новый адрес и пробуем снова."""
    vk.upload_statuses = [405]
    image = tmp_path / "card.png"
    Image.new("RGB", (8, 8)).save(image)
    result = await publisher.publish(post(image_path=str(image)), channel())
    assert result.note is None and vk.methods().count("photos.getWallUploadServer") == 2
    assert vk.methods().count("upload") == 2


async def test_text_goes_out_with_a_note_without_a_site_address(publisher, vk, tmp_path):
    """Нет адреса сайта для карточки — текст выходит без картинки и с пояснением."""
    vk.restricted = True
    image = tmp_path / "card.png"
    Image.new("RGB", (8, 8)).save(image)
    result = await publisher.publish(post(image_path=str(image)), channel())
    assert "без картинки" in result.note and "токен администратора" in result.note
    assert "attachments" not in vk.params("wall.post")
    assert vk.methods()[-1] == "wall.post"


async def test_user_token_publishes_with_photo_and_no_note(publisher, vk, tmp_path):
    image = tmp_path / "card.png"
    Image.new("RGB", (8, 8)).save(image)
    result = await publisher.publish(post(image_path=str(image)), channel())
    assert result.note is None and vk.params("wall.post")["attachments"]


async def test_publish_network_error_warns_about_possible_duplicate(publisher, vk):
    vk.fail["wall.post"] = [httpx.ConnectError("boom")]
    with pytest.raises(PublishError, match="могла выйти"):
        await publisher.publish(post(), channel())


async def test_publish_api_error_is_readable(publisher, vk):
    vk.fail["wall.post"] = [{"error": {"error_code": 214, "error_msg": "Access to adding post"}}]
    with pytest.raises(PublishError, match="закрытая стена"):
        await publisher.publish(post(), channel())


async def test_publish_without_token_explains_what_to_do():
    publisher = VKPublisher(VKClients({}))
    with pytest.raises(PublishError, match="VK_COMMUNITIES"):
        await publisher.publish(post(), channel())


async def test_long_text_is_cut_to_vk_limit(publisher, vk):
    await publisher.publish(post(text="а" * 20_000), channel())
    assert len(vk.params("wall.post")["message"]) == 15_000


# ----------------------------------------------------------------------------- правка записи


async def test_edit_changes_text_and_keeps_photo(publisher, vk, tmp_path):
    image = tmp_path / "card.png"
    Image.new("RGB", (8, 8)).save(image)
    published = await publisher.publish(post(image_path=str(image)), channel())
    live = post(external_id=published.external_id, status=PostStatus.PUBLISHED)
    await publisher.edit(live, channel(), "Новый текст")
    sent = vk.params("wall.edit")
    assert sent["message"] == "Новый текст" and sent["post_id"] == published.external_id
    assert sent["attachments"] == f"photo-{GROUP}_55"  # картинка не пропала
    assert vk.wall[int(published.external_id)]["text"] == "Новый текст"


async def test_edit_of_deleted_record_is_reported(publisher):
    with pytest.raises(PublishError, match="удалена"):
        await publisher.edit(post(external_id="999"), channel(), "текст")


async def test_edit_when_wall_cannot_be_read_changes_only_text(publisher, vk):
    """Ключ без чтения стены (27): у наших записей вложений нет, правим один текст."""
    published = await publisher.publish(post(), channel())
    vk.fail["wall.getById"] = [ERROR_27]
    await publisher.edit(post(external_id=published.external_id), channel(), "Новый текст")
    assert "attachments" not in vk.params("wall.edit")
    assert vk.wall[int(published.external_id)]["text"] == "Новый текст"


async def test_edit_with_community_key_explains_the_limit(publisher, vk):
    """На живом ключе сообщества wall.edit закрыт (27): понятная ошибка, пост не меняется."""
    published = await publisher.publish(post(), channel())
    vk.restricted = True
    with pytest.raises(PublishError, match="токен администратора"):
        await publisher.edit(post(external_id=published.external_id), channel(), "Другой текст")
    assert vk.wall[int(published.external_id)]["text"] == "Привет, ВК!"


async def test_edit_needs_vk_record_number(publisher):
    with pytest.raises(PublishError, match="номера записи"):
        await publisher.edit(post(external_id="dry-run-7"), channel(), "текст")


async def test_default_publisher_does_not_support_edit():
    class Other(Publisher):
        platform = Platform.MAX

        async def publish(self, post, channel):  # pragma: no cover
            raise NotImplementedError

    with pytest.raises(PublishError, match="не поддерживает"):
        await Other().edit(post(), channel(), "x")


# ----------------------------------------------------------------------------- проверка сообщества


async def test_check_channel_page_shows_subscribers(publisher):
    check = await publisher.check_channel(channel())
    assert check.ok
    assert "САЛАВАТСТЕКЛО" in check.message and "1234" in check.message
    assert "публичная страница" in check.message
    assert "Токен администратора" in check.message  # токен пользователя — все функции


async def test_check_channel_new_style_community_is_not_warned(publisher, vk):
    """Новые сообщества ВК отдают type=group, но это нормальное сообщество с лентой."""
    vk.kind = "group"
    check = await publisher.check_channel(channel())
    assert check.ok and "сообщество" in check.message
    assert "чат" not in check.message


async def test_check_channel_says_what_a_community_key_cannot_do(publisher, vk):
    vk.restricted = True
    check = await publisher.check_channel(channel())
    assert check.ok and "Ключ сообщества" in check.message
    assert "картинки, правка записей и просмотры недоступны" in check.message
    assert "правка записей и просмотры недоступны" in check.message


@pytest.mark.parametrize(("state", "word"), [(3, "закрыта"), (0, "выключена")])
async def test_check_channel_wall_unavailable(publisher, vk, state, word):
    vk.wall_state = state
    check = await publisher.check_channel(channel())
    assert not check.ok and f"стена {word}" in check.message


async def test_check_channel_without_token():
    check = await VKPublisher(VKClients({})).check_channel(channel())
    assert not check.ok and "VK_COMMUNITIES" in check.message


async def test_resolve_by_link_needs_a_key(publisher):
    community = await publisher.resolve("https://vk.com/salavatsteklo")
    assert (community.id, community.name) == (GROUP, "САЛАВАТСТЕКЛО")
    # Сообщество найдено, но ключа для него в настройках нет
    other = VKPublisher(VKClients({1: "x"}, transport=publisher.clients._transport, sleep=no_sleep))
    with pytest.raises(PublishError, match="нет ключа доступа"):
        await other.resolve("salavatsteklo")


# ----------------------------------------------------------------------------- аналитика


async def test_analytics_subscribers_and_views(clients, vk, publisher):
    first = await publisher.publish(post(id=1), channel())
    second = await publisher.publish(post(id=2), channel())
    analytics = VKAnalytics(clients)
    assert (await analytics.channel_metrics(str(GROUP))).subscribers == 1234
    metrics = await analytics.post_metrics(
        str(GROUP), [first.external_id, second.external_id, "dry-run-3"]
    )
    assert metrics[first.external_id].views == 1000 and metrics[first.external_id].forwards == 2
    assert metrics[second.external_id].views == 1010
    assert "dry-run-3" not in metrics


async def test_analytics_marks_removed_posts(clients, publisher, vk):
    published = await publisher.publish(post(), channel())
    del vk.wall[int(published.external_id)]
    metrics = await VKAnalytics(clients).post_metrics(str(GROUP), [published.external_id])
    assert metrics[published.external_id].deleted


async def test_analytics_errors_are_not_deletions(clients, vk):
    vk.fail["wall.getById"] = [{"error": {"error_code": 15, "error_msg": "Access denied"}}]
    with pytest.raises(AnalyticsError):
        await VKAnalytics(clients).post_metrics(str(GROUP), ["100"])
    vk.fail["groups.getById"] = [{"error": {"error_code": 5, "error_msg": "bad token"}}]
    with pytest.raises(AnalyticsError, match="подписчиков"):
        await VKAnalytics(clients).channel_metrics(str(GROUP))


async def test_hourly_collection_stores_vk_numbers(db, session, owner, clients, publisher):
    _, company = owner
    link = await ChannelRepo(session).upsert(company.id, Platform.VK, str(GROUP), "САЛАВАТСТЕКЛО")
    start = utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    result = await publisher.publish(post(), channel())
    session.add(
        Post(
            business_id=company.id,
            channel_id=link.id,
            platform=Platform.VK,
            text="Опубликован",
            status=PostStatus.PUBLISHED,
            external_id=result.external_id,
            published_at=start,
        )
    )
    await session.commit()
    await social_metrics.collect_hour(db.sessionmaker, {Platform.VK: VKAnalytics(clients)}, start)
    stored = await social_metrics.series(session, company.id, start, utcnow(), None)
    assert stored["channels"][0]["name"] == "САЛАВАТСТЕКЛО"
    assert stored["channels"][0]["points"][0][1] == 1234
    assert stored["posts"], "у поста появились просмотры"


# ----------------------------------------------------------------------------- сервисы


async def test_connect_and_disconnect_community(session, owner, publisher):
    _, company = owner
    link, check = await vk_channel.connect(session, publisher, company, "https://vk.com/club777")
    assert link.external_id == str(GROUP) and link.title == "САЛАВАТСТЕКЛО" and check.ok
    assert (await ChannelRepo(session).get_active(company.id, Platform.VK)).id == link.id
    await vk_channel.disconnect(session, company)
    assert await ChannelRepo(session).get_active(company.id, Platform.VK) is None


async def test_connect_errors_are_user_friendly(session, owner, publisher, vk):
    _, company = owner
    with pytest.raises(vk_channel.VKConnectError, match="не настроен"):
        await vk_channel.connect(session, VKPublisher(VKClients({})), company, "club777")
    with pytest.raises(vk_channel.VKConnectError, match="не настроен"):
        await vk_channel.connect(session, None, company, "club777")
    vk.wall_state = 3
    with pytest.raises(vk_channel.VKConnectError, match="стена закрыта"):
        await vk_channel.connect(session, publisher, company, "club777")
    assert await ChannelRepo(session).get_active(company.id, Platform.VK) is None


async def _published_vk_post(session, company, publisher):
    link = await ChannelRepo(session).upsert(company.id, Platform.VK, str(GROUP), "САЛАВАТСТЕКЛО")
    item = Post(
        business_id=company.id,
        channel_id=link.id,
        platform=Platform.VK,
        text="Было",
        status=PostStatus.APPROVED,
    )
    session.add(item)
    await session.commit()
    service = PublishingService({Platform.VK: publisher})
    outcome = await service.publish(session, item)
    assert outcome.ok, outcome.error
    return service, item


async def test_edit_published_post_updates_vk_and_history(session, owner, publisher, vk):
    _, company = owner
    service, item = await _published_vk_post(session, company, publisher)
    await service.edit_published(session, item, "  Стало лучше  ", author_id=1)
    assert item.text == "Стало лучше" and item.status == PostStatus.PUBLISHED
    assert vk.wall[int(item.external_id)]["text"] == "Стало лучше"
    revisions = await posts.PostRepo(session).list_revisions(item.id)
    assert revisions[-1].text == "Стало лучше"


async def test_edit_published_rules(session, owner, publisher, vk):
    _, company = owner
    service, item = await _published_vk_post(session, company, publisher)
    with pytest.raises(ValueError, match="пустым"):
        await service.edit_published(session, item, "   ")
    with pytest.raises(ValueError, match="лимита"):
        await service.edit_published(session, item, "а" * 16_000)
    calls = len(vk.calls)
    assert await service.edit_published(session, item, item.text) is item
    assert len(vk.calls) == calls  # тот же текст — в ВК не ходим
    vk.fail["wall.edit"] = [{"error": {"error_code": 15, "error_msg": "Access denied"}}]
    with pytest.raises(PublishError):
        await service.edit_published(session, item, "Другой текст")
    assert item.text == "Было"  # ошибка ВК не меняет пост у нас
    draft = Post(business_id=company.id, platform=Platform.VK, text="x", status=PostStatus.DRAFT)
    with pytest.raises(ValueError, match="опубликованный"):
        await service.edit_published(session, draft, "y")


async def test_edit_published_in_dry_run_stays_local(session, owner, publisher, vk):
    _, company = owner
    item = Post(
        business_id=company.id,
        platform=Platform.VK,
        text="Было",
        status=PostStatus.PUBLISHED,
        external_id="dry-run-1",
    )
    session.add(item)
    await session.commit()
    await PublishingService({Platform.VK: publisher}, dry_run=True).edit_published(
        session, item, "Стало"
    )
    assert item.text == "Стало" and not vk.calls


async def test_telegram_posts_cannot_be_edited_remotely(session, owner):
    _, company = owner
    item = Post(
        business_id=company.id,
        platform=Platform.TELEGRAM,
        text="Было",
        status=PostStatus.PUBLISHED,
        external_id="5",
    )
    session.add(item)
    await ChannelRepo(session).upsert(company.id, Platform.TELEGRAM, "@x")
    await session.commit()

    class TelegramLike(Publisher):
        platform = Platform.TELEGRAM

        async def publish(self, post, channel):  # pragma: no cover
            raise NotImplementedError

    service = PublishingService({Platform.TELEGRAM: TelegramLike()})
    with pytest.raises(PublishError, match="не поддерживает"):
        await service.edit_published(session, item, "Стало")
    assert item.text == "Было"


async def test_clone_post_for_vk(session, owner):
    _, company = owner
    original = Post(
        business_id=company.id,
        platform=Platform.TELEGRAM,
        topic="Тема",
        text="Текст " * 3000,
        image_path="/tmp/x.png",
        status=PostStatus.PUBLISHED,
    )
    session.add(original)
    await ChannelRepo(session).upsert(company.id, Platform.VK, str(GROUP))
    await session.commit()
    copy = await posts.clone_for_platform(session, original, Platform.VK, author_id=1)
    assert copy.id != original.id and copy.platform == Platform.VK
    assert copy.status == PostStatus.DRAFT and copy.topic == "Тема"
    assert copy.image_path == "/tmp/x.png" and len(copy.text) <= 15_000
    assert copy.channel_id is not None
    with pytest.raises(ValueError, match="уже предназначен"):
        await posts.clone_for_platform(session, copy, Platform.VK)


# ----------------------------------------------------------------------------- Mini App


async def test_miniapp_vk_flow(settings, db, vk):
    """Подключить сообщество → копия поста для ВК → публикация → правка → метрики-ссылка."""
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    settings.vk_communities = f"{GROUP}:{TOKEN}"
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    container.publishing.publishers[Platform.VK] = VKPublisher(
        VKClients({GROUP: TOKEN}, transport=vk.transport(), sleep=no_sleep)
    )
    app = web.Application()
    MiniApp(container).register(app)
    headers = {"Authorization": "Telegram " + signed_user(601)}
    api = "/miniapp/api/"
    async with TestClient(TestServer(app)) as client:
        state = await (await client.get(api + "state", headers=headers)).json()
        company = state["active"]
        assert state["profiles"][0]["vk_enabled"] and state["profiles"][0]["vk_channel"] == ""

        # без сообщества копию делать нельзя
        chat = await client.post(
            api + "chat", headers=headers, json={"company": company, "text": "Напиши пост про окна"}
        )
        assert chat.status == 200
        state = await (await client.get(api + "state", headers=headers)).json()
        source = state["posts"][0]
        response = await client.post(
            api + "post",
            headers=headers,
            json={
                "company": company,
                "id": source["id"],
                "version": source["version"],
                "action": "copy_to_vk",
            },
        )
        assert response.status == 400 and "подключите" in (await response.json())["error"]

        # неверная ссылка — понятная ошибка, ничего не сохраняется
        response = await client.patch(
            api + "profile", headers=headers, json={"company": company, "vk_channel": "не!ссылка"}
        )
        assert response.status == 400

        response = await client.patch(
            api + "profile",
            headers=headers,
            json={"company": company, "vk_channel": "https://vk.com/salavatsteklo"},
        )
        assert response.status == 200, await response.text()
        state = await (await client.get(api + "state", headers=headers)).json()
        assert state["profiles"][0]["vk_channel"] == "САЛАВАТСТЕКЛО"

        # диагностика видит сообщество
        diagnostics = await (await client.get(api + "diagnostics", headers=headers)).json()
        assert diagnostics["vk_check"]["ok"] and "1234" in diagnostics["vk_check"]["message"]

        # копия для ВК появляется как обычный черновик на согласовании
        source = state["posts"][0]
        response = await client.post(
            api + "post",
            headers=headers,
            json={
                "company": company,
                "id": source["id"],
                "version": source["version"],
                "action": "copy_to_vk",
            },
        )
        assert response.status == 200, await response.text()
        state = await (await client.get(api + "state", headers=headers)).json()
        copy = next(p for p in state["posts"] if p["platform"] == "vk")
        assert copy["status"] == "pending_approval" and copy["text"] == source["text"]

        # публикация во ВК — только по кнопке; запись появляется на стене сообщества
        assert "wall.post" not in vk.methods()
        response = await client.post(
            api + "post",
            headers=headers,
            json={
                "company": company,
                "id": copy["id"],
                "version": copy["version"],
                "action": "publish",
            },
        )
        assert response.status == 200, await response.text()
        assert vk.wall and vk.params("wall.post")["owner_id"] == f"-{GROUP}"

        # история знает платформу и ссылку, а правка меняет и запись в ВК
        history = (await (await client.get(api + "history", headers=headers)).json())["posts"]
        published = next(p for p in history if p["platform"] == "vk")
        assert published["url"].startswith("https://vk.com/wall-")
        response = await client.post(
            api + "post",
            headers=headers,
            json={
                "company": company,
                "id": published["id"],
                "version": published["version"],
                "action": "edit_published",
                "text": "Исправленный текст",
            },
        )
        assert response.status == 200, await response.text()
        assert next(iter(vk.wall.values()))["text"] == "Исправленный текст"

        # отключение сообщества
        response = await client.patch(
            api + "profile", headers=headers, json={"company": company, "vk_channel": ""}
        )
        assert response.status == 200
        state = await (await client.get(api + "state", headers=headers)).json()
        assert state["profiles"][0]["vk_channel"] == ""


async def test_miniapp_publish_with_community_key_explains_missing_picture(settings, db, vk):
    """Ваш случай: ключ сообщества, пост с картинкой. Текст выходит, Mini App объясняет, почему
    без картинки; правка живой записи (закрыта ВК) даёт понятную ошибку."""
    vk.restricted = True
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    settings.vk_communities = f"{GROUP}:{TOKEN}"
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    container.publishing.publishers[Platform.VK] = VKPublisher(
        VKClients({GROUP: TOKEN}, transport=vk.transport(), sleep=no_sleep)
    )
    app = web.Application()
    MiniApp(container).register(app)
    headers = {"Authorization": "Telegram " + signed_user(602)}
    api = "/miniapp/api/"

    def body(company, item, action, **extra):
        return {
            "company": company,
            "id": item["id"],
            "version": item["version"],
            "action": action,
            **extra,
        }

    async with TestClient(TestServer(app)) as client:
        state = await (await client.get(api + "state", headers=headers)).json()
        company = state["active"]
        response = await client.patch(
            api + "profile",
            headers=headers,
            json={"company": company, "vk_channel": "https://vk.ru/club777"},
        )
        assert response.status == 200, await response.text()

        # пост с фирменной картинкой (повод из календаря) → копия для ВК → публикация
        key = state["occasions"][0]["key"]
        await client.post(api + "occasion", headers=headers, json={"company": company, "key": key})
        state = await (await client.get(api + "state", headers=headers)).json()
        source = next(p for p in state["posts"] if p["has_image"])
        response = await client.post(
            api + "post", headers=headers, json=body(company, source, "copy_to_vk")
        )
        assert response.status == 200
        state = await (await client.get(api + "state", headers=headers)).json()
        copy = next(p for p in state["posts"] if p["platform"] == "vk")
        assert copy["has_image"]
        response = await client.post(
            api + "post", headers=headers, json=body(company, copy, "publish")
        )
        assert response.status == 200, await response.text()
        assert "без картинки" in (await response.json())["note"]
        assert vk.wall and "attachments" not in vk.params("wall.post")

        # правка живой записи недоступна ключу сообщества — понятная ошибка, текст не меняется
        history = (await (await client.get(api + "history", headers=headers)).json())["posts"]
        published = next(p for p in history if p["platform"] == "vk")
        response = await client.post(
            api + "post",
            headers=headers,
            json=body(company, published, "edit_published", text="Новый текст"),
        )
        assert response.status == 400
        assert "токен администратора" in (await response.json())["error"]

        diagnostics = await (await client.get(api + "diagnostics", headers=headers)).json()
        assert "Ключ сообщества" in diagnostics["vk_check"]["message"]
