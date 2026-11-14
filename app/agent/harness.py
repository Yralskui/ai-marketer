"""Маршрутизация мини-приложения. Публикация остаётся действием пользователя."""

import json
import re
from datetime import datetime, timedelta
from secrets import token_hex
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select

from app.agent.context import AgentContext
from app.agent.help_search import search_help
from app.agent.prompts import render_prompt
from app.agent.tools import analytics_tools, plan_tools, post_tools
from app.db.knowledge import PendingDraftOptions, PendingPostAction
from app.db.models import MessageRole, Platform, PostStatus
from app.db.publications import PostAttachments
from app.db.repositories import ChannelRepo, PostRepo
from app.db.repositories.agent_message import AgentMessageRepo
from app.llm.base import LLMError
from app.services import approval, content_gallery, editorial, posts
from app.services.business_profile import profile_to_text
from app.services.image_references import numbered_images, profile_images
from app.services.knowledge_search import retrieve
from app.services.post_images import brand_materials, layout_requirement
from app.utils.time import parse_schedule_time, utcnow


class Intent(BaseModel):
    intent: Literal[
        "help",
        "integration",
        "publish",
        "create_post",
        "revise_post",
        "list_posts",
        "plan",
        "analytics",
        "report",
        "web_search",
        "critique",
        "suggest_topic",
        "company",
    ]


class DraftRequest(post_tools.CreatePostArgs):
    topic: str = Field("", max_length=1000, description="Конкретная тема поста")
    clarification: str = Field(
        "", max_length=400, description="Один вопрос, если без ответа нельзя написать пост"
    )
    suggestion: str = Field(
        "", max_length=400, description="Конкретный ракурс или идея, если уместно"
    )
    when: str = Field("", max_length=200, description="Желаемая дата публикации из запроса")
    avoid_image: bool = Field(False, description="Пользователь явно попросил пост без картинок")

    @field_validator(
        "topic",
        "clarification",
        "suggestion",
        "when",
        "image_direction",
        mode="before",
    )
    @classmethod
    def empty_optional_text(cls, value):
        return "" if value is None else value


EXPLICIT_POST = re.compile(
    r"^(?:пожалуйста[, ]+)?(?:сделай|напиши|подготовь|создай|сгенерируй)\s+"
    r"(?:новый\s+)?(?:пост|публикацию)(?:\s*[—:–-])?\s+(?:(?:на тему|про|об?)\s+)?(.+)",
    re.IGNORECASE | re.DOTALL,
)
REPEATED_TOPIC_QUESTION = re.compile(
    r"(?:уточн\w*\s+тем|(?:на\s+)?какую\s+тем|о\s+ч[её]м\s+(?:пост|написать))",
    re.IGNORECASE,
)


COPY_POST = re.compile(
    r"\b(?:копи[юя]|скопируй\w*|продублируй\w*|дубликат)\b.*?\b(?:пост\w*|публикаци\w*|черновик\w*)\b"
    r"|\b(?:пост\w*|публикаци\w*|черновик\w*)\b.*?\b(?:копи[юя]|скопируй\w*|продублируй\w*|дубликат)\b",
    re.IGNORECASE | re.DOTALL,
)
NO_CHANGES = re.compile(
    r"\b(?:без\s+(?:правок|изменений)|как\s+есть|нич(?:его|[её])(?:\s+не\s+меня(?:й|ть))?)\b[.!]?",
    re.IGNORECASE,
)
COPY_ONLY = re.compile(
    r"^\s*(?:можешь\s+)?(?:пожалуйста[, ]+)?"
    r"(?:(?:сделай|создай|подготовь|предложи)\s+)?"
    r"(?:копи[юя]|скопируй|продублируй|дубликат)\s+"
    r"(?:(?:этого|этот|текущий|текущего|последний|последнего)\s+)?(?:пост\w*|публикаци\w*|черновик\w*)"
    r"(?:\s*[№#]?\s*\d+)?"
    r"(?:\s+(?:опять|снова|ещ[её]\s+раз|предложи(?:ть)?|отправь|отправить|на\s+согласование))*[.!?]*$",
    re.IGNORECASE,
)
POST_NUMBER = re.compile(r"\b(?:пост\w*|черновик\w*|публикаци\w*)\s*[№#]?\s*(\d+)", re.I)


CURRENT_IMAGE_QUESTION = re.compile(
    r"номер\s+(?:самой\s+)?(?:исходн\w+|текущ\w+)\s+(?:картинк|изображен|иллюстрац)",
    re.IGNORECASE,
)


DRAFT_OPTION_LABELS = {
    "neutral": "Обычный",
    "official": "Официальный",
    "social": "Соцсети",
    "anti_llm": "Анти-ЛЛМ",
    "scientific": "Научный",
    "content": "Использовать предложенную",
    "existing": "Выбрать существующую",
    "previous": "Назад по списку",
    "next": "Дальше по списку",
    "back": "К выбору картинки",
    "generate": "Сгенерировать картинку",
    "layout": "Фирменная карточка",
    "none": "Без картинки",
    "cancel": "Отменить",
    "copy_draft": "Создать черновик",
}


DRAFT_PICKER_SIZE = 5
DRAFT_PICKER_STAGES = {"pick_image", "pick_logo"}


async def image_choices(ctx: AgentContext, *, originals_only=False) -> dict[int, str]:
    documents, gallery = await numbered_images(
        ctx.session, ctx.business_id, ctx.storage, originals_only=originals_only
    )
    return {
        **{number: f"{doc.filename} · Файлы профиля" for number, doc in sorted(documents.items())},
        **{number: f"{item.label} · Контент" for number, item in gallery.items()},
    }


def picker_page(data: dict) -> list[dict]:
    offset = data["image_page"] * DRAFT_PICKER_SIZE
    return data["image_choices"][offset : offset + DRAFT_PICKER_SIZE]


