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
    MessageReactionCountUpdated,
    PhotoSize,
    ReactionCount,
    ReactionTypeEmoji,
    Update,
    User,
)

from app.bot import keyboards as kb
from app.bot.keyboards import CommentCb, PostCb
from app.bot.setup import create_dispatcher
from app.container import build_container
from app.db.models import PostStatus
from app.db.repositories import PostRepo
from app.scheduler.setup import create_scheduler

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


async def test_end_to_end_bot_flow(settings, monkeypatch, tmp_path):
    import app.services.media

    monkeypatch.setattr(app.services.media, "UPLOADS_DIR", tmp_path / "uploads")
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

    # /start: приветственная картинка + меню кнопками
    fake.clear()
    await feed(bot, msg(OWNER_ID, "/start"))
    assert "Привет, Урал" in fake.sent_texts(OWNER_ID)[-1]
    welcome = next(r for r in fake.requests if type(r).__name__ == "SendPhoto")
    menu = [b.text for row in welcome.reply_markup.keyboard for b in row]
    assert kb.MENU_NEW_POST in menu and kb.MENU_COMPANIES in menu  # у админа — и компании

    # Кнопка меню прерывает онбординг и показывает посты
    await feed(bot, msg(OWNER_ID, kb.MENU_MATERIALS))
    assert "Расскажите о вашем бизнесе" in fake.sent_texts(OWNER_ID)[-1]
    await feed(bot, msg(OWNER_ID, kb.MENU_POSTS))
    assert "Постов пока нет" in fake.sent_texts(OWNER_ID)[-1]

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
    assert buttons == [
        "✅ Опубликовать сейчас",
        "🕒 Запланировать",
        "✏️ Изменить",
        "🖼 Своё фото",
        "❌ Отклонить",
    ]
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

    # 💬 Комментарий подписчика под постом -> карточка владельцу -> «Отправить» -> ответ
    async with container.db.sessionmaker() as s:
        channel_message_id = int((await PostRepo(s).get(post1)).external_id)
    fake.clear()
    await feed(
        bot,
        group_comment(
            COMMENTER_ID, "Выглядит вкусно! А тыквенный латте будет?", channel_message_id
        ),
    )
    card = fake.sent_texts(OWNER_ID)[-1]
    assert "Новый комментарий" in card and f"к посту #{post1}" in card and "Анна" in card
    assert fake.sent_texts(GROUP_ID) == []  # без разрешения в группу ничего не ушло
    card_msg = next(r for r in fake.requests if getattr(r, "reply_markup", None) is not None)
    send_data = card_msg.reply_markup.inline_keyboard[0][0].callback_data
    assert CommentCb.unpack(send_data).action == "send"
    await feed(bot, press_data(OWNER_ID, send_data))
    replies = [
        r
        for r in fake.requests
        if type(r).__name__ == "SendMessage" and str(r.chat_id) == str(GROUP_ID)
    ]
    assert len(replies) == 1 and "Анна" in replies[0].text
    await feed(bot, press_data(OWNER_ID, send_data))  # повторное нажатие — без второго ответа
    replies = [
        r
        for r in fake.requests
        if type(r).__name__ == "SendMessage" and str(r.chat_id) == str(GROUP_ID)
    ]
    assert len(replies) == 1
    await feed(bot, msg(OWNER_ID, "/comments"))
    overview = fake.sent_texts(OWNER_ID)[-1]
    assert "подключена ✅" in overview and "✅ <b>Анна" in overview

    # ❤️ Telegram прислал счётчик реакций на пост -> 📈 отчёт с цифрами и графиком
    reaction = Update(
        update_id=next(_update_ids),
        message_reaction_count=MessageReactionCountUpdated(
            chat=CHANNEL_CHAT,
            message_id=channel_message_id,
            date=datetime.now(),
            reactions=[ReactionCount(type=ReactionTypeEmoji(emoji="👍"), total_count=7)],
        ),
    )
    await feed(bot, reaction)
    fake.clear()
    await feed(bot, msg(OWNER_ID, kb.MENU_REPORT))
    photo = next(r for r in fake.requests if type(r).__name__ == "SendPhoto")
    report_text = " ".join(fake.sent_texts(OWNER_ID))
    assert photo.chat_id == OWNER_ID
    assert "Отчёт за 7 дн." in report_text and "Реакции: <b>7</b>" in report_text
    assert "Комментарии: <b>1</b>, отвечено 1" in report_text

    # 🕒 Запланировать
    await feed(bot, msg(OWNER_ID, "Подготовь пост про скидки на выходных"))
    post2 = last_draft_id(fake)
    await feed(bot, press(OWNER_ID, "schedule", post2))
    await feed(bot, msg(OWNER_ID, "абракадабра"))
    assert "Не понял время" in fake.sent_texts(OWNER_ID)[-1]
    await feed(bot, msg(OWNER_ID, "завтра в 10 утра"))
    assert "запланирован" in fake.sent_texts(OWNER_ID)[-1]
    assert await status_of(post2) == PostStatus.SCHEDULED

    # ✏️ Изменить -> новая версия черновика (пост создаём через кнопку меню)
    await feed(bot, msg(OWNER_ID, kb.MENU_NEW_POST))
    assert "О чём пост" in fake.sent_texts(OWNER_ID)[-1]
    await feed(bot, msg(OWNER_ID, "новый десерт"))
    post3 = last_draft_id(fake)
    fake.clear()
    await feed(bot, press(OWNER_ID, "edit", post3))
    await feed(bot, msg(OWNER_ID, "добавь эмодзи"))
    assert last_draft_id(fake) == post3
    assert await status_of(post3) == PostStatus.PENDING_APPROVAL

    # 🖼 Своё фото вместо картинки: черновик снова уходит на согласование
    fake.clear()
    await feed(bot, press(OWNER_ID, "photo", post3))
    assert "Пришлите фото для поста" in fake.sent_texts(OWNER_ID)[-1]
    await feed(bot, photo_msg(OWNER_ID, "own-1"))
    assert f"Фото в посте #{post3} заменено" in " ".join(fake.sent_texts(OWNER_ID))
    assert last_draft_id(fake) == post3
    async with container.db.sessionmaker() as s:
        assert (await PostRepo(s).get(post3)).image_path.endswith("u-own-1.jpg")
    assert await status_of(post3) == PostStatus.PENDING_APPROVAL

    # ❌ Отклонить
    await feed(bot, press(OWNER_ID, "reject", post3))
    assert await status_of(post3) == PostStatus.REJECTED

    # Фото с подписью -> сразу пост с этим фото; без подписи -> кнопки
    fake.clear()
    await feed(bot, photo_msg(OWNER_ID, "own-2", "Новый зал на втором этаже"))
    post4 = last_draft_id(fake)
    async with container.db.sessionmaker() as s:
        assert (await PostRepo(s).get(post4)).image_path.endswith("u-own-2.jpg")
    await feed(bot, photo_msg(OWNER_ID, "own-3"))
    assert "Сделать с ним пост" in fake.sent_texts(OWNER_ID)[-1]
    await feed(bot, msg(OWNER_ID, kb.MENU_PHOTOS))
    assert "Фотобанк" in fake.sent_texts(OWNER_ID)[-1]
    assert any(type(r).__name__ == "SendMediaGroup" for r in fake.requests)

    # /posts, /status, неизвестная команда
    await feed(bot, msg(OWNER_ID, "/posts"))
    posts_text = fake.sent_texts(OWNER_ID)[-1]
    assert f"#{post1}" in posts_text and f"#{post2}" in posts_text
    await feed(bot, msg(OWNER_ID, "/status"))
    status_text = fake.sent_texts(OWNER_ID)[-1]
    assert "Диагностика" in status_text and "mock" in status_text
    await feed(bot, msg(OWNER_ID, "/unknown"))
    assert "Не знаю такой команды" in fake.sent_texts(OWNER_ID)[-1]

    # ---------------- вторая компания без кода (критерии 9 и 11)
    await feed(bot, msg(OWNER_ID, "/new_company Цветы «Роза»"))
    created = fake.sent_texts(OWNER_ID)[-1]
    assert "Роза" in created and "создана" in created
    invite = re.search(r"https://t\.me/test_bot\?start=([A-Za-z0-9_-]+)", created)
    assert invite, created
    company_b = int(re.search(r"\(#(\d+)\)", created).group(1))

    # Клиент подключается по ссылке и видит только свою компанию
    fake.clear()
    await feed(bot, msg(CLIENT_ID, f"/start {invite.group(1)}"))
    assert "подключены к компании <b>Цветы «Роза»</b>" in fake.sent_texts(CLIENT_ID)[-1]
    welcome = next(r for r in fake.requests if type(r).__name__ == "SendPhoto")
    client_menu = [b.text for row in welcome.reply_markup.keyboard for b in row]
    assert kb.MENU_NEW_POST in client_menu and kb.MENU_COMPANIES not in client_menu
    await feed(bot, msg(CLIENT_ID, "/posts"))
    assert "Постов пока нет" in fake.sent_texts(CLIENT_ID)[-1]
    await feed(bot, msg(CLIENT_ID, f"/post {post1}"))
    assert "не найден" in fake.sent_texts(CLIENT_ID)[-1]

    # Кнопка под чужим постом не работает
    fake.clear()
    await feed(bot, press(CLIENT_ID, "reject", post2))
    answers = [r for r in fake.requests if type(r).__name__ == "AnswerCallbackQuery"]
    assert answers and "не найден" in (answers[-1].text or "")
    assert await status_of(post2) == PostStatus.SCHEDULED

    # Управление компаниями — только администратору
    await feed(bot, msg(CLIENT_ID, "/companies"))
    assert "только администратору" in fake.sent_texts(CLIENT_ID)[-1]

    # Клиент подключает свой канал
    await feed(bot, msg(CLIENT_ID, "/channel t.me/roza_flowers"))
    assert "<code>@roza_flowers</code> подключён" in fake.sent_texts(CLIENT_ID)[-1]

    # Администратор видит обе компании и переключается между ними
    await feed(bot, msg(OWNER_ID, "/companies"))
    listing = fake.sent_texts(OWNER_ID)[-1]
    assert "Роза" in listing and "@roza_flowers" in listing and "@test_channel" in listing
    await feed(bot, msg(OWNER_ID, "/posts"))
    assert "Постов пока нет" in fake.sent_texts(OWNER_ID)[-1]  # сейчас выбрана компания B
    async with container.db.sessionmaker() as s:
        company_a = (await PostRepo(s).get(post1)).business_id
    assert company_a != company_b
    await feed(bot, msg(OWNER_ID, f"/use {company_a}"))
    await feed(bot, msg(OWNER_ID, "/posts"))
    assert f"#{post1}" in fake.sent_texts(OWNER_ID)[-1]

    # Расходы на AI: админ видит все компании и ставит лимит, клиент — нет
    await feed(bot, msg(OWNER_ID, "/costs"))
    costs_text = fake.sent_texts(OWNER_ID)[-1]
    assert "Расходы на AI" in costs_text and "Роза" in costs_text
    await feed(bot, msg(OWNER_ID, "/budget 500"))
    assert "500 ₽ в месяц" in fake.sent_texts(OWNER_ID)[-1]
    await feed(bot, msg(CLIENT_ID, "/budget 1"))
    assert "только администратору" in fake.sent_texts(CLIENT_ID)[-1]
    await feed(bot, msg(CLIENT_ID, "/costs"))
    assert "Роза" in fake.sent_texts(CLIENT_ID)[-1]
    assert "Зерно" not in fake.sent_texts(CLIENT_ID)[-1]

    # Журнал действий и отзыв доступа клиента
    await feed(bot, msg(OWNER_ID, f"/use {company_b}"))
    await feed(bot, msg(OWNER_ID, "/log"))
    assert "Журнал действий" in fake.sent_texts(OWNER_ID)[-1]
    assert "client_joined" in fake.sent_texts(OWNER_ID)[-1]
    fake.clear()
    await feed(bot, msg(OWNER_ID, "/clients"))
    clients_msg = next(r for r in fake.requests if getattr(r, "reply_markup", None) is not None)
    revoke = clients_msg.reply_markup.inline_keyboard[0][0].callback_data
    await feed(bot, press_data(OWNER_ID, revoke))
    assert "отключён" in fake.sent_texts(OWNER_ID)[-1]
    await feed(bot, msg(CLIENT_ID, "/posts"))
    assert "нет доступа" in fake.sent_texts(CLIENT_ID)[-1]

    # Без приглашения посторонний по-прежнему не проходит
    await feed(bot, msg(STRANGER_ID, "/start wrong-invite-code"))
    assert "нет доступа" in fake.sent_texts(STRANGER_ID)[-1]

    await container.db.dispose()
