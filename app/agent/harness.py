"""Маршрутизация мини-приложения. Публикация остаётся действием пользователя."""

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.agent.context import AgentContext
from app.agent.help_search import search_help
from app.agent.prompts import render_prompt
from app.agent.tools import analytics_tools, plan_tools, post_tools
from app.db.models import MessageRole, PostStatus
from app.db.repositories import PostRepo
from app.db.repositories.agent_message import AgentMessageRepo
from app.services.business_profile import profile_to_text
from app.services.knowledge_search import retrieve
from app.utils.time import parse_schedule_time


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
        "suggest_topic",
        "company",
    ]


class DraftRequest(post_tools.CreatePostArgs):
    when: str = Field("", max_length=200, description="Желаемая дата публикации из запроса")


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
    history = (
        await AgentMessageRepo(ctx.session).last_n(
            ctx.user_id, history_limit, business_id=ctx.business_id
        )
        if history_limit
        else []
    )
    recent = "\n".join(
        f"{message.role.value}: {message.content[:500]}"
        for message in history
        if message.role in {MessageRole.USER, MessageRole.ASSISTANT}
        and message.content
        and not message.tool_calls
    )
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
        materials = await retrieve(ctx.session, ctx.settings, ctx.business_id, text)
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


async def run_scenario(ctx: AgentContext, intent: str, text: str, history: str) -> dict:
    schema, run, clarify = SCENARIOS[intent]
    raw = await ctx.llm.complete(
        render_prompt(
            "scenario_args",
            intent=intent,
            schema=json.dumps(schema.model_json_schema(), ensure_ascii=False),
        ),
        request_with_history(text, history),
        json_mode=True,
        task=f"args_{intent}",
    )
    try:
        args = schema.model_validate_json(raw)
    except ValidationError:
        return {"text": await remember(ctx, text, clarify)}
    when = None
    if isinstance(args, DraftRequest) and args.when:
        try:
            when = parse_schedule_time(args.when, ctx.settings.tz)
        except ValueError:
            return {"text": await remember(ctx, text, "Уточните дату и время публикации.")}
    if intent == "revise_post":
        post = await PostRepo(ctx.session).get_for_business(args.post_id, ctx.business_id)
        if post is None or post.status in {PostStatus.PUBLISHED, PostStatus.REJECTED}:
            return {"text": await remember(ctx, text, "Этот пост недоступен для правок.")}
    if intent in {"create_post", "plan"}:
        query = args.topic if isinstance(args, DraftRequest) else text
        ctx.reference = await retrieve(ctx.session, ctx.settings, ctx.business_id, query)
    if intent in {"create_post", "revise_post", "plan"}:
        request = await AgentMessageRepo(ctx.session).add(
            ctx.user_id, MessageRole.USER, text, business_id=ctx.business_id
        )
        ctx.request_id = request.id
    result = await run(ctx, args)
    if result.get("error"):
        reply = result["error"]
    elif result.get("needs_platform_choice"):
        reply = result["message"]
    elif intent in {"create_post", "revise_post"}:
        if result.get("posts"):
            numbers = ", ".join(f"№{item['post_id']}" for item in result["posts"])
            reply = f"Черновики {numbers} готовы и ждут согласования: по одному на каждую соцсеть."
            when = None  # время назначают при согласовании каждого поста
        else:
            reply = f"Черновик №{result['post_id']} готов и ждёт согласования."
        if result.get("warning"):
            reply += " " + result["warning"]
        if when is not None:
            return await propose_draft_schedule(ctx, text, reply, result["post_id"], when)
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
    return {"text": await remember(ctx, text, reply), "proposal": proposal}


async def remember(ctx: AgentContext, text: str, reply: str) -> str:
    history = AgentMessageRepo(ctx.session)
    if ctx.request_id is None:
        await history.add(ctx.user_id, MessageRole.USER, text, business_id=ctx.business_id)
    ctx.request_id = None
    await history.add(ctx.user_id, MessageRole.ASSISTANT, reply, business_id=ctx.business_id)
    return reply


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