async def draft_question(ctx: AgentContext, pending: PendingDraftOptions) -> dict:
    if pending.stage == "style":
        args = DraftRequest.model_validate(pending.data["args"])
        prompt = "В каком стиле написать пост «" + args.topic[:100] + "»?"
        options = [style.value for style in posts.PostStyle]
    elif pending.stage == "copy_draft":
        args = post_tools.RevisePostArgs.model_validate(pending.data["args"])
        prompt = (
            f"Создать новый черновик по посту №{args.post_id} с этой правкой?"
            if args.has_changes
            else f"Создать копию поста №{args.post_id} без изменений?"
        )
        options = ["copy_draft"]
    elif pending.stage in DRAFT_PICKER_STAGES:
        page = picker_page(pending.data)
        options = [str(index) for index in range(1, len(page) + 1)]
        if page:
            prompt = (
                "Выберите оригинал логотипа."
                if pending.stage == "pick_logo"
                else "Какую картинку прикрепить?"
            )
            prompt += (
                " Номер № на картинке — в левом верхнем углу в редакторе профиля, "
                "в «Файлах профиля» или «Контенте». Выберите кнопку по списку ниже."
            )
            prompt += "\n\n" + "\n".join(
                f"{index} — №{entry['number']} · {entry['label']}"
                for index, entry in enumerate(page, start=1)
            )
        else:
            prompt = (
                "Добавьте оригинал логотипа"
                if pending.stage == "pick_logo"
                else "Готовых картинок в этом профиле пока нет. Добавьте изображение"
            ) + " в «Файлы профиля» или «Контент» и попробуйте снова."
        if pending.data["image_page"]:
            options.append("previous")
        if (pending.data["image_page"] + 1) * DRAFT_PICKER_SIZE < len(
            pending.data["image_choices"]
        ):
            options.append("next")
        if not pending.data.get("revision"):
            options.append("back")
    else:
        args = DraftRequest.model_validate(pending.data["args"])
        prompt = "Добавить картинку к посту?"
        options = ["generate", "existing", "none"]
        has_style = bool(await brand_materials(ctx.session, ctx.business))
        can_layout = (
            has_style
            and args.use_logo is not False
            and bool(await image_choices(ctx, originals_only=True))
        )
        if can_layout:
            options.insert(0 if args.branded_layout else 1, "layout")
        content = (
            await content_gallery.get_content(ctx.session, ctx.business_id, args.content_id)
            if args.content_id
            else None
        )
        if args.existing_image_number:
            label = (await image_choices(ctx)).get(args.existing_image_number)
            if label:
                prompt = f"Использовать картинку №{args.existing_image_number} · {label}?"
                options.insert(0, "content")
        elif content is not None:
            prompt = f"Использовать картинку «{content.label}» из раздела «Контент»?"
            options.insert(0, "content")
        elif args.branded_layout:
            if can_layout:
                prompt = "Собрать фирменную карточку с фотографией и текстом?"
            elif not has_style:
                prompt = "Для фирменной карточки нужен визуальный стиль в профиле или обработанный брендбук. Как продолжить?"
            else:
                prompt = "Для фирменной карточки нужен оригинал логотипа. Как продолжить?"
        elif args.content_id:
            prompt = "Эта картинка больше не найдена в разделе «Контент». Как продолжить?"
        elif args.references:
            numbers = ", ".join(f"№{item.number}" for item in args.references)
            prompt = f"Сгенерировать картинку по референсам {numbers}?"
        elif args.use_logo is True:
            prompt = "Сгенерировать картинку с логотипом компании?"
    options.append("cancel")
    return {
        "text": prompt,
        "draft_options": {
            "stage": pending.stage,
            "token": pending.token,
            "options": [{"id": key, "label": DRAFT_OPTION_LABELS.get(key, key)} for key in options],
        },
    }


async def open_image_picker(ctx, pending, choice, *, logo=False):
    images = await image_choices(ctx, originals_only=logo)
    pending.stage = "pick_logo" if logo else "pick_image"
    pending.token = token_hex(6)
    pending.data = {
        **pending.data,
        "image_page": 0,
        "image_choices": [
            {"number": number, "label": " ".join(label.split())[:150]}
            for number, label in images.items()
        ],
        "image_action": choice,
    }
    return await draft_question(ctx, pending)


def start_research(ctx, key, topic, request, style=None):
    editorial.start_post_research(
        key,
        ctx.research_llm,
        ctx.session_factory,
        ctx.settings.tz,
        topic,
        request,
        scientific=style == posts.PostStyle.SCIENTIFIC,
    )


async def ask_draft_options(ctx: AgentContext, text: str, args: DraftRequest) -> dict:
    previous = await ctx.session.get(PendingDraftOptions, (ctx.user_id, ctx.business_id))
    if previous and previous.data.get("research_id"):
        editorial.discard_post_research((ctx.business_id, previous.data["research_id"]))
    research_id = token_hex(8)
    pending = await ctx.session.merge(
        PendingDraftOptions(
            user_id=ctx.user_id,
            business_id=ctx.business_id,
            token=token_hex(6),
            stage="style",
            data={
                "text": text,
                "args": args.model_dump(mode="json"),
                "research_id": research_id,
                "request_id": ctx.request_id,
            },
        )
    )
    start_research(ctx, (ctx.business_id, research_id), args.topic, text)
    return await draft_question(ctx, pending)


async def active_draft_options_for(session, user_id, business_id):
    pending = await session.get(PendingDraftOptions, (user_id, business_id))
    if pending and pending.updated_at >= utcnow() - timedelta(hours=1):
        return pending
    return None


async def active_draft_options(ctx: AgentContext) -> dict | None:
    pending = await active_draft_options_for(ctx.session, ctx.user_id, ctx.business_id)
    if pending:
        return await draft_question(ctx, pending)
    return None


async def choose_draft_option(ctx: AgentContext, token: str, choice: str) -> dict:
    result = await apply_draft_option(ctx, token, choice)
    if question := result.get("draft_options"):
        await record_question(ctx, result["text"], question["token"])
    elif choice == "cancel":
        await AgentMessageRepo(ctx.session).add(
            ctx.user_id, MessageRole.ASSISTANT, result["text"], business_id=ctx.business_id
        )
    await save_chat_proposal(ctx, result)
    return result


