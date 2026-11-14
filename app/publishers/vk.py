"""Публикация во ВКонтакте: запись на стену сообщества, правка записи, проверка сообщества.

Нужно именно сообщество с лентой новостей (публичная страница, «Бизнес» и подобные), а не
группа-чат с участниками: записи сообщества выходят в ленту подписчиков. Запись публикуется
от имени сообщества (`from_group=1`). Публикатор не проверяет согласование — это делает
services/publishing.py, как и для Telegram.

Настройка: VK_COMMUNITIES=<id сообщества>:<ключ>[,<id>:<ключ>…], см. docs/VK.md.
Канал компании в базе: platform=vk, external_id=<числовой id сообщества>.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db.models import Channel, Platform, Post
from app.publishers.base import ChannelCheck, Publisher, PublishError, PublishResult
from app.publishers.vk_api import VKClients, VKError
from app.services import public_posts
from app.services.posts import PLATFORM_HARD_LIMITS

log = logging.getLogger(__name__)

_PREFIX_RE = re.compile(r"^(?:https?://)?(?:m\.)?(?:vk\.com|vk\.ru)/", re.IGNORECASE)
_ID_RE = re.compile(r"^-?(\d+)$")
_PAGE_RE = re.compile(r"^(?:club|public|event)(\d+)$", re.IGNORECASE)
_WALL_RE = re.compile(r"^wall-(\d+)_\d+$", re.IGNORECASE)
_NAME_RE = re.compile(r"^[A-Za-z0-9_.]{2,64}$")

_KINDS = {
    "page": "публичная страница",
    "group": "сообщество",  # новые сообщества ВКонтакте — с лентой, API отдаёт им type=group
    "event": "мероприятие",
}


@dataclass(frozen=True)
class VKCommunity:
    id: int
    name: str
    kind: str
    members: int | None = None


def parse_community(raw: str) -> int | str:
    """Адрес сообщества → числовой id (int) или короткое имя (str) для поиска через API.

    Принимает: https://vk.com/salavatsteklo, vk.com/club123, club123, public123, -123, 123.
    """
    value = _PREFIX_RE.sub("", raw.strip()).split("?")[0].strip("/ @")
    for pattern in (_ID_RE, _PAGE_RE, _WALL_RE):
        match = pattern.match(value)
        if match:
            return int(match.group(1))
    if _NAME_RE.match(value):
        return value
    raise VKError(0, "Не похоже на сообщество ВКонтакте. Укажите ссылку вида vk.com/название")


def group_id_of(channel: Channel) -> int:
    match = _ID_RE.match(channel.external_id.strip())
    if not match:
        raise PublishError(f"Некорректный id сообщества ВКонтакте: {channel.external_id!r}")
    return int(match.group(1))


def _first_group(response: Any) -> dict[str, Any]:
    """groups.getById: в новых версиях API ответ — {"groups": [...]}, в старых — список."""
    groups = response.get("groups") if isinstance(response, dict) else response
    if not groups:
        raise VKError(100, "Сообщество не найдено")
    return groups[0]


def _community(info: dict[str, Any]) -> VKCommunity:
    return VKCommunity(
        id=int(info["id"]),
        name=str(info.get("name") or info.get("screen_name") or info["id"]),
        kind=str(info.get("type") or "group"),
        members=info.get("members_count"),
    )


class VKPublisher(Publisher):
    platform = Platform.VK

    def __init__(
        self,
        clients: VKClients | None = None,
        *,
        public_url: str = "",
        secret: str = "",
        card_in_text: bool = False,
    ) -> None:
        self.clients = clients or VKClients({})
        # Адрес сайта и секрет для карточек-ссылок: без них картинку без токена админа не показать
        self.public_url = public_url
        self.secret = secret
        # Ссылка на страницу поста в тексте записи (вместо картинки); по умолчанию выключено
        self.card_in_text = card_in_text

    # ------------------------------------------------------------------ подключение

    async def resolve(self, raw: str) -> VKCommunity:
        """Находит сообщество по ссылке или id; ключ для него должен быть в настройках."""
        try:
            parsed = parse_community(raw)
            client = self.clients.get(parsed) if isinstance(parsed, int) else None
            client = client or self.clients.any_client()
            info = _first_group(
                await client.call(
                    "groups.getById",
                    {"group_id": parsed, "fields": "members_count"},
                    idempotent=True,
                )
            )
        except VKError as exc:
            raise PublishError(f"ВКонтакте: {exc}") from exc
        community = _community(info)
        if not self.clients.has(community.id):
            raise PublishError(
                f"Сообщество «{community.name}» найдено, но для него нет ключа доступа: "
                f"добавьте «{community.id}:<ключ>» в VK_COMMUNITIES и перезапустите бота"
            )
        return community

    # ------------------------------------------------------------------ публикация

    def _card_link(self, post: Post) -> str | None:
        """Ссылка на публичную страницу поста — из неё ВК делает карточку с картинкой."""
        if not (self.public_url and self.secret):
            return None
        return f"{self.public_url.rstrip('/')}/p/{public_posts.make_token(self.secret, post.id)}"

    async def publish(self, post: Post, channel: Channel) -> PublishResult:
        group_id = group_id_of(channel)
        try:
            client = self.clients.get(group_id)
        except VKError as exc:
            raise PublishError(f"ВКонтакте: {exc}") from exc
        text = post.text[: PLATFORM_HARD_LIMITS[Platform.VK]]
        attachments: list[str] = []
        note: str | None = None
        link_in_text: str | None = None
        if post.image_path:
            image = Path(post.image_path)
            if not image.exists():
                log.warning("Картинка поста #%s не найдена: %s", post.id, post.image_path)
            else:
                try:
                    attachments.append(await client.upload_wall_photo(group_id, image))
                except VKError as exc:
                    if exc.code not in (15, 27):
                        raise PublishError(
                            f"ВКонтакте: не удалось загрузить картинку — {exc}"
                        ) from exc
                    # Ключ сообщества не загружает фото (проверено на живом ключе). Вложение-
                    # ссылку ВК тоже отвергает: «No photo given», а «фото сообщений» молча
                    # выбрасывает. Остаётся ссылка в тексте — по включённому card_in_text.
                    card = self._card_link(post) if self.card_in_text else None
                    if card is not None:
                        link_in_text = card
                    else:
                        log.warning("VK: картинка поста #%s не загружена: %s", post.id, exc)
                        note = (
                            "Пост вышел без картинки: ВКонтакте не принял загрузку фото ключом "
                            "сообщества. Для картинок может понадобиться токен администратора."
                        )
        if link_in_text:
            limit = PLATFORM_HARD_LIMITS[Platform.VK] - len(link_in_text) - 2
            text = text[:limit] + "\n\n" + link_in_text
        try:
            response = await client.call(
                "wall.post",
                {
                    "owner_id": -group_id,
                    "from_group": 1,
                    "message": text,
                    "attachments": ",".join(attachments),
                    # Защита от дубля: ВК не примет вторую запись с тем же guid в течение часа
                    "guid": f"aimkt-{post.id}",
                },
            )
        except VKError as exc:
            if exc.network:
                raise PublishError(
                    f"нет связи с ВКонтакте ({exc}). Запись могла выйти — проверьте стену "
                    "сообщества, прежде чем публиковать повторно"
                ) from exc
            raise PublishError(f"ВКонтакте: {exc}") from exc
        post_id = response["post_id"] if isinstance(response, dict) else response
        return PublishResult(
            external_id=str(post_id), url=f"https://vk.com/wall-{group_id}_{post_id}", note=note
        )

    async def edit(self, post: Post, channel: Channel, text: str) -> None:
        """Меняет текст записи на стене, сохраняя её вложения (картинку)."""
        group_id = group_id_of(channel)
        if not post.external_id or not post.external_id.isdecimal():
            raise PublishError("У поста нет номера записи ВКонтакте — править нечего")
        try:
            client = self.clients.get(group_id)
            attachments: list[str] | None = None
            try:
                current = await client.call(
                    "wall.getById",
                    {"posts": f"-{group_id}_{post.external_id}"},
                    idempotent=True,
                )
            except VKError as exc:
                # Ключ сообщества не читает стену (27/15). Такой ключ и фото не загружает,
                # значит, у наших записей вложений нет — правим один текст.
                if exc.code not in (15, 27):
                    raise
                current = None
            if current is not None:
                items = current.get("items") if isinstance(current, dict) else current
                if not items:
                    raise VKError(100, "запись уже удалена из сообщества")
                attachments = _attachments_of(items[0])
            # wall.edit без вложений снимает их — когда они есть, передаём прежние заново
            await client.call(
                "wall.edit",
                {
                    "owner_id": -group_id,
                    "post_id": int(post.external_id),
                    "message": text[: PLATFORM_HARD_LIMITS[Platform.VK]],
                    "attachments": ",".join(attachments) if attachments else None,
                },
            )
        except VKError as exc:
            raise PublishError(f"ВКонтакте: {exc}") from exc

    # ------------------------------------------------------------------ диагностика

    async def check_channel(self, channel: Channel) -> ChannelCheck:
        try:
            group_id = group_id_of(channel)
            client = self.clients.get(group_id)
            info = _first_group(
                await client.call(
                    "groups.getById",
                    {"group_id": group_id, "fields": "members_count,wall"},
                    idempotent=True,
                )
            )
        except (VKError, PublishError) as exc:
            return ChannelCheck(False, f"сообщество недоступно: {exc}")
        community = _community(info)
        wall = info.get("wall")
        if wall in (0, 3):
            state = "выключена" if wall == 0 else "закрыта"
            return ChannelCheck(False, f"«{community.name}»: стена {state} — публиковать нельзя")
        kind = _KINDS.get(community.kind, community.kind)
        message = f"«{community.name}» — {kind}"
        if community.members is not None:
            message += f", подписчиков: {community.members}"
        # Какой это ключ: права ключа сообщества отдаёт только groups.getTokenPermissions
        try:
            await client.call("groups.getTokenPermissions", idempotent=True)
        except VKError:
            return ChannelCheck(True, message + ". Токен администратора: доступны все функции")
        return ChannelCheck(
            True,
            message + ". Ключ сообщества: публикация текста, картинки карточкой со ссылкой, "
            "подписчики; правка записей и просмотры недоступны (ограничение ВКонтакте)",
        )


def _attachments_of(post: dict[str, Any]) -> list[str]:
    """Вложения записи в виде `photo-1_2`, `doc…` — как их принимает wall.edit."""
    result = []
    for item in post.get("attachments") or []:
        kind = item.get("type")
        data = item.get(kind) if kind else None
        if not isinstance(data, dict) or "owner_id" not in data or "id" not in data:
            continue
        key = f"_{data['access_key']}" if data.get("access_key") else ""
        result.append(f"{kind}{data['owner_id']}_{data['id']}{key}")
    return result
