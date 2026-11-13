"""FSM-состояния многошаговых диалогов."""

from aiogram.fsm.state import State, StatesGroup


class OnboardingStates(StatesGroup):
    collecting = State()  # принимаем текст / файлы / ссылки о бизнесе


class ApprovalStates(StatesGroup):
    waiting_revision = State()  # ждём текст правки; в data: post_id
    waiting_schedule_time = State()  # ждём дату/время; в data: post_id, draft_message_id


class MenuStates(StatesGroup):
    waiting_post_topic = State()  # «✍️ Новый пост» — ждём тему
    waiting_company_name = State()  # «➕ Новая компания» — ждём название


class CommentStates(StatesGroup):
    waiting_reply = State()  # свой текст ответа на комментарий; в data: comment_id


class MediaStates(StatesGroup):
    waiting_topic = State()  # тема поста к фото из фотобанка; в data: media_id
    waiting_replacement = State()  # новое фото для черновика; в data: post_id


class BrandStates(StatesGroup):
    waiting_material = State()  # брендбук файлом или правила текстом