async def apply_draft_option(
    ctx: AgentContext, token: str, choice: str, *, image_number: int | None = None
) -> dict:
    pending = await ctx.session.get(
        PendingDraftOptions, (ctx.user_id, ctx.business_id), with_for_update=True
    )
    if not pending or pending.token != token or pending.updated_at < utcnow() - timedelta(hours=1):
        raise ValueError(
            "Этот выбор устарел. Откройте актуальный вопрос или отправьте задачу заново."
        )
    question = await draft_question(ctx, pending)
    if image_number is not None and pending.stage not in DRAFT_PICKER_STAGES:
        raise ValueError("Сейчас номер картинки не требуется.")
    if image_number is None and choice not in {
        item["id"] for item in question["draft_options"]["options"]
    }:
        raise ValueError("Этот вариант недоступен.")
    data = pending.data
    research_key = (ctx.business_id, data.get("research_id") or token_hex(8))
    if choice == "cancel":
        editorial.discard_post_research(research_key)
        await ctx.session.delete(pending)
        return {
            "text": "Правка поста отменена."
            if data.get("revision")
            else "Подготовка поста отменена."
        }
    if data.get("revision"):
        return await continue_revision(ctx, pending, choice, logo_number=image_number)
    args = DraftRequest.model_validate(data["args"])
    if pending.stage == "style":
        args.style = posts.PostStyle(choice)
        start_research(ctx, research_key, args.topic, data["text"], args.style)
        if not args.avoid_image and (args.platform != "vk" or ctx.settings.vk_is_business_account):
            pending.stage = "image"
            pending.token = token_hex(6)
            pending.data = {
                **data,
                "args": args.model_dump(mode="json"),
                "research_id": research_key[1],
            }
            return await draft_question(ctx, pending)
        choice = "none"
    if choice != "content":
        args.existing_image_number = None
    if pending.stage in DRAFT_PICKER_STAGES:
        if choice == "back":
            pending.stage = "image"
            pending.token = token_hex(6)
            return await draft_question(ctx, pending)
        if choice in {"previous", "next"}:
            return await turn_picker_page(ctx, pending, choice)
        selected = image_number or picker_page(data)[int(choice) - 1]["number"]
        logo = pending.stage == "pick_logo"
        if selected not in await image_choices(ctx, originals_only=logo):
            question = await open_image_picker(ctx, pending, data["image_action"], logo=logo)
            question["text"] = f"Картинка №{selected} недоступна. " + question["text"]
            return question
        choice = data["image_action"]
        if logo:
            args.logo_number = selected
        else:
            args.existing_image_number = selected
    elif choice == "existing":
        return await open_image_picker(ctx, pending, choice)
    elif choice == "layout" or (choice == "generate" and args.use_logo is True):
        originals = await image_choices(ctx, originals_only=True)
        if args.logo_number not in originals:
            return await open_image_picker(ctx, pending, choice, logo=True)
    args.with_image = choice in {"generate", "layout"}
    args.branded_layout = choice == "layout"
    if choice != "content":
        args.content_id = None
    ctx.request_id = data.get("request_id") or ctx.request_id
    await ctx.session.delete(pending)
    await ctx.session.commit()
    return await run_scenario(
        ctx, "create_post", data["text"], "", args=args, confirmed=True, research_key=research_key
    )


async def turn_picker_page(ctx, pending, choice):
    pending.token = token_hex(6)
    pending.data = {
        **pending.data,
        "image_page": pending.data["image_page"] + (1 if choice == "next" else -1),
    }
    return await draft_question(ctx, pending)


async def ask_revision_options(ctx, text, args, *, ask_copy=False, copy_source=False):
    previous = await ctx.session.get(PendingDraftOptions, (ctx.user_id, ctx.business_id))
    if previous and previous.data.get("research_id"):
        editorial.discard_post_research((ctx.business_id, previous.data["research_id"]))
    pending = await ctx.session.merge(
        PendingDraftOptions(
            user_id=ctx.user_id,
            business_id=ctx.business_id,
            stage="copy_draft" if ask_copy else "pick_logo",
            token=token_hex(6),
            data={
                "revision": True,
                "copy_source": ask_copy or copy_source,
                "text": text,
                "request_id": ctx.request_id,
                "args": args.model_dump(mode="json"),
            },
        )
    )
    if ask_copy:
        return await draft_question(ctx, pending)
    return await open_image_picker(ctx, pending, "revise", logo=True)


async def continue_revision(ctx, pending, choice, *, logo_number=None):
    data = pending.data
    args = post_tools.RevisePostArgs.model_validate(data["args"])
    if pending.stage == "pick_logo":
        if choice in {"previous", "next"}:
            return await turn_picker_page(ctx, pending, choice)
        number = logo_number or picker_page(data)[int(choice) - 1]["number"]
        if number not in await image_choices(ctx, originals_only=True):
            question = await open_image_picker(ctx, pending, "revise", logo=True)
            question["text"] = f"Картинка №{number} недоступна. " + question["text"]
            return question
        args.logo_number = number
    copy_source = data.get("copy_source", data.get("copy_published", False))
    confirmed = pending.stage == "pick_logo"
    ctx.request_id = data.get("request_id") or ctx.request_id
    await ctx.session.delete(pending)
    await ctx.session.commit()
    return await run_scenario(
        ctx,
        "revise_post",
        data["text"],
        "",
        args=args,
        confirmed=confirmed,
        copy_source=copy_source,
    )


