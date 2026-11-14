"""Лайки и комментарии ВКонтакте для статистики.

Два источника, потому что ВК по-разному ограничивает ключи:

* токен администратора — почасовая сверка: `wall.getById` отдаёт число лайков и комментариев,
  `wall.getComments` — сами комментарии, включая старые. Это полная картина;
* ключ сообщества стену не читает (ошибка 27), но ему доступен Long Poll сообщества: события
  о новых комментариях и лайках приходят в реальном времени, только с момента включения.

Комментарии пишутся в ту же таблицу `comments`, что и у Telegram, со статусом SKIPPED («только
статистика»: бот под ними не отвечает и не присылает черновиков). Лайки — в `PostMetric.reactions`
под ключом «❤», поэтому отчёты и Mini App показывают их так же, как реакции Telegram.
Схема БД не меняется.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Channel, Comment, CommentStatus, Platform, Post, PostStatus
from app.db.repositories.post_metric import PostMetricRepo
from app.publishers.vk_api import VKClient, VKClients, VKError

log = logging.getLogger(__name__)

LIKE = "❤"
PAGE = 100
LONGPOLL_EVENTS = (
    "wall_reply_new",
    "wall_reply_edit",
    "wall_reply_delete",
    "wall_reply_restore",
    "like_add",
    "like_remove",
)


# ---------------------------------------------------------------- общие операции с базой


async def _find_post(session: AsyncSession, group_id: int, vk_post_id: int) -> Post | None:
    return (
        await session.execute(
            select(Post)
            .join(Channel, Channel.id == Post.channel_id)
            .where(
                Channel.platform == Platform.VK,
                Channel.external_id == str(group_id),
                Post.platform == Platform.VK,
                Post.external_id == str(vk_post_id),
                Post.status == PostStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()


def _author(profiles: dict[int, str], from_id: int) -> str:
    if from_id < 0:
        return profiles.get(from_id) or "Сообщество"
    return profiles.get(from_id) or f"id{from_id}"


async def _save_comment(
    session: AsyncSession,
    post: Post,
    group_id: int,
    vk_comment_id: int,
    from_id: int,
    author_name: str,
    text: str,
) -> bool:
    """Сохраняет комментарий. True — новый, False — уже был (текст при этом обновляется)."""
    chat_id = -group_id
    found = (
        await session.execute(
            select(Comment).where(
                Comment.business_id == post.business_id,
                Comment.chat_id == chat_id,
                Comment.message_id == vk_comment_id,
            )
        )
    ).scalar_one_or_none()
    if found is not None:
        found.text = text
        return False
    session.add(
        Comment(
            business_id=post.business_id,
            post_id=post.id,
            chat_id=chat_id,
            message_id=vk_comment_id,
            author_id=from_id,
            author_name=author_name[:255],
            text=text,
            status=CommentStatus.SKIPPED,
        )
    )
    return True


async def _delete_comment(session: AsyncSession, group_id: int, vk_comment_id: int) -> None:
    for comment in (
        await session.execute(
            select(Comment).where(Comment.chat_id == -group_id, Comment.message_id == vk_comment_id)
        )
    ).scalars():
        await session.delete(comment)


async def _change_likes(session: AsyncSession, post: Post, delta: int) -> None:
    repo = PostMetricRepo(session)
    metric = await repo.get_for_post(post.id)
    current = int((metric.reactions or {}).get(LIKE, 0)) if metric else 0
    await repo.set_reactions(post.id, post.business_id, {LIKE: max(0, current + delta)})


# ---------------------------------------------------------------- токен администратора


def _profiles(response: dict[str, Any]) -> dict[int, str]:
    names: dict[int, str] = {}
    for user in response.get("profiles") or []:
        names[int(user["id"])] = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    for group in response.get("groups") or []:
        names[-int(group["id"])] = str(group.get("name") or "Сообщество")
    return names


async def _comments_of(client: VKClient, group_id: int, vk_post_id: int) -> list[dict[str, Any]]:
    """Все комментарии записи вместе с ответами в ветках (постранично)."""
    result: list[dict[str, Any]] = []
    profiles: dict[int, str] = {}
    offset = 0
    while True:
        response = await client.call(
            "wall.getComments",
            {
                "owner_id": -group_id,
                "post_id": vk_post_id,
                "count": PAGE,
                "offset": offset,
                "thread_items_count": 10,
                "extended": 1,
                "sort": "asc",
            },
            idempotent=True,
        )
        profiles.update(_profiles(response))
        items = response.get("items") or []
        for item in items:
            result.append(item)
            result.extend((item.get("thread") or {}).get("items") or [])
        offset += PAGE
        if len(items) < PAGE or offset >= int(response.get("count") or 0):
            break
    for item in result:
        item["_author"] = _author(profiles, int(item.get("from_id") or 0))
    return result


async def sync_engagement(
    sessions: async_sessionmaker[AsyncSession], clients: VKClients
) -> dict[str, int]:
    """Сверяет лайки и комментарии опубликованных записей. Нужен токен администратора.

    Ключ сообщества получит ошибку 27 — тогда тихо выходим (данные идут через Long Poll).
    """
    stats = {"posts": 0, "comments": 0, "skipped_no_access": 0}
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Post.id, Post.external_id, Channel.external_id)
                .join(Channel, Channel.id == Post.channel_id)
                .where(
                    Post.platform == Platform.VK,
                    Post.status == PostStatus.PUBLISHED,
                    Post.external_id.is_not(None),
                    Channel.platform == Platform.VK,
                )
            )
        ).all()
    by_group: dict[int, list[tuple[int, int]]] = {}
    for post_id, external_id, channel_external in rows:
        if external_id.isascii() and external_id.isdecimal() and channel_external.isdecimal():
            by_group.setdefault(int(channel_external), []).append((post_id, int(external_id)))

    for group_id, posts in by_group.items():
        if not clients.has(group_id):
            continue
        client = clients.get(group_id)
        for start in range(0, len(posts), PAGE):
            chunk = posts[start : start + PAGE]
            try:
                response = await client.call(
                    "wall.getById",
                    {"posts": ",".join(f"-{group_id}_{vk_id}" for _, vk_id in chunk)},
                    idempotent=True,
                )
            except VKError as exc:
                if exc.code in (15, 27):
                    stats["skipped_no_access"] += 1
                    log.info("VK #%s: ключ не читает стену, лайки идут через Long Poll", group_id)
                else:
                    log.warning(
                        "VK #%s: не удалось получить лайки и комментарии: %s", group_id, exc
                    )
                break
            items = response.get("items") if isinstance(response, dict) else response
            found = {int(item["id"]): item for item in items or [] if "id" in item}
            for post_id, vk_id in chunk:
                item = found.get(vk_id)
                if item is None:
                    continue
                likes = (item.get("likes") or {}).get("count")
                comments_count = (item.get("comments") or {}).get("count") or 0
                try:
                    fetched = await _comments_of(client, group_id, vk_id) if comments_count else []
                except VKError as exc:
                    log.warning("VK #%s: комментарии записи %s: %s", group_id, vk_id, exc)
                    fetched = []
                async with sessions.begin() as session:
                    post = await session.get(Post, post_id)
                    if post is None:
                        continue
                    if isinstance(likes, int):
                        await PostMetricRepo(session).set_reactions(
                            post.id, post.business_id, {LIKE: likes}
                        )
                    for comment in fetched:
                        if comment.get("deleted") or not comment.get("id"):
                            continue
                        if await _save_comment(
                            session,
                            post,
                            group_id,
                            int(comment["id"]),
                            int(comment.get("from_id") or 0),
                            comment["_author"],
                            str(comment.get("text") or ""),
                        ):
                            stats["comments"] += 1
                stats["posts"] += 1
    return stats


# ---------------------------------------------------------------- Long Poll сообщества


async def apply_event(
    sessions: async_sessionmaker[AsyncSession], group_id: int, event: dict[str, Any]
) -> None:
    """Применяет одно событие Long Poll к базе. События чужих записей молча игнорируются."""
    kind = event.get("type")
    obj = event.get("object") or {}
    async with sessions.begin() as session:
        if kind in ("wall_reply_new", "wall_reply_edit", "wall_reply_restore"):
            post = await _find_post(session, group_id, int(obj.get("post_id") or 0))
            if post is None or not obj.get("id"):
                return
            from_id = int(obj.get("from_id") or 0)
            # Имя автора Long Poll не присылает — «id123», либо «Сообщество» для своих ответов
            await _save_comment(
                session,
                post,
                group_id,
                int(obj["id"]),
                from_id,
                _author({}, from_id),
                str(obj.get("text") or ""),
            )
        elif kind == "wall_reply_delete":
            if obj.get("id"):
                await _delete_comment(session, group_id, int(obj["id"]))
        elif kind in ("like_add", "like_remove"):
            if (
                obj.get("object_type") != "post"
                or int(obj.get("object_owner_id") or 0) != -group_id
            ):
                return
            post = await _find_post(session, group_id, int(obj.get("object_id") or 0))
            if post is not None:
                await _change_likes(session, post, 1 if kind == "like_add" else -1)


async def enable_longpoll(client: VKClient, group_id: int) -> None:
    """Включает Long Poll сообщества и нужные события (меняет настройки сообщества)."""
    await client.call(
        "groups.setLongPollSettings",
        {
            "group_id": group_id,
            "enabled": 1,
            "api_version": "5.199",
            **{event: 1 for event in LONGPOLL_EVENTS},
        },
    )


async def run_longpoll(
    sessions: async_sessionmaker[AsyncSession],
    client: VKClient,
    group_id: int,
    *,
    http_get: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    stop: asyncio.Event,
    pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Слушает события сообщества, пока не выставлен `stop`.

    `http_get(url, params)` возвращает разобранный JSON — так цикл проверяется в тестах без сети.
    """
    server: dict[str, Any] | None = None
    ts: str | None = None
    while not stop.is_set():
        try:
            if server is None:
                server = await client.call(
                    "groups.getLongPollServer", {"group_id": group_id}, idempotent=True
                )
                ts = str(server["ts"])
            answer = await http_get(
                server["server"],
                {"act": "a_check", "key": server["key"], "ts": ts, "wait": 25},
            )
        except VKError as exc:
            log.warning("VK Long Poll #%s: %s", group_id, exc)
            if exc.code in (15, 27):
                return  # ключу Long Poll недоступен — повторять бессмысленно
            server = None
            await pause(30)
            continue
        except Exception as exc:  # сеть: ждём и пробуем снова
            log.warning("VK Long Poll #%s: сбой соединения: %s", group_id, exc)
            await pause(10)
            continue
        failed = answer.get("failed")
        if failed == 1:
            ts = str(answer.get("ts", ts))
            continue
        if failed in (2, 3):
            server = None  # ключ устарел — берём новый адрес
            continue
        ts = str(answer.get("ts", ts))
        for event in answer.get("updates") or []:
            try:
                await apply_event(sessions, group_id, event)
            except Exception:  # одно битое событие не должно ронять слушатель
                log.exception("VK Long Poll #%s: событие не обработано", group_id)


async def start_longpolls(
    sessions: async_sessionmaker[AsyncSession], clients: VKClients
) -> tuple[asyncio.Event, list[asyncio.Task[None]]]:
    """Запускает слушателей Long Poll для сообществ с ключом сообщества.

    Токену администратора Long Poll не нужен: он читает всё через `sync_engagement`.
    """
    import httpx

    stop = asyncio.Event()
    http = httpx.AsyncClient(timeout=40)

    async def http_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await http.get(url, params=params)
        return response.json()

    async def one(group_id: int) -> None:
        client = clients.get(group_id)
        try:
            await client.call("groups.getTokenPermissions", idempotent=True)
        except VKError:
            return  # токен администратора — Long Poll не нужен
        try:
            await enable_longpoll(client, group_id)
        except VKError as exc:
            log.warning("VK #%s: Long Poll не включился: %s", group_id, exc)
            return
        log.info("VK #%s: слушаем комментарии и лайки через Long Poll", group_id)
        await run_longpoll(sessions, client, group_id, http_get=http_get, stop=stop)

    async def supervise() -> None:
        try:
            await asyncio.gather(*(one(g) for g in clients.tokens))
        finally:
            await http.aclose()

    return stop, [asyncio.create_task(supervise())]
