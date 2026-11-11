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
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot.keyboards import PostCb
from app.bot.setup import create_dispatcher
from app.container import build_container
from app.db.models import PostStatus
from app.db.repositories import PostRepo
from app.scheduler.setup import create_scheduler

OWNER_ID = 111
STRANGER_ID = 666


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
        if name == "GetMe":
            return User(id=999, is_bot=True, first_name="Bot", username="test_bot")
        if name in ("GetChat", "GetChatMember"):
            raise TelegramBadRequest(method=method, message="chat not found")
        return True

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        yield b""

    # --- помощники для проверок

    def sent_texts(self, chat_id: int | str) -> list[str]:
        return [
            r.text
            for r in self.requests
            if type(r).__name__ == "SendMessage" and str(r.chat_id) == str(chat_id)
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
    return Update(
        update_id=next(_update_ids),
        callback_query=CallbackQuery(
            id=str(next(_update_ids)),
            from_user=_user(uid),
            chat_instance="test",
            data=PostCb(action=action, post_id=post_id).pack(),
            message=Message(
                message_id=next(_update_ids),
                date=datetime.now(),
                chat=Chat(id=uid, type="private"),
                text="черновик",
            ),
        ),
    )


def last_draft_id(session: FakeSession) -> int:
    for text in reversed(session.sent_texts(OWNER_ID)):
        m = re.search(r"Черновик #(\d+)", text or "")
        if m:
            return int(m.group(1))
    raise AssertionError("черновик не отправлен")


async def test_end_to_end_bot_flow(settings):
    fake = FakeSession()
    settings.bot_token = "123456:" + "A" * 35
    bot = Bot(settings.bot_token, session=fake)
    container = build_container(settings, bot)
    await container.db.create_all()
    dp = create_dispatcher(container)
    container.scheduler = create_scheduler(container)
    feed = dp.feed_update

    async def status_of(post_id: int) -> PostStatus:
        async with container.db.sessionmaker() as s:
            return (await PostRepo(s).get(post_id)).status

    # Чужой пользователь получает отказ и свой ID
    await feed(bot, msg(STRANGER_ID, "/start"))
    assert str(STRANGER_ID) in fake.sent_texts(STRANGER_ID)[-1]

    # /start
    await feed(bot, msg(OWNER_ID, "/start"))
    assert "Привет, Урал" in fake.sent_texts(OWNER_ID)[-1]

    # Онбординг текстом
    await feed(bot, msg(OWNER_ID, "/onboarding"))
    await feed(
        bot,
        msg(OWNER_ID, "Мы кофейня «Зерно». Предлагаем: эспрессо, капучино. Клиенты — студенты."),
    )
    await feed(bot, msg(OWNER_ID, "/done"))
    assert "Зерно" in fake.sent_texts(OWNER_ID)[-1]
    await feed(bot, msg(OWNER_ID, "/profile"))
    assert "капучино" in fake.sent_texts(OWNER_ID)[-1]

    # Свободный текст -> агент -> черновик с кнопками
    fake.clear()
    await feed(bot, msg(OWNER_ID, "Напиши пост про наше новое осеннее меню"))
    post1 = last_draft_id(fake)
    draft_msg = next(r for r in fake.requests if getattr(r, "reply_markup", None) is not None)
    buttons = [b.text for row in draft_msg.reply_markup.inline_keyboard for b in row]
    assert buttons == ["✅ Опубликовать сейчас", "🕒 Запланировать", "✏️ Изменить", "❌ Отклонить"]
    assert await status_of(post1) == PostStatus.PENDING_APPROVAL

    # ✅ Опубликовать -> сообщение в канал + ссылка
    fake.clear()
    await feed(bot, press(OWNER_ID, "publish", post1))
    channel_posts = fake.sent_texts("@test_channel")
    assert len(channel_posts) == 1 and "осеннее меню" in channel_posts[0]
    assert "опубликован" in fake.sent_texts(OWNER_ID)[-1]
    assert "https://t.me/test_channel/" in fake.sent_texts(OWNER_ID)[-1]
    assert await status_of(post1) == PostStatus.PUBLISHED

    # Повторное нажатие не публикует второй раз
    fake.clear()
    await feed(bot, press(OWNER_ID, "publish", post1))
    assert fake.sent_texts("@test_channel") == []

    # 🕒 Запланировать
    await feed(bot, msg(OWNER_ID, "Подготовь пост про скидки на выходных"))
    post2 = last_draft_id(fake)
    await feed(bot, press(OWNER_ID, "schedule", post2))
    await feed(bot, msg(OWNER_ID, "абракадабра"))
    assert "Не понял время" in fake.sent_texts(OWNER_ID)[-1]
    await feed(bot, msg(OWNER_ID, "завтра в 10 утра"))
    assert "запланирован" in fake.sent_texts(OWNER_ID)[-1]
    assert await status_of(post2) == PostStatus.SCHEDULED

    # ✏️ Изменить -> новая версия черновика
    await feed(bot, msg(OWNER_ID, "Напиши пост про новый десерт"))
    post3 = last_draft_id(fake)
    fake.clear()
    await feed(bot, press(OWNER_ID, "edit", post3))
    await feed(bot, msg(OWNER_ID, "добавь эмодзи"))
    assert last_draft_id(fake) == post3
    assert await status_of(post3) == PostStatus.PENDING_APPROVAL

    # ❌ Отклонить
    await feed(bot, press(OWNER_ID, "reject", post3))
    assert await status_of(post3) == PostStatus.REJECTED

    # /posts, /status, неизвестная команда
    await feed(bot, msg(OWNER_ID, "/posts"))
    posts_text = fake.sent_texts(OWNER_ID)[-1]
    assert f"#{post1}" in posts_text and f"#{post2}" in posts_text
    await feed(bot, msg(OWNER_ID, "/status"))
    status_text = fake.sent_texts(OWNER_ID)[-1]
    assert "Диагностика" in status_text and "mock" in status_text
    await feed(bot, msg(OWNER_ID, "/unknown"))
    assert "Не знаю такой команды" in fake.sent_texts(OWNER_ID)[-1]

    await container.db.dispose()