async def resume_chat_question(ctx, text):
    value = text.strip().casefold().rstrip(".!")
    image_reply = {
        "без картинки": "none",
        "без фото": "none",
        "выбрать существующую": "existing",
        "сгенерировать": "generate",
        "сгенерировать картинку": "generate",
        "фирменная карточка": "layout",
    }.get(value)
    valid_confirmation = value in {
        "да",
        "давай",
        "да, давай",
        "давай сделаем",
        "создай",
        "создай новый",
        "создай черновик",
        "создай новый черновик",
        "ок",
        "окей",
        "подтверждаю",
    }
    number = re.fullmatch(
        r"(?:(?:логотип|картинка|номер(?:\s+(?:логотипа|картинки))?)\s*)?[№#]?\s*([1-9][0-9]{0,9})",
        value,
    )
    cancelled = value in {"нет", "отмена", "отмени", "отменить"}
    no_changes = bool(NO_CHANGES.fullmatch(value))
    style = next(
        (
            style
            for pattern, style in (
                (r"анти[\s‐‑–—-]*(?:ллм|llm)", posts.PostStyle.ANTI_LLM),
                (r"официальн\w*", posts.PostStyle.OFFICIAL),
                (r"научн\w*", posts.PostStyle.SCIENTIFIC),
                (r"соцсет\w*|(?:для\s+)?социальн\w*\s+сет\w*", posts.PostStyle.SOCIAL),
                (r"обычн\w*|стандартн\w*", posts.PostStyle.NEUTRAL),
            )
            if re.fullmatch(
                rf"(?:(?:давай|только|лучше|в|на|поменяй стиль|перепиши)\s+)*(?:{pattern})(?:\s+стил[ьея])?",
                value,
            )
        ),
        None,
    )
    if not (valid_confirmation or number or cancelled or no_changes or style or image_reply):
        return None
    pending = await ctx.session.get(
        PendingDraftOptions, (ctx.user_id, ctx.business_id), with_for_update=True
    )
    if not pending or pending.updated_at < utcnow() - timedelta(hours=1):
        return None
    if cancelled:
        return await apply_draft_option(ctx, pending.token, "cancel")
    if pending.stage == "style" and style is not None:
        return await apply_draft_option(ctx, pending.token, style.value)
    if pending.stage == "image" and image_reply:
        question = await draft_question(ctx, pending)
        if image_reply in {item["id"] for item in question["draft_options"]["options"]}:
            return await apply_draft_option(ctx, pending.token, image_reply)
        return question
    if pending.stage == "copy_draft" and (valid_confirmation or no_changes or style):
        if no_changes or style is not None:
            args = post_tools.RevisePostArgs.model_validate(pending.data["args"])
            if no_changes:
                args = post_tools.RevisePostArgs(post_id=args.post_id, copy_as_draft=True)
            else:
                args.style = style
            pending.data = {**pending.data, "args": args.model_dump(mode="json")}
        return await continue_revision(ctx, pending, "copy_draft")
    if pending.stage in DRAFT_PICKER_STAGES and number:
        return await apply_draft_option(ctx, pending.token, "", image_number=int(number[1]))
    return None


class CritiqueRequest(BaseModel):
    post_id: int = Field(gt=0)


class SearchRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=350)
    question: str = Field(min_length=2, max_length=400)


class PublicationIntent(BaseModel):
    action: Literal["publish", "schedule", "cancel"]
    post_id: int | None = None
    when: str = ""


SCENARIOS = {
    "create_post": (DraftRequest, post_tools.create_post_draft, "Уточните тему нового поста."),
    "revise_post": (
        post_tools.RevisePostArgs,
        post_tools.revise_post,
        "Укажите номер поста и что изменить.",
    ),
    "list_posts": (
        post_tools.ListPostsArgs,
        post_tools.list_posts,
        "Уточните, какие посты показать.",
    ),
    "plan": (plan_tools.CreatePlanArgs, plan_tools.create_content_plan, "Уточните период плана."),
    "analytics": (
        analytics_tools.PeriodArgs,
        analytics_tools.get_analytics,
        "Уточните период статистики.",
    ),
    "report": (analytics_tools.PeriodArgs, analytics_tools.make_report, "Уточните период отчёта."),
}


async def answer(ctx: AgentContext, text: str, *, history_limit: int = 6) -> dict:
    if not ctx.interactive_drafts:
        return await respond(ctx, text, history_limit)
    repo = AgentMessageRepo(ctx.session)
    message = await repo.add(ctx.user_id, MessageRole.USER, text, business_id=ctx.business_id)
    ctx.request_id = message.id
    result = await respond(ctx, text, history_limit)
    if question := result.get("draft_options"):
        await record_question(ctx, result["text"], question["token"])
    elif ctx.request_id is not None:
        await remember(ctx, text, result["text"])
    await save_chat_proposal(ctx, result)
    return result


async def save_chat_proposal(ctx: AgentContext, result: dict) -> None:
    if (proposal := result.get("proposal")) and not result.get("draft_id"):
        await ctx.session.merge(
            PendingPostAction(
                user_id=ctx.user_id,
                business_id=ctx.business_id,
                proposal=proposal,
            )
        )


async def record_question(ctx: AgentContext, text: str, token: str) -> None:
    await AgentMessageRepo(ctx.session).add(
        ctx.user_id,
        MessageRole.ASSISTANT,
        text,
        business_id=ctx.business_id,
        name=f"question:{token}",
    )


