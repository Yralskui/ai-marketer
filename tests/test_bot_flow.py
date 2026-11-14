"""Сквозной сценарий через настоящий Dispatcher без сети (фейковая сессия Telegram).

/start -> онбординг -> «напиши пост» -> кнопки: опубликовать / запланировать /
изменить / отклонить -> /posts, /status. Проверяем ответы бота и статусы в БД.

Роутеры — модульные синглтоны, поэтому Dispatcher собирается один раз на весь
процесс: весь сценарий — один тест.
"""

from __future__ import annotations

import itertools
import re
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    Chat,
    File,
    Message,
    MessageOriginChannel,
    PhotoSize,
    Update,
    User,
)

from app.bot.keyboards import PostCb
from app.bot.setup import create_dispatcher
from app.container import build_container
from app.db.models import PostStatus
from app.db.repositories import PostRepo

OWNER_ID = 111
STRANGER_ID = 666
CLIENT_ID = 222
COMMENTER_ID = 333
PHOTO_BYTES = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"fake-jpeg"


class FakeSession(BaseSession):
    """Отвечает на методы Bot API правдоподобными объектами и запоминает запросы."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[Any] = []
        self._ids = itertools.count(1000)

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:  # noqa: ASYNC109 — сигнатура aiogram
        self.requests.append(method)
        name = type(method).__name__
        if name in ("SendMessage", "SendPhoto"):
            chat_id = str(method.chat_id)
            if chat_id.startswith("@"):
                chat = Chat(id=-1001234567890, type="channel", username=chat_id[1:])
            else:
                chat = Chat(id=int(chat_id), type="private")
            return Message(
                message_id=next(self._ids),
                date=datetime.now(),
                chat=chat,
                text=getattr(method, "text", None),
            )
        if name == "SendMediaGroup":
            return [
                Message(message_id=next(self._ids), date=datetime.now(),
                        chat=Chat(id=int(method.chat_id), type="private"))
                for _ in method.media
            ]  # fmt: skip
        if name == "GetFile":
            return File(file_id=method.file_id, file_unique_id="u-" + method.file_id,
                        file_path=f"photos/{method.file_id}.jpg")  # fmt: skip
        if name == "GetMe":
            return User(id=999, is_bot=True, first_name="Bot", username="test_bot")
        if name in ("GetChat", "GetChatMember"):
            raise TelegramBadRequest(method=method, message="chat not found")
        return True

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any):
        yield PHOTO_BYTES  # «скачанное» фото

    # --- помощники для проверок

    def sent_texts(self, chat_id: int | str) -> list[str]:
        """Тексты сообщений и подписи к фото, отправленные в чат."""
        return [
            r.text if type(r).__name__ == "SendMessage" else (r.caption or "")
            for r in self.requests
            if type(r).__name__ in ("SendMessage", "SendPhoto") and str(r.chat_id) == str(chat_id)
        ]

    def clear(self) -> None:
        self.requests.clear()


def _user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name="Урал" if uid == OWNER_ID else "Чужой")


_update_ids = itertools.count(1)


def msg(uid: int, text: str) -> Update:
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=next(_update_ids),
            date=datetime.now(),
            chat=Chat(id=uid, type="private"),
            from_user=_user(uid),
            text=text,
        ),
    )


def press(uid: int, action: str, post_id: int) -> Update:
    return press_data(uid, PostCb(action=action, post_id=post_id).pack())


def press_data(uid: int, data: str) -> Update:
    return Update(
        update_id=next(_update_ids),
        callback_query=CallbackQuery(
            id=str(next(_update_ids)),
            from_user=_user(uid),
            chat_instance="test",
            data=data,
            message=Message(
                message_id=next(_update_ids),
                date=datetime.now(),
                chat=Chat(id=uid, type="private"),
                text="черновик",
            ),
        ),
    )


GROUP_ID = -100555
CHANNEL_CHAT = Chat(id=-1001234567890, type="channel", username="test_channel")


def group_comment(uid: int, text: str, channel_message_id: int) -> Update:
    """Комментарий в группе обсуждения — ответ на автоматически пересланный пост канала."""
    root = Message(
        message_id=next(_update_ids),
        date=datetime.now(),
        chat=Chat(id=GROUP_ID, type="supergroup"),
        sender_chat=CHANNEL_CHAT,
        is_automatic_forward=True,
        forward_origin=MessageOriginChannel(
            date=datetime.now(), chat=CHANNEL_CHAT, message_id=channel_message_id
        ),
        text="пост канала",
    )
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=next(_update_ids),
            date=datetime.now(),
            chat=Chat(id=GROUP_ID, type="supergroup"),
            from_user=User(id=COMMENTER_ID, is_bot=False, first_name="Анна"),
            text=text,
            reply_to_message=root,
        ),
    )


def photo_msg(uid: int, file_id: str, caption: str | None = None) -> Update:
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=next(_update_ids),
            date=datetime.now(),
            chat=Chat(id=uid, type="private"),
            from_user=_user(uid),
            photo=[
                PhotoSize(file_id=file_id, file_unique_id="u-" + file_id, width=800, height=600)
            ],
            caption=caption,
        ),
    )


def last_draft_id(session: FakeSession) -> int:
    for text in reversed(session.sent_texts(OWNER_ID)):
        m = re.search(r"Черновик #(\d+)", text or "")
        if m:
            return int(m.group(1))
    raise AssertionError("черновик не отправлен")


async def test_end_to_end_bot_flow(settings):
    from app.bot.setup import BOT_COMMANDS
    from app.db.knowledge import MiniUserState
    from app.db.repositories import BusinessRepo, UserRepo

    fake = FakeSession()
    settings.bot_token = "123456:" + "A" * 35
    settings.miniapp_public_url = "https://miniapp.example.com"
    bot = Bot(settings.bot_token, session=fake)
    container = build_container(settings, bot)
    await container.db.create_all()
    dp = create_dispatcher(container)

    assert [item.command for item in BOT_COMMANDS] == ["start"]
    await dp.feed_update(bot, msg(STRANGER_ID, "/start"))
    assert str(STRANGER_ID) in fake.sent_texts(STRANGER_ID)[-1]
    await dp.feed_update(bot, msg(OWNER_ID, "/start"))
    assert fake.requests[-1].reply_markup.inline_keyboard[0][0].web_app.url.endswith("/miniapp")
    async with container.db.sessionmaker() as session:
        user = await UserRepo(session).get_by_telegram_id(OWNER_ID)
        company = await BusinessRepo(session).create(user.id, "Студия стекла")
        await BusinessRepo(session).create(user.id, "Кофейня")
        session.add(MiniUserState(user_id=user.id, default_created=True))
        await session.commit()
        business_id = company.id

    await dp.feed_update(bot, msg(OWNER_ID, "Напиши пост про витрины"))
    assert "Для какой компании" in fake.sent_texts(OWNER_ID)[-1]
    await dp.feed_update(bot, msg(OWNER_ID, "Студия стекла"))
    assert "ждёт согласования" in fake.sent_texts(OWNER_ID)[-1]
    async with container.db.sessionmaker() as session:
        (post,) = await PostRepo(session).list_for_business(business_id)
        assert post.status == PostStatus.PENDING_APPROVAL
    await dp.feed_update(bot, press(OWNER_ID, "publish", post.id))
    assert fake.sent_texts("@test_channel") == []
    await dp.feed_update(bot, msg(OWNER_ID, "/posts"))
    assert "Mini App" in fake.sent_texts(OWNER_ID)[-1]
    await container.db.dispose()