async def respond(ctx: AgentContext, text: str, history_limit: int) -> dict:
    if ctx.interactive_drafts and (resumed := await resume_chat_question(ctx, text)) is not None:
        return resumed
    revision = re.fullmatch(r"Правка поста №(\d+)\s*:\s*(.{2,2000})", text.strip(), re.S)
    if revision:
        post_id, instruction = int(revision[1]), revision[2]
        args = await scenario_args(ctx, post_tools.RevisePostArgs, "revise_post", text, "")
        args = (
            args or post_tools.RevisePostArgs(post_id=post_id, instruction=instruction)
        ).model_copy(update={"post_id": post_id})
        return await run_scenario(ctx, "revise_post", text, "", args=args)
    explicit = re.fullmatch(
        r"(?:разбери|проанализируй)\s+пост\s*[№#]?\s*(\d+)[.!]?", text.strip(), re.I
    )
    if explicit:
        return {"text": await remember(ctx, text, await critique_reply(ctx, int(explicit[1])))}
    history = (
        await AgentMessageRepo(ctx.session).last_n(
            ctx.user_id,
            history_limit + int(ctx.request_id is not None),
            business_id=ctx.business_id,
        )
        if history_limit
        else []
    )
    history = [message for message in history if message.id != ctx.request_id]
    recent = "\n".join(
        f"{message.role.value}: {message.content[:500]}"
        + (
            f" [Карточка поста №{ref[1]}]"
            if message.role == MessageRole.ASSISTANT
            and (ref := re.fullmatch(r"draft:(\d+)(?:\|.*)?", message.name or ""))
            else ""
        )
        for message in history
        if message.role in {MessageRole.USER, MessageRole.ASSISTANT}
        and message.content
        and not message.tool_calls
    )
    vk = r"\b(?:вк|вконтакте|vk)\b"
    publish = r"\b(?:опубликова\w*|опублику\w*|вылож\w*|запости\w*|продублиру\w*)"
    number = re.fullmatch(
        r"(?:номер\s+поста\s*|(?:пост\s*)?[№#]?\s*)?(\d+)\s*[.!]?", text.strip(), re.I
    )
    previous_request = (
        next(
            (
                message.content
                for message in reversed(history)
                if message.role == MessageRole.USER
                and message.content
                and not re.fullmatch(
                    r"(?:номер\s+поста\s*)?[№#]?\s*\d+\s*[.!]?", message.content.strip(), re.I
                )
            ),
            "",
        )
        if number
        else ""
    )
    if (
        re.search(vk, text, re.I)
        and re.search(publish, text, re.I)
        and not re.search(r"\bкак\b", text, re.I)
    ) or (
        number
        and re.search(vk, previous_request, re.I)
        and re.search(publish, previous_request, re.I)
    ):
        explicit_id = re.search(r"(?:пост(?:а)?\s*|[№#]\s*)(\d+)", text, re.I)
        last_draft = next(
            (
                match
                for message in reversed(history)
                if message.role == MessageRole.ASSISTANT
                and (match := re.fullmatch(r"draft:(\d+)(?:\|.*)?", message.name or ""))
            ),
            None,
        )
        post_ref = number or explicit_id or last_draft
        return await propose_vk_copy(ctx, text, int(post_ref[1]) if post_ref else None)
    explicit_copy = bool(COPY_POST.search(text)) and not re.search(r"^как\s", text, re.I)
    older_copy = NO_CHANGES.fullmatch(text.strip()) and any(
        item.role == MessageRole.USER and COPY_POST.search(item.content or "")
        for item in history[-4:]
    )
    copy_number = number and previous_request and COPY_POST.search(previous_request)
    if explicit_copy or older_copy or copy_number:
        intent = "revise_post"
        ref = number if copy_number else POST_NUMBER.search(text)
        if older_copy:
            source = next(
                item.content
                for item in reversed(history)
                if item.role == MessageRole.USER and COPY_POST.search(item.content or "")
            )
            ref = POST_NUMBER.search(source)
        copy_request = previous_request if copy_number else text
        plain_copy = (
            older_copy
            or COPY_ONLY.fullmatch(copy_request.strip())
            or NO_CHANGES.search(copy_request)
        )
        args = (
            None
            if plain_copy
            else await scenario_args(ctx, post_tools.RevisePostArgs, intent, copy_request, recent)
        )
        args = args or post_tools.RevisePostArgs()
        args.copy_as_draft = True
        if ref:
            args.post_id = int(ref[1])
        if not args.has_changes:
            args.clarification = ""
        return await run_scenario(ctx, intent, text, recent, args=args)
    raw = await ctx.llm.complete(
        render_prompt("classify_intent"),
        request_with_history(text, recent),
        json_mode=True,
        task="classify_intent",
    )
    try:
        intent = Intent.model_validate_json(raw).intent
    except ValidationError:
        intent = "company"
    if intent == "web_search":
        if EXPLICIT_POST.match(text.strip()):
            intent = "create_post"
        elif not editorial.requested_web_search(text):
            intent = "company"
    if intent in {"critique", "web_search"}:
        schema = CritiqueRequest if intent == "critique" else SearchRequest
        args = await scenario_args(ctx, schema, intent, text, recent)
        if args is None:
            reply = (
                "Укажите номер опубликованного поста."
                if intent == "critique"
                else "Уточните, какой факт найти."
            )
        elif isinstance(args, CritiqueRequest):
            reply = await critique_reply(ctx, args.post_id)
        else:
            ctx.update_progress("search")
            brief = await editorial.research(
                ctx.research_llm, ctx.settings.tz, args.topic, args.question
            )
            reply = (brief or {}).get("summary") or "Подтверждённых сведений по запросу пока нет."
            if brief and brief["summary"]:
                reply += "\n\n" + "\n".join(source["url"] for source in brief["sources"])
        return {"text": await remember(ctx, text, reply)}
    if intent == "publish":
        return await propose_publication(ctx, text, recent)
    if intent in {"help", "integration"}:
        corpus = "integrations" if intent == "integration" else "platform"
        reply = await ctx.llm.complete(
            render_prompt("platform_help"),
            f"Справка:\n{search_help(text, corpus)}\n\nВопрос:\n{text}",
            task="platform_help",
        )
    elif intent in SCENARIOS:
        return await run_scenario(ctx, intent, text, recent)
    else:
        try:
            materials = await retrieve(ctx.session, ctx.settings, ctx.business_id, text)
        except LLMError:
            materials = ""
        reply = await ctx.llm.complete(
            render_prompt(
                "company_answer",
                profile=profile_to_text(ctx.business),
                materials=materials,
            ),
            request_with_history(text, recent),
            task=intent,
        )
    return {"text": await remember(ctx, text, reply)}


def request_with_history(text: str, history: str) -> str:
    if not history:
        return text
    return f"Недавние реплики:\n{history}\n\nТекущий запрос:\n{text}"


async def critique_reply(ctx, post_id):
    post = await PostRepo(ctx.session).get_for_business(post_id, ctx.business_id)
    if post is None:
        return "Пост не найден."
    try:
        ctx.update_progress("critique")
        result = await editorial.critique(
            ctx.session,
            ctx.research_llm or ctx.llm,
            ctx.business,
            post,
            ctx.settings.tz,
        )
    except ValueError as exc:
        return str(exc)
    return (
        f"Пост №{post.id} · {post.topic}\n\n{result['facts']}\n\n"
        f"Что могло повлиять: {result['hypothesis']}\n\nЧто проверить: {result['next']}"
    )


async def scenario_args(ctx, schema, intent, text, history):
    catalog = ""
    profile_catalog = "[]"
    if intent in {"create_post", "revise_post"}:
        images = (await content_gallery.list_content(ctx.session, ctx.business_id))[:150]
        catalog = json.dumps(
            [[item.id, item.number, item.label] for item in images], ensure_ascii=False
        )
        documents = await profile_images(ctx.session, ctx.business_id)
        profile_catalog = json.dumps(
            [[number, doc.filename] for number, doc in documents.items()], ensure_ascii=False
        )
    prompt = (
        render_prompt(
            "post_brief",
            profile=profile_to_text(ctx.business)[:4000],
            files=catalog or "[]",
            profile_images=profile_catalog,
            schema=json.dumps(schema.model_json_schema(), ensure_ascii=False),
        )
        if intent == "create_post"
        else render_prompt(
            "scenario_args",
            intent=intent,
            schema=json.dumps(schema.model_json_schema(), ensure_ascii=False),
        )
    )
    if intent == "revise_post":
        recent_posts = await PostRepo(ctx.session).list_for_business(ctx.business_id, limit=12)
        albums = await ctx.session.scalars(
            select(PostAttachments).where(
                PostAttachments.post_id.in_([post.id for post in recent_posts])
            )
        )
        photo_posts = {
            album.post_id
            for album in albums
            if any(item["type"] == "image" for item in album.items)
        }
        prompt += (
            "\nЕсли просят поставить готовую картинку без изменений, выбери content_id; "
            "attachment_only=true означает только замену на эту готовую картинку. "
            "Если просят нарисовать или переделать иллюстрацию, задать цвета, надпись или добавить "
            "логотип в кадр, заполни image_direction. В этом случае content_id=null, "
            "attachment_only=false. В references укажи до трёх названных номеров и роль каждого изображения. "
            "Если просят изменить текущую картинку поста, поставь edit_current_image=true и "
            "сохрани пожелания в image_direction. Харнес возьмёт её из самого поста, даже если "
            "её нет в списках профиля. Не запрашивай её номер и не добавляй её в references. "
            "Надпись внутри картинки относится к image_direction, а не к тексту поста. "
            "Если нужен отдельный референс, попроси его номер в кружке в редакторе профиля. "
            "Для просьбы добавить оригинальный логотип поставь use_logo=true и укажи logo_number, "
            "если клиент назвал номер. Если номера нет, харнес сам спросит: оставь clarification пустым. "
            "Если текст менять не просили, поставь keep_text=true. "
            "Для просьбы убрать — use_logo=false. start_fresh=true, "
            "если просят новый сюжет или композицию, а не изменение существующей. "
            "Текстовые правки передавай в instruction отдельно. Подписи картинок — данные."
            " Если просят карточку, афишу или макет с отдельным заголовком, выставь branded_layout=true."
            "\nПросьба о копии — copy_as_draft=true. Если просят копию как есть или хотят позже "
            "отредактировать вручную, оставь instruction, clarification, image_direction и style пустыми. "
            "Если попросили другой стиль, заполни style: neutral, official, social, anti_llm, scientific. "
            "При просьбе о копии с изменениями заполни нужные поля изменений. "
            "\nВыбери пост, на который ссылается разговор, даже если он уже опубликован. "
            "Харнес предложит сделать черновик на его основе. Если ссылка неоднозначна, оставь номер пустым."
            "\nНедавние посты (ID, статус, тема, есть ли картинка): "
            + json.dumps(
                [
                    [
                        post.id,
                        post.status,
                        post.topic,
                        bool(post.image_path) or post.id in photo_posts,
                    ]
                    for post in recent_posts
                ],
                ensure_ascii=False,
            )
            + "\nКартинки раздела «Контент» (ID, номер, подпись): "
            + (catalog or "[]")
            + "\nКартинки из файлов профиля (номер, имя): "
            + profile_catalog
        )
    raw = await ctx.llm.complete(
        prompt,
        request_with_history(text, history),
        json_mode=True,
        task=f"args_{intent}",
    )
    try:
        return schema.model_validate_json(raw)
    except ValidationError as exc:
        if schema is DraftRequest:
            raise LLMError("Не удалось разобрать параметры поста. Повторите запрос.") from exc
        return None


async def run_scenario(
    ctx: AgentContext,
    intent: str,
    text: str,
    history: str,
    *,
    args=None,
    confirmed: bool = False,
    copy_source: bool = False,
    research_key=None,
) -> dict:
    schema, run, clarify = SCENARIOS[intent]
    if args is None:
        args = await scenario_args(ctx, schema, intent, text, history)
    if args is None:
        return {"text": await remember(ctx, text, clarify)}
    if isinstance(args, DraftRequest):
        match = EXPLICIT_POST.match(text.strip())
        topic = match[1].strip(" .?!") if match else args.topic.strip()
        if len(topic) >= 2 and not re.fullmatch(
            r"(?:в\s+)?(?:вк|вконтакте|телеграм\w*|tg|vk|telegram)", topic, re.I
        ):
            if len(args.topic.strip()) < 2:
                args.topic = topic[:1000]
            clarification = args.clarification.strip()
            if REPEATED_TOPIC_QUESTION.match(clarification):
                args.clarification = ""
    if isinstance(args, DraftRequest) and (
        args.clarification.strip() or len(args.topic.strip()) < 2
    ):
        reply = args.clarification.strip() or clarify
        if args.suggestion.strip():
            reply += "\n\n" + args.suggestion.strip()
        return {"text": await remember(ctx, text, reply)}
    if isinstance(args, DraftRequest):
        if args.platform is None:
            args.platform = await post_tools.sole_or_default_platform(ctx)
        if args.platform is None or args.platform == "max":
            reply = (
                "Куда готовить пост: в Telegram, во ВКонтакте или в обе соцсети?"
                if args.platform is None
                else "Для черновика выберите Telegram или ВКонтакте."
            )
            return {"text": await remember(ctx, text, reply)}
    when = None
    if isinstance(args, DraftRequest) and args.when:
        try:
            when = parse_schedule_time(args.when, ctx.settings.tz)
        except ValueError:
            return {"text": await remember(ctx, text, "Уточните дату и время публикации.")}
    if intent == "revise_post":
        if (
            args.edit_current_image
            and args.image_direction
            and CURRENT_IMAGE_QUESTION.search(args.clarification)
        ):
            args.clarification = ""
        if args.clarification.strip():
            return {"text": await remember(ctx, text, args.clarification)}
        if args.post_id is None:
            recent_posts = re.findall(
                r"^assistant: [^\n]*\[Карточка поста №(\d+)\]|^assistant: Черновик №(\d+)",
                history,
                re.I | re.M,
            )
            if recent_posts:
                args.post_id = int(next(value for value in recent_posts[-1] if value))
            else:
                return {
                    "text": await remember(
                        ctx,
                        text,
                        "Какой пост скопировать? Укажите его номер."
                        if args.copy_as_draft
                        else "Какой пост изменить? Укажите его номер.",
                    )
                }
        post = await PostRepo(ctx.session).get_for_business(args.post_id, ctx.business_id)
        if post is None or post.status == PostStatus.REJECTED:
            return {"text": await remember(ctx, text, "Этот пост недоступен для правок.")}
        if (post.status == PostStatus.PUBLISHED or args.copy_as_draft) and not copy_source:
            if ctx.interactive_drafts:
                return await ask_revision_options(ctx, text, args, ask_copy=True)
            return {
                "text": await remember(
                    ctx,
                    text,
                    "Пост уже опубликован. Откройте чат, чтобы создать черновик с правкой.",
                )
            }
        if (
            args.image_direction
            and args.branded_layout
            and not args.edit_current_image
            and (args.use_logo is False or not await brand_materials(ctx.session, ctx.business))
        ):
            missing = await layout_requirement(
                ctx.session, ctx.business, storage=ctx.storage, use_logo=args.use_logo
            )
            return {"text": await remember(ctx, text, missing)}
        if (
            ctx.interactive_drafts
            and not confirmed
            and args.image_direction
            and (args.use_logo is True or (args.branded_layout and not args.edit_current_image))
            and (post.platform != Platform.VK or ctx.settings.vk_is_business_account)
            and args.logo_number not in await image_choices(ctx, originals_only=True)
        ):
            return await ask_revision_options(ctx, text, args, copy_source=copy_source)
    content = None
    if intent in {"create_post", "revise_post"} and args.content_id is not None:
        content = await content_gallery.get_content(ctx.session, ctx.business_id, args.content_id)
        if content is None and not (
            isinstance(args, DraftRequest) and ctx.interactive_drafts and not confirmed
        ):
            return {
                "text": await remember(
                    ctx, text, "Не нашёл такую картинку в разделе «Контент». Уточните, какую."
                )
            }
        vk_only = (
            args.platform == "vk"
            if isinstance(args, DraftRequest)
            else post.platform == Platform.VK
        )
        if (
            vk_only
            and not ctx.settings.vk_is_business_account
            and not isinstance(args, DraftRequest)
        ):
            return {
                "text": await remember(
                    ctx, text, "Во ВКонтакте картинки пока недоступны: нужен бизнес-аккаунт."
                )
            }
    if isinstance(args, DraftRequest) and ctx.interactive_drafts and not confirmed:
        return await ask_draft_options(ctx, text, args)
    if intent == "revise_post" and args.attachment_only and not copy_source:
        if content is None:
            return {
                "text": await remember(ctx, text, "Какую картинку из раздела «Контент» поставить?")
            }
        if ctx.request_id is None:
            request = await AgentMessageRepo(ctx.session).add(
                ctx.user_id, MessageRole.USER, text, business_id=ctx.business_id
            )
            ctx.request_id = request.id
        await content_gallery.replace_post_image(ctx.session, post, content, author_id=ctx.user_id)
        await approval.submit_for_approval(ctx.session, post, ctx.notifier, ctx.target_chat_id)
        return {
            "text": await remember(
                ctx,
                text,
                f"Поставил картинку «{content.label}» в черновик №{post.id}. Он снова ждёт согласования.",
                draft_id=post.id,
            ),
            "draft_id": post.id,
        }
    if intent == "create_post" or (intent == "revise_post" and args.changes_text):
        style = args.style
        if intent == "revise_post" and style is None:
            style = await posts.post_style(ctx.session, post.id)
        topic = args.topic if isinstance(args, DraftRequest) else post.topic
        if research_key is None:
            research_key = (ctx.business_id, token_hex(8))
        start_research(ctx, research_key, topic, text, style)
    if intent in {"create_post", "plan"} or (intent == "revise_post" and args.image_direction):
        query = (
            args.topic
            if isinstance(args, DraftRequest)
            else f"{post.topic} {args.image_direction}"
            if intent == "revise_post"
            else text
        )
        try:
            ctx.reference = await retrieve(ctx.session, ctx.settings, ctx.business_id, query)
        except LLMError:
            ctx.reference = ""
    if ctx.request_id is None and intent in {"create_post", "revise_post", "plan"}:
        request = await AgentMessageRepo(ctx.session).add(
            ctx.user_id, MessageRole.USER, text, business_id=ctx.business_id
        )
        ctx.request_id = request.id
    if intent == "plan":
        ctx.update_progress("plan")
    elif intent == "revise_post":
        ctx.update_progress("revise")
    ctx.web_context = editorial.take_post_research(research_key) if research_key else ""
    try:
        result = await run(ctx, args, copy_source=True) if copy_source else await run(ctx, args)
    except posts.MissingPostInformation as exc:
        return {"text": await remember(ctx, text, str(exc))}
    if result.get("error"):
        reply = result["error"]
    elif intent in {"create_post", "revise_post"}:
        if result.get("post_ids"):
            post_ids = result["post_ids"]
            for index, post_id in enumerate(post_ids):
                reply = f"Черновик №{post_id} готов и ждёт согласования."
                await remember(
                    ctx, text, reply, draft_id=post_id, draft_when=when, save_request=index == 0
                )
            return {
                "text": "Готовы два черновика: для Telegram и ВКонтакте.",
                "reload_history": True,
            }
        reply = f"Черновик №{result['post_id']} готов и ждёт согласования."
        if result.get("warning"):
            reply += " " + result["warning"]
        if isinstance(args, DraftRequest) and args.suggestion.strip():
            reply += "\n\n" + args.suggestion.strip()
        if when is not None:
            return await propose_draft_schedule(ctx, text, reply, result["post_id"], when)
        return {
            "text": await remember(ctx, text, reply, draft_id=result["post_id"]),
            "draft_id": result["post_id"],
        }
    elif intent == "plan":
        topics = "\n".join(f"{item['date']} · {item['topic']}" for item in result["items"])
        reply = "Контент-план готов. Утвердите его на главной в Mini App.\n" + topics
    elif intent == "report":
        reply = result["text"]
    else:
        reply = await ctx.llm.complete(
            render_prompt("scenario_result"),
            json.dumps(result, ensure_ascii=False),
            task=f"result_{intent}",
        )
    return {"text": await remember(ctx, text, reply)}


async def propose_draft_schedule(
    ctx: AgentContext, text: str, reply: str, post_id: int, when: datetime
) -> dict:
    post = await PostRepo(ctx.session).get_for_business(post_id, ctx.business_id)
    proposal = {
        "id": post.id,
        "action": "schedule",
        "company": ctx.business_id,
        "version": post.updated_at.isoformat(),
        "when": when.isoformat(),
    }
    reply += f" Запланировать на {when:%d.%m.%Y %H:%M %Z}? Подтвердите действие."
    return {
        "text": await remember(ctx, text, reply, draft_id=post.id, draft_when=when),
        "proposal": proposal,
        "draft_id": post.id,
    }


async def remember(
    ctx: AgentContext,
    text: str,
    reply: str,
    *,
    draft_id: int | None = None,
    draft_when: datetime | None = None,
    save_request: bool = True,
) -> str:
    history = AgentMessageRepo(ctx.session)
    if save_request and ctx.request_id is None:
        await history.add(ctx.user_id, MessageRole.USER, text, business_id=ctx.business_id)
    ctx.request_id = None
    draft_ref = f"draft:{draft_id}" if draft_id is not None else None
    if draft_ref and draft_when is not None:
        draft_ref += "|" + draft_when.isoformat()
    await history.add(
        ctx.user_id,
        MessageRole.ASSISTANT,
        reply,
        business_id=ctx.business_id,
        name=draft_ref,
    )
    return reply


async def propose_vk_copy(ctx: AgentContext, text: str, post_id: int | None) -> dict:
    if await ChannelRepo(ctx.session).get_active(ctx.business_id, Platform.VK) is None:
        reply = "Сначала подключите сообщество ВКонтакте в настройках профиля."
    elif post_id is None:
        reply = "Укажите номер поста, который нужно скопировать во ВКонтакте."
    else:
        post = await PostRepo(ctx.session).get_for_business(post_id, ctx.business_id)
        if post and post.platform == Platform.TELEGRAM and post.status != PostStatus.REJECTED:
            return {
                "text": await remember(
                    ctx,
                    text,
                    f"Создать копию поста №{post.id} для ВКонтакте? Подтвердите действие.",
                ),
                "proposal": {
                    "action": "copy_to_vk",
                    "id": post.id,
                    "company": ctx.business_id,
                    "version": post.updated_at.isoformat(),
                },
            }
        reply = f"Пост №{post_id} недоступен для копирования из Telegram."
    return {"text": await remember(ctx, text, reply)}


async def propose_publication(ctx: AgentContext, text: str, history: str) -> dict:
    candidates = [
        post
        for post in await PostRepo(ctx.session).list_for_business(ctx.business_id)
        if post.status
        in {
            PostStatus.PENDING_APPROVAL,
            PostStatus.APPROVED,
            PostStatus.SCHEDULED,
            PostStatus.FAILED,
        }
    ]
    raw = await ctx.llm.complete(
        render_prompt(
            "publish_intent",
            posts="\n".join(f"#{post.id} ({post.status}) {post.topic}" for post in candidates),
        ),
        request_with_history(text, history),
        json_mode=True,
        task="publish_intent",
    )
    try:
        intent = PublicationIntent.model_validate_json(raw)
        post = next(item for item in candidates if item.id == intent.post_id)
    except (ValueError, StopIteration):
        return {
            "text": await remember(
                ctx, text, "Уточните номер поста. Его можно посмотреть на главной."
            )
        }
    valid_statuses = {
        "publish": {PostStatus.PENDING_APPROVAL, PostStatus.APPROVED, PostStatus.FAILED},
        "schedule": {PostStatus.PENDING_APPROVAL, PostStatus.APPROVED, PostStatus.SCHEDULED},
        "cancel": {PostStatus.APPROVED, PostStatus.SCHEDULED},
    }
    if post.status not in valid_statuses[intent.action]:
        return {
            "text": await remember(
                ctx,
                text,
                "Это действие недоступно для текущего статуса поста. Проверьте его на главной.",
            )
        }
    action = {
        "id": post.id,
        "action": intent.action,
        "company": ctx.business_id,
        "version": post.updated_at.isoformat(),
    }
    if intent.action == "schedule":
        try:
            when = parse_schedule_time(intent.when, ctx.settings.tz)
        except ValueError:
            return {"text": await remember(ctx, text, "Уточните дату и время публикации.")}
        action["when"] = when.isoformat()
    labels = {
        "publish": "Опубликовать",
        "schedule": "Запланировать",
        "cancel": "Снять с расписания",
    }
    label = f"{labels[intent.action]} пост №{post.id}"
    if intent.action == "schedule":
        label += f" на {when:%d.%m.%Y %H:%M %Z}"
    return {
        "text": await remember(ctx, text, label + "? Подтвердите действие."),
        "proposal": action,
    }
