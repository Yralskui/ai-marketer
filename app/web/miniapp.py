"""Telegram Mini App: профили, материалы, расписание и чат."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from base64 import b64encode
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import unquote
from uuid import UUID, uuid4
from xml.etree.ElementTree import ParseError

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiohttp import web
from botocore.exceptions import BotoCoreError, ClientError
from defusedxml.common import DefusedXmlException
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, func, select, text, update

from app.agent.context import AgentContext
from app.agent.harness import active_draft_options, answer, choose_draft_option
from app.config import IMAGES_DIR
from app.db.knowledge import (
    MiniUserState,
    PendingDraftOptions,
    PendingPostAction,
    ProfileDocument,
)
from app.db.models import (
    AgentMessage,
    Business,
    Channel,
    Comment,
    ContentPlan,
    ContentPlanItem,
    ContentPlanStatus,
    EventLog,
    EventType,
    MediaFile,
    MessageRole,
    Platform,
    Post,
    PostRevision,
    PostStatus,
    Source,
    User,
    UserRole,
)
from app.db.publications import (
    ChannelSnapshot,
    PostAttachments,
    PostCritique,
    PostSnapshot,
    RevisionAttachments,
    RevisionDecision,
    RevisionProvenance,
)
from app.db.repositories import (
    BusinessRepo,
    ChannelRepo,
    CommentRepo,
    EventLogRepo,
    MediaRepo,
    PostRepo,
    UserRepo,
)
from app.db.repositories.agent_message import AgentMessageRepo
from app.ingestion.profile_files import (
    FORMAT_ERROR,
    IMAGE_EXTENSIONS,
    PROFILE_FILE_EXTENSIONS,
    ProfileFileFormatError,
    render_profile_image,
    validate_profile_file,
)
from app.ingestion.website import WebsiteFetchError, fetch_site_text, pages_to_text, public_url
from app.llm.base import LLMError
from app.publishers.base import PublishError
from app.publishers.telegram_text import format_text, formatted_post
from app.publishers.vk import VKPublisher
from app.services import (
    accounts,
    analytics,
    approval,
    business_profile,
    comments,
    content_gallery,
    content_plan,
    editorial,
    media,
    posts,
    reports,
    social_metrics,
    vk_channel,
)
from app.services.document_uploads import (
    DocumentQuotaError,
    ImageLimitError,
    remaining_bytes,
    save_document,
    stage_document,
)
from app.services.image_references import document_image_number
from app.services.post_history import RevisionSource, save_revision
from app.services.usage import BudgetExceededError, get_budget, month_spend, set_budget, usage_scope
from app.utils.time import format_local, to_local, to_utc_naive, utcnow
from app.web.telegram_auth import validate_init_data
from app.web.uploads import UPLOAD_TIMEOUT

if TYPE_CHECKING:
    from app.container import AppContainer

log = logging.getLogger(__name__)
DEFAULT_PROFILE_NAME = "Салават Стекло"
ASSETS = Path(__file__).parent / "miniapp"
POST_LIMIT = 75
DOCUMENT_PREVIEW_MAX_BYTES = 12_000_000
SECURITY = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'self' https://telegram.org; "
    "style-src 'self'; img-src 'self' https: blob:; media-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'self' https://web.telegram.org",
}


def json_response(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=SECURITY)


def has_document_preview(document: ProfileDocument) -> bool:
    return (
        document.size <= DOCUMENT_PREVIEW_MAX_BYTES
        and Path(document.filename).suffix.lower() in IMAGE_EXTENSIONS
    )


class MiniApp:
    def __init__(self, container: AppContainer):
        self.container = container
        self.active_uploads = 0
        self.channel_labels: dict[str, tuple[float, str]] = {}
        self.chat_progress: dict[tuple[int, int, UUID], str] = {}
        self.chat_jobs: set[asyncio.Task] = set()
        self.chat_results: dict[tuple[int, int, UUID], dict] = {}

    def register(self, app: web.Application) -> None:
        app.router.add_get("/miniapp", self.asset)
        app.router.add_get(
            r"/miniapp/{asset:ui\.js|post-text\.js|ui\.css|icons\.svg|glass\.webp}", self.asset
        )
        app.router.add_route(
            "*",
            "/miniapp/api/{action:state|profiles|select|profile|document|content|post|plan|chat|chat-progress|preview|settings|comment|photo|report|history|diagnostics|website|metrics|post-pool|versions|critique|attachment|channel-status}",
            self.api,
        )

    async def asset(self, request: web.Request) -> web.StreamResponse:
        asset = request.match_info.get("asset")
        if asset:
            return web.FileResponse(ASSETS / asset, headers=SECURITY)
        page = (ASSETS / "index.html").read_text(encoding="utf-8")
        icons = (ASSETS / "icons.svg").read_text(encoding="utf-8")
        return web.Response(
            text=page.replace("<!-- miniapp-icons -->", icons),
            content_type="text/html",
            headers=SECURITY,
        )

    async def api(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        method = request.method
        if request.headers.get("Origin") not in {None, self.container.settings.miniapp_public_url}:
            return json_response({"error": "Неверный источник запроса"}, 403)
        if (action, method) not in {
            ("state", "GET"),
            ("channel-status", "GET"),
            ("report", "GET"),
            ("history", "GET"),
            ("metrics", "GET"),
            ("critique", "POST"),
            ("attachment", "PUT"),
            ("post-pool", "GET"),
            ("versions", "GET"),
            ("diagnostics", "GET"),
            ("preview", "GET"),
            ("profiles", "POST"),
            ("select", "POST"),
            ("profile", "PATCH"),
            ("profile", "DELETE"),
            ("profile", "POST"),
            ("document", "PUT"),
            ("document", "DELETE"),
            ("document", "POST"),
            ("document", "PATCH"),
            ("document", "GET"),
            ("content", "GET"),
            ("content", "PUT"),
            ("content", "DELETE"),
            ("website", "POST"),
            ("post", "POST"),
            ("post", "PUT"),
            ("photo", "GET"),
            ("photo", "PUT"),
            ("photo", "POST"),
            ("plan", "POST"),
            ("chat", "GET"),
            ("chat", "POST"),
            ("chat", "DELETE"),
            ("chat-progress", "GET"),
            ("settings", "POST"),
            ("comment", "POST"),
        }:
            return json_response({"error": "Метод недоступен"}, 405)
        authorization = request.headers.get("Authorization", "")
        try:
            if not authorization.startswith("Telegram "):
                raise ValueError("Telegram authorization required")
            identity = validate_init_data(
                authorization.removeprefix("Telegram "),
                self.container.settings.bot_token,
            )
        except (ValueError, KeyError):
            return json_response({"error": "Откройте приложение из Telegram."}, 401)
        settings = self.container.settings
        if settings.telegram_allowlist_only and identity.id not in settings.allowed_ids:
            return json_response({"error": "Доступ к приложению закрыт"}, 403)
        try:
            async with self.container.db.sessionmaker() as session:
                # Параллельные запросы первого открытия создают только одну копию шаблона.
                if session.get_bind().dialect.name == "postgresql":
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:user_key)"),
                        {"user_key": -identity.id},
                    )
                user = await UserRepo(session).get_by_telegram_id(identity.id)
                if user is None:
                    if (
                        not self.container.settings.miniapp_open_signup
                        and identity.id not in self.container.settings.allowed_ids
                    ):
                        return json_response(
                            {"error": "Войдите по приглашению или обратитесь к администратору."},
                            403,
                        )
                    role = (
                        UserRole.ADMIN
                        if identity.id in self.container.settings.allowed_ids
                        else UserRole.OWNER
                    )
                    user = await UserRepo(session).get_or_create(
                        identity.id, identity.name, role=role
                    )
                selection = await session.get(MiniUserState, user.id)
                default = None
                if user.is_active and (
                    not accounts.is_admin(user)
                    or user.telegram_id in self.container.settings.allowed_ids
                ):
                    selection, default = await self.initialize_profiles(session, user, selection)
                await session.commit()
                if not user.is_active or (
                    accounts.is_admin(user)
                    and user.telegram_id not in self.container.settings.allowed_ids
                ):
                    return json_response({"error": "Доступ отозван"}, 403)
                companies = await accounts.accessible_profiles(session, user)
                allowed = {company.id: company for company in companies}
                active = allowed.get(selection.selected_id)
                if action == "state":
                    return await self.state(session, identity, user, companies, active)
                if action == "chat-progress":
                    company_id = int(request.query["company"])
                    if company_id not in allowed:
                        return json_response({"error": "Профиль недоступен"}, 404)
                    key = (user.id, company_id, UUID(request.query["id"]))
                    progress = {"stage": self.chat_progress.get(key)}
                    if key in self.chat_results:
                        progress["result"] = self.chat_results[key]
                    return json_response(progress)
                if action == "channel-status":
                    company = allowed.get(int(request.query.get("company", 0)))
                    if company is None:
                        return json_response({"error": "Профиль недоступен"}, 404)
                    platform = Platform(request.query.get("platform", "telegram"))
                    channel = await ChannelRepo(session).get_active(company.id, platform)
                    if platform == Platform.VK:
                        return json_response(await self.vk_channel_status(channel))
                    if platform == Platform.TELEGRAM:
                        return json_response(await self.channel_status(channel))
                    raise ValueError("Проверка подключения недоступна для этой площадки.")
                if action == "chat" and method == "GET":
                    if active is None:
                        return json_response({"error": "Выберите профиль"}, 409)
                    return json_response(await self.chat_state(session, user, active))
                if action == "document" and method == "GET":
                    company = allowed.get(int(request.query["company"]))
                    if company is None or company.owner_id != user.id:
                        return json_response({"error": "Материал недоступен"}, 404)
                    return await self.document_preview(
                        session, company.id, int(request.query["id"])
                    )
                if action == "content" and method == "GET":
                    company = allowed.get(int(request.query["company"]))
                    if company is None or company.owner_id != user.id:
                        return json_response({"error": "Изображение недоступно"}, 404)
                    item = await content_gallery.get_content(
                        session, company.id, request.query.get("id")
                    )
                    if item is None:
                        return json_response({"error": "Изображение недоступно"}, 404)
                    return web.FileResponse(item.path, headers=SECURITY)
                if action == "content" and method == "PUT":
                    if (
                        active is None
                        or active.owner_id != user.id
                        or int(request.query.get("company", 0)) != active.id
                    ):
                        return json_response({"error": "Выберите свой профиль"}, 403)
                    return await self.upload_content(request, session, active)
                if action in {
                    "report",
                    "history",
                    "diagnostics",
                    "metrics",
                    "post-pool",
                    "versions",
                }:
                    if active is None:
                        return json_response({"error": "Выберите профиль"}, 404)
                    if action == "report":
                        return await self.report(session, active, int(request.query.get("days", 7)))
                    if action == "history":
                        return await self.publication_history(session, active)
                    if action == "metrics":
                        return await self.metrics(session, active, request.query)
                    if action == "post-pool":
                        return await self.post_pool(
                            session, active, int(request.query.get("before", 0))
                        )
                    if action == "versions":
                        post = await PostRepo(session).get_for_business(
                            int(request.query["id"]), active.id
                        )
                        if post is None:
                            return json_response({"error": "Пост не найден"}, 404)
                        return await self.versions(session, post)
                    return await self.diagnostics(session, active)
                if action == "preview":
                    post_id = int(request.query.get("id", 0))
                    post = (
                        await PostRepo(session).get_for_business(post_id, active.id)
                        if active
                        else None
                    )
                    image_path = post.image_path if post else None
                    if post and "revision" in request.query:
                        image_path = await session.scalar(
                            select(RevisionProvenance.image_path)
                            .join(PostRevision, PostRevision.id == RevisionProvenance.revision_id)
                            .where(
                                PostRevision.post_id == post.id,
                                PostRevision.id == int(request.query["revision"]),
                            )
                        )
                    if post and "media" in request.query:
                        revision_id = int(request.query.get("revision", 0))
                        revision = (
                            await session.get(PostRevision, revision_id) if revision_id else None
                        )
                        album = (
                            await session.get(RevisionAttachments, revision_id)
                            if revision and revision.post_id == post.id
                            else await session.get(PostAttachments, post.id)
                            if not revision_id
                            else None
                        )
                        index = int(request.query["media"])
                        image_path = (
                            album.items[index]["path"]
                            if album and 0 <= index < len(album.items)
                            else None
                        )
                    if not image_path:
                        return json_response({"error": "Изображение недоступно"}, 404)
                    path = Path(image_path).resolve()
                    if (
                        not path.is_relative_to(IMAGES_DIR.resolve())
                        or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".mp4"}
                        or not path.is_file()
                    ):
                        return json_response({"error": "Изображение недоступно"}, 404)
                    return web.FileResponse(path, headers=SECURITY)
                if action == "document" and method == "PUT":
                    company = allowed.get(int(request.query.get("company", 0)))
                    if company is None:
                        return json_response({"error": "Профиль недоступен"}, 404)
                    return await self.upload(request, session, company, user)
                if action == "attachment":
                    if (
                        active is None
                        or active.owner_id != user.id
                        or int(request.query.get("company", 0)) != active.id
                    ):
                        return json_response({"error": "Выберите свой профиль"}, 403)
                    return await self.upload_attachment(request, session, active)
                if action == "photo":
                    if active is None or active.owner_id != user.id:
                        return json_response({"error": "Выберите свой профиль"}, 403)
                    return await self.photo_action(request, session, active, user)
                if action == "post" and method == "PUT":
                    if active is None or active.owner_id != user.id:
                        return json_response({"error": "Выберите свой профиль"}, 403)
                    return await self.replace_photo(request, session, active, user)
                limit = 128_000 if action == "post" else 32_768
                if action == "chat" and method == "DELETE" and "company" in request.query:
                    data = {"company": int(request.query["company"])}
                elif request.content_length is None or request.content_length > limit:
                    return json_response({"error": "Слишком большой запрос"}, 413)
                else:
                    data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError("Ожидался объект")
                if (
                    method == "POST"
                    and action in {"profile", "document"}
                    and data.get("action") != "delete"
                ):
                    raise ValueError("Неизвестное действие")
                if action == "select":
                    selected = data.get("id")
                    if selected is not None and selected not in allowed:
                        return json_response({"error": "Профиль недоступен"}, 404)
                    selection.selected_id = selected
                elif action == "profiles":
                    name = str(data.get("name", "")).strip()[:255]
                    if not name:
                        raise ValueError("Укажите название")
                    company = await BusinessRepo(session).create(user.id, name)
                    company.settings = {"miniapp_private": True}
                    await self.edit_profile(session, company, user, data)
                    selection.selected_id = company.id
                    await session.commit()
                    return json_response({"ok": True, "id": company.id})
                else:
                    company = allowed.get(data.get("company"))
                    if company is None:
                        return json_response({"error": "Профиль недоступен"}, 404)
                    if (
                        action in {"profile", "document", "content", "website"}
                        and company.owner_id != user.id
                    ):
                        return json_response({"error": "Изменять профиль может владелец"}, 403)
                    if action == "website":
                        return await self.import_website(session, company, data)
                    if action in {"settings", "comment"}:
                        if selection.selected_id != company.id or company.owner_id != user.id:
                            return json_response({"error": "Выберите свой профиль"}, 403)
                        return await self.manage_company(session, company, user, action, data)
                    if action == "profile" and method == "PATCH":
                        await self.edit_profile(session, company, user, data)
                    elif action == "profile":
                        if (company.settings or {}).get("miniapp_default"):
                            return json_response(
                                {"error": "Профиль «Салават Стекло» нельзя удалить"}, 403
                            )
                        locked = await session.scalar(
                            select(Business)
                            .where(Business.id == company.id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                        locked.settings = {**(locked.settings or {}), "miniapp_archived": True}
                        locked.invite_code = None
                        await session.execute(
                            update(ProfileDocument)
                            .where(ProfileDocument.business_id == company.id)
                            .values(status="deleted")
                        )
                        await session.execute(
                            update(Post)
                            .where(
                                Post.business_id == company.id,
                                Post.status.in_([PostStatus.SCHEDULED, PostStatus.APPROVED]),
                            )
                            .values(status=PostStatus.DRAFT, scheduled_at=None)
                        )
                        if selection.selected_id == company.id:
                            selection.selected_id = default.id if default else None
                    elif action == "content":
                        await session.execute(
                            select(Business.id).where(Business.id == company.id).with_for_update()
                        )
                        await content_gallery.delete_content(
                            session, company.id, str(data.get("id", ""))
                        )
                    elif action == "document":
                        doc = await session.scalar(
                            select(ProfileDocument)
                            .where(
                                ProfileDocument.business_id == company.id,
                                ProfileDocument.id == data.get("id"),
                            )
                            .with_for_update()
                        )
                        if doc is None or doc.status == "deleted":
                            return json_response({"error": "Файл не найден"}, 404)
                        if method in {"DELETE", "POST"}:
                            doc.status = "deleted"
                        elif doc.status == "failed":
                            doc.status = "queued"
                            doc.error = None
                    elif action == "critique":
                        if selection.selected_id != company.id:
                            return json_response({"error": "Сначала выберите этот профиль"}, 409)
                        post = await PostRepo(session).get_for_business(int(data["id"]), company.id)
                        if post is None:
                            return json_response({"error": "Пост не найден"}, 404)
                        async with usage_scope(session, company.id):
                            result = await editorial.critique(
                                session,
                                self.container.research_llm or self.container.llm,
                                company,
                                post,
                                self.container.settings.tz,
                            )
                        await session.commit()
                        return json_response({"critique": result})
                    elif action in {"chat", "post", "plan"}:
                        if selection.selected_id != company.id:
                            return json_response({"error": "Сначала выберите этот профиль"}, 409)
                        if action == "plan":
                            if company.owner_id != user.id:
                                return json_response(
                                    {"error": "Планом управляет владелец профиля"}, 403
                                )
                            plan = await content_plan.get_plan_for_update(
                                session, company.id, int(data["id"])
                            )
                            if plan is None:
                                return json_response({"error": "План не найден"}, 404)
                            if data.get("version") != plan.updated_at.isoformat():
                                return json_response(
                                    {"error": "План изменился. Обновите экран."}, 409
                                )
                            if data.get("action") == "approve":
                                content_plan.approve_plan(plan)
                            elif data.get("action") == "revise":
                                changes = data.get("items")
                                if not isinstance(changes, list) or len(changes) > 12:
                                    raise ValueError("Ожидался список тем")
                                content_plan.revise_plan(
                                    plan,
                                    [
                                        content_plan.PlanCopy.model_validate(item)
                                        for item in changes
                                    ],
                                )
                            elif data.get("action") == "archive":
                                plan.status = ContentPlanStatus.ARCHIVED
                            else:
                                raise ValueError("Неизвестное действие с планом")
                            await session.commit()
                            return json_response({"ok": True})
                        if action == "chat" and method == "DELETE":
                            if any(key[:2] == (user.id, company.id) for key in self.chat_progress):
                                return json_response(
                                    {"error": "Дождитесь ответа и повторите."}, 409
                                )
                            pending = await session.get(PendingDraftOptions, (user.id, company.id))
                            if pending and pending.data.get("research_id"):
                                editorial.discard_post_research(
                                    (company.id, pending.data["research_id"])
                                )
                            await session.execute(
                                delete(PendingDraftOptions).filter_by(
                                    user_id=user.id, business_id=company.id
                                )
                            )
                            await session.execute(
                                delete(PendingPostAction).filter_by(
                                    user_id=user.id, business_id=company.id
                                )
                            )
                            await AgentMessageRepo(session).clear(user.id, company.id)
                            await session.commit()
                            for key in list(self.chat_results):
                                if key[:2] == (user.id, company.id):
                                    self.chat_results.pop(key, None)
                            return json_response({"ok": True})
                        async with usage_scope(session, company.id):
                            if action == "chat":
                                message = str(data.get("text", "")).strip()
                                option = data.get("draft_option")
                                if option is not None:
                                    if (
                                        not isinstance(option, dict)
                                        or not re.fullmatch(
                                            r"[0-9a-f]{12}", str(option.get("token", ""))
                                        )
                                        or not re.fullmatch(
                                            r"(?:[a-z_]{1,12}|[1-5])", str(option.get("choice", ""))
                                        )
                                    ):
                                        raise ValueError("Неверный вариант черновика")
                                elif not 0 < len(message) <= 4000:
                                    raise ValueError("Сообщение должно быть от 1 до 4000 символов")
                                return await self.chat(session, user, company, message, data)
                            outcome = await self.post_action(session, user, company, data)
                            if outcome is not None:
                                return json_response(outcome)
                await session.commit()
                return json_response({"ok": True})
        except approval.PostChangedError:
            return json_response(
                {"error": "Пост изменился. Обновите экран и проверьте черновик."}, 409
            )
        except BudgetExceededError as exc:
            return json_response({"error": str(exc)}, 429)
        except LLMError:
            return json_response(
                {"error": "ИИ или поиск временно недоступен. Попробуйте ещё раз."}, 503
            )
        except (ValueError, KeyError, TypeError) as exc:
            return json_response({"error": str(exc)[:300]}, 400)
        except Exception:
            log.exception("Ошибка Mini App в маршруте %s", action)
            return json_response({"error": "Операция не завершилась. Попробуйте ещё раз."}, 500)

    async def chat(self, session, user, company, message, data) -> web.Response:
        request_id = UUID(str(data["id"])) if "id" in data else uuid4()
        key = (user.id, company.id, request_id)
        option = data.get("draft_option")
        if key in self.chat_progress or key in self.chat_results:
            return json_response({"error": "Запрос уже выполняется"}, 409)
        if data.get("background") is True:
            if any(running[:2] == key[:2] for running in self.chat_progress):
                return json_response({"error": "Дождитесь ответа на предыдущее сообщение."}, 409)
            self.chat_progress[key] = "thinking"
            job = asyncio.create_task(self.run_chat_job(key, message, option))
            self.chat_jobs.add(job)
            job.add_done_callback(self.chat_jobs.discard)
            return json_response({"accepted": True}, 202)
        self.chat_progress[key] = "thinking"
        try:
            return json_response(
                await self.chat_reply(session, user, company, message, key, option)
            )
        finally:
            self.chat_progress.pop(key, None)

    async def run_chat_job(self, key, message, option):
        try:
            async with self.container.db.sessionmaker() as session:
                user = await session.get(User, key[0])
                company = await session.get(Business, key[1])
                if user is None or company is None:
                    raise ValueError("Профиль недоступен.")
                async with usage_scope(session, company.id):
                    result = await self.chat_reply(session, user, company, message, key, option)
        except (BudgetExceededError, ValueError) as exc:
            result = {"error": str(exc)}
        except LLMError:
            log.warning(
                "Чат: сбой модели для профиля %s на этапе %s", key[1], self.chat_progress.get(key)
            )
            result = {"error": "ИИ или поиск временно недоступен. Попробуйте ещё раз."}
        except Exception:
            log.exception("Ошибка фонового запроса чата для профиля %s", key[1])
            result = {"error": "Операция не завершилась. Проверьте черновики перед повтором."}
        finally:
            self.chat_progress.pop(key, None)
        self.chat_results[key] = result
        asyncio.get_running_loop().call_later(600, self.chat_results.pop, key, None)

    def agent_context(self, session, user, company, progress=None):
        return AgentContext(
            session,
            user,
            company,
            self.container.settings,
            self.container.llm,
            self.container.images,
            research_llm=self.container.research_llm,
            progress=progress,
            interactive_drafts=True,
            storage=self.container.storage,
            session_factory=self.container.db.sessionmaker,
        )

    async def chat_reply(self, session, user, company, message, key, option) -> dict:
        def progress(stage: str) -> None:
            self.chat_progress[key] = stage

        ctx = self.agent_context(session, user, company, progress)
        reply = (
            await choose_draft_option(ctx, option["token"], option["choice"])
            if option
            else await answer(ctx, message)
        )
        if not reply.get("text", "").strip():
            raise LLMError("Пустой ответ чата")
        await session.commit()
        return reply

    async def initialize_profiles(self, session, user, selection):
        if selection is None:
            selection = MiniUserState(user_id=user.id, selected_id=user.business_id)
            session.add(selection)
            await session.flush()
        default = await self.ensure_default_profile(session, user, selection)
        return selection, default

    async def ensure_default_profile(self, session, user, selection) -> Business:
        repo = BusinessRepo(session)
        owned = await repo.list_owned(user.id)
        default = next(
            (item for item in owned if (item.settings or {}).get("miniapp_default")), None
        )
        if default is None:
            default = next(
                (
                    item
                    for item in owned
                    if (item.settings or {}).get("default_placeholder")
                    or (
                        (item.settings or {}).get("miniapp_private")
                        and item.name.casefold().replace(" ", "") == "салаватстекло"
                    )
                ),
                None,
            )
        created = default is None
        if default is None:
            default = await repo.create(user.id, DEFAULT_PROFILE_NAME)
        settings = default.settings or {}
        template_path = self.container.settings.default_profile_file
        profile = default.profile or {}
        if template_path and (created or settings.get("default_placeholder")):
            template = business_profile.BusinessProfile.model_validate_json(
                Path(template_path).read_bytes()
            )
            profile = template.model_dump()
        if created or default.name != DEFAULT_PROFILE_NAME or profile != default.profile:
            await repo.set_profile(default, {**profile, "name": DEFAULT_PROFILE_NAME})
        if (
            created
            or not settings.get("miniapp_default")
            or settings.get("miniapp_archived")
            or (template_path and settings.get("default_placeholder"))
        ):
            default.settings = {
                **settings,
                "miniapp_default": True,
                "miniapp_private": True,
                "miniapp_archived": False,
                "default_placeholder": not template_path
                if created
                else bool(settings.get("default_placeholder") and not template_path),
            }

        if not settings.get("miniapp_default"):
            # Hide empty legacy bootstrap profiles. Carry over their Telegram connection.
            for legacy in owned:
                if (
                    legacy.id == default.id
                    or legacy.name != accounts.DEFAULT_COMPANY_NAME
                    or legacy.profile
                    or legacy.settings
                ):
                    continue
                for model in (Post, Source, ProfileDocument, ContentPlan, MediaFile):
                    if await session.scalar(
                        select(model.id).where(model.business_id == legacy.id).limit(1)
                    ):
                        break
                else:
                    joined = await session.scalar(
                        select(User.id)
                        .where(User.business_id == legacy.id, User.id != user.id)
                        .limit(1)
                    )
                    if joined:
                        continue
                    channels = ChannelRepo(session)
                    if not await channels.get_active(default.id, Platform.TELEGRAM):
                        channel = await channels.get_active(legacy.id, Platform.TELEGRAM)
                        if channel:
                            channel.business_id = default.id
                    legacy.settings = {"miniapp_archived": True}
                    if selection.selected_id == legacy.id:
                        selection.selected_id = default.id
                    if user.business_id == legacy.id:
                        user.business_id = default.id
        if selection.selected_id is None and (created or not selection.default_created):
            selection.selected_id = default.id
        selection.default_created = True
        return default

    async def channel_label(self, channel: Channel) -> str:
        cached = self.channel_labels.get(channel.external_id)
        if cached and cached[0] > monotonic():
            return cached[1]
        if channel.external_id.startswith("@"):
            return channel.external_id
        if channel.title.startswith("@"):
            return channel.title
        label = channel.title or "Канал Telegram"
        bot = self.container.bot
        if bot:
            try:
                async with asyncio.timeout(3):
                    info = await bot.get_chat(int(channel.external_id))
                label = f"@{info.username}" if info.username else info.title or label
            except (TelegramAPIError, TimeoutError, ValueError):
                pass
        self.channel_labels[channel.external_id] = (monotonic() + 300, label)
        return label

    async def channel_status(self, channel: Channel | None) -> dict[str, str | bool]:
        if channel is None:
            return {"ok": False, "message": "Канал не подключён"}
        bot = self.container.bot
        unavailable = {"ok": False, "message": "Не удалось проверить связь с Telegram"}
        if bot is None:
            return unavailable
        chat_id = (
            int(channel.external_id)
            if channel.external_id.lstrip("-").isdigit()
            else channel.external_id
        )
        try:
            async with asyncio.timeout(5):
                try:
                    chat = await bot.get_chat(chat_id)
                except TelegramBadRequest:
                    return {"ok": False, "message": "Канал не найден или недоступен боту"}
                if chat.type != "channel":
                    return {"ok": False, "message": "По этому адресу нет канала Telegram"}
                me = await bot.me()
                try:
                    member = await bot.get_chat_member(chat.id, me.id)
                except TelegramBadRequest:
                    return {"ok": False, "message": "У бота нет доступа к каналу"}
        except TelegramForbiddenError:
            return {"ok": False, "message": "У бота нет доступа к каналу"}
        except (TelegramAPIError, TimeoutError):
            return unavailable
        label = f"@{chat.username}" if chat.username else chat.title or "Канал Telegram"
        self.channel_labels[channel.external_id] = (monotonic() + 300, label)
        if member.status == ChatMemberStatus.CREATOR or (
            member.status == ChatMemberStatus.ADMINISTRATOR
            and getattr(member, "can_post_messages", False)
        ):
            return {"ok": True, "message": "Подключено", "label": label}
        return {"ok": False, "message": "Нет права публиковать", "label": label}

    async def vk_channel_status(self, channel: Channel | None) -> dict[str, str | bool]:
        if channel is None:
            return {"ok": False, "message": "Сообщество не подключено"}
        publisher = self.container.publishing.get_publisher(Platform.VK)
        if not isinstance(publisher, VKPublisher):
            return {"ok": False, "message": "Нет ключа подключения"}
        try:
            async with asyncio.timeout(5):
                check = await publisher.check_channel(channel)
        except TimeoutError:
            return {"ok": False, "message": "Не удалось проверить подключение"}
        return {"ok": check.ok, "message": "Подключено" if check.ok else check.message}

    async def report(self, session, company, days: int) -> web.Response:
        if not 1 <= days <= 90:
            raise ValueError("Укажите период от 1 до 90 дней")
        summary = await analytics.get_summary(
            session,
            company,
            days=days,
            tz=self.container.settings.tz,
        )
        insights = reports.rule_based_insights(summary)
        chart = await asyncio.to_thread(reports.chart_image, summary)
        output = BytesIO()
        chart.save(output, "PNG")
        return json_response(
            {
                "text": reports.Report(reports.render_text(summary, insights)).plain_text,
                "chart": b64encode(output.getvalue()).decode("ascii"),
                "subscribers": summary.subscribers,
                "posts": [
                    {
                        "id": post.id,
                        "topic": post.topic,
                        "views": post.views,
                        "forwards": post.forwards,
                    }
                    for post in summary.posts
                    if post.views is not None or post.forwards is not None
                ],
            }
        )

    async def import_website(self, session, company, data) -> web.Response:
        url = str(data.get("url", "")).strip()
        if len(url) > 2048:
            raise ValueError("Укажите адрес сайта")
        if url:
            try:
                url = public_url(url if "://" in url else "https://" + url)
            except WebsiteFetchError as exc:
                return json_response({"error": str(exc)}, 422)
        current = (company.settings or {}).get("miniapp_website", "")
        if url == current:
            return json_response({"ok": True})
        if not url:
            company.settings = {**(company.settings or {}), "miniapp_website": ""}
            await session.commit()
            return json_response({"ok": True})
        if self.container.storage is None:
            return json_response({"error": "Хранилище материалов не настроено"}, 503)
        if self.active_uploads >= 2:
            return json_response({"error": "Подождите окончания текущих загрузок"}, 503)
        self.active_uploads += 1
        try:
            async with asyncio.timeout(25):
                pages = await fetch_site_text(url, request_timeout=6)
            content = pages_to_text(pages)[:120_000]
            name = (pages[0].title or "Сайт компании").replace("/", " ").replace("\\", " ")
            name = "".join(char for char in name if ord(char) >= 32).strip()[:100]
            with stage_document(company.id) as path:
                await asyncio.to_thread(path.write_text, content, encoding="utf-8")
                await save_document(
                    session,
                    company,
                    f"{name or 'Сайт компании'}.md",
                    path,
                    storage=self.container.storage,
                )
            company.settings = {**(company.settings or {}), "miniapp_website": url}
            await session.commit()
        except WebsiteFetchError as exc:
            return json_response({"error": str(exc)}, 422)
        except TimeoutError:
            return json_response({"error": "Сайт не ответил вовремя"}, 504)
        except DocumentQuotaError:
            return json_response({"error": "В профиле не осталось места для материала"}, 413)
        finally:
            self.active_uploads -= 1
        return json_response({"ok": True})

    async def document_preview(self, session, company_id: int, document_id: int) -> web.Response:
        document = await session.scalar(
            select(ProfileDocument).where(
                ProfileDocument.id == document_id,
                ProfileDocument.business_id == company_id,
                ProfileDocument.status != "deleted",
                ProfileDocument.size <= DOCUMENT_PREVIEW_MAX_BYTES,
            )
        )
        storage = self.container.storage
        if document is None or storage is None or not has_document_preview(document):
            return json_response({"error": "Предпросмотр недоступен"}, 404)
        try:
            async with asyncio.timeout(6):
                with TemporaryDirectory(prefix="marketer-preview-") as tmp:
                    path = Path(tmp) / "image"
                    await storage.download(document.original_key, path)
                    preview = await asyncio.to_thread(
                        render_profile_image,
                        path,
                        Path(document.filename).suffix.lower(),
                        320,
                        "WEBP",
                    )
        except (
            BotoCoreError,
            ClientError,
            OSError,
            TimeoutError,
            Image.DecompressionBombError,
            ValueError,
            ParseError,
            DefusedXmlException,
        ):
            return json_response({"error": "Предпросмотр недоступен"}, 404)
        return web.Response(body=preview, content_type="image/webp", headers=SECURITY)

    async def metrics(self, session, company, query) -> web.Response:
        now = utcnow()
        tz = self.container.settings.tz
        earliest = await social_metrics.bounds(session, company.id)
        today = to_local(now, tz).date()
        first_day = to_local(earliest, tz).date() if earliest else None
        hours = int(query.get("hours", 24))
        if "from" in query or "to" in query:
            first, last = date.fromisoformat(query["from"]), date.fromisoformat(query["to"])
            if first_day is None or not first_day <= first <= last <= today:
                raise ValueError("Выберите даты, за которые собиралась аналитика")
            start = to_utc_naive(datetime.combine(first, time.min, tz))
            end = min(now, to_utc_naive(datetime.combine(last, time.max, tz)))
        elif hours in {24, 168, 720}:
            start, end = now - timedelta(hours=hours), now
        else:
            raise ValueError("Выберите 24 часа, 7 или 30 дней")
        post_id = int(query.get("post", 0)) or None
        value = query.get("platform", "all")
        if value not in {"all", "telegram", "vk"}:
            raise ValueError("Выберите Telegram или ВКонтакте")
        platform = Platform(value) if value != "all" else None
        payload = await social_metrics.series(session, company.id, start, end, post_id, platform)
        payload.update(
            critique=None,
            can_critique=False,
            start=social_metrics.timestamp(start),
            end=social_metrics.timestamp(end),
        )
        if post_id is not None:
            post = await PostRepo(session).get_for_business(post_id, company.id)
            if (
                post is None
                or post.status != PostStatus.PUBLISHED
                or (platform and post.platform != platform)
            ):
                return json_response({"error": "Пост не найден"}, 404)
            stored = await session.get(PostCritique, post.id)
            payload["critique"] = stored.result if stored else None
            payload["can_critique"] = bool(
                await session.scalar(
                    select(PostSnapshot.post_id)
                    .where(PostSnapshot.post_id == post.id, PostSnapshot.views.is_not(None))
                    .limit(1)
                )
            )
            baseline = await session.scalar(
                select(PostSnapshot)
                .where(
                    PostSnapshot.post_id == post.id,
                    PostSnapshot.hour < start,
                    PostSnapshot.views.is_not(None),
                    PostSnapshot.deleted.is_(False),
                )
                .order_by(PostSnapshot.hour.desc())
                .limit(1)
            )
            payload["baseline"] = (
                {"at": social_metrics.timestamp(baseline.hour), "views": baseline.views}
                if baseline
                else None
            )
        return json_response(
            {
                **payload,
                "min_date": first_day.isoformat() if first_day else None,
                "max_date": today.isoformat(),
                "timezone": str(tz),
            }
        )

    async def post_pool(self, session, company, before: int) -> web.Response:
        query = select(Post).where(Post.business_id == company.id)
        if before:
            query = query.where(Post.id < before)
        rows = list(await session.scalars(query.order_by(Post.id.desc()).limit(POST_LIMIT + 1)))
        return json_response(
            {
                "posts": [
                    {
                        "id": post.id,
                        "topic": post.topic,
                        "status": post.status,
                        "platform": post.platform,
                    }
                    for post in rows[:POST_LIMIT]
                ],
                "next": rows[POST_LIMIT - 1].id if len(rows) > POST_LIMIT else None,
            }
        )

    async def versions(self, session, post) -> web.Response:
        rows = (
            await session.execute(
                select(
                    PostRevision, RevisionProvenance, AgentMessage.content, AgentMessage.created_at
                )
                .outerjoin(RevisionProvenance, RevisionProvenance.revision_id == PostRevision.id)
                .outerjoin(
                    AgentMessage,
                    (AgentMessage.id == RevisionProvenance.request_id)
                    & (AgentMessage.business_id == post.business_id),
                )
                .where(PostRevision.post_id == post.id)
                .order_by(PostRevision.id)
            )
        ).all()
        albums = (
            {
                album.revision_id: album.items
                for album in await session.scalars(
                    select(RevisionAttachments).where(
                        RevisionAttachments.revision_id.in_(
                            [revision.id for revision, _, _, _ in rows]
                        )
                    )
                )
            }
            if rows
            else {}
        )
        decisions = {}
        for version_id, event, user_id in (
            await session.execute(
                select(RevisionDecision.revision_id, EventLog, RevisionDecision.user_id)
                .join(EventLog)
                .where(EventLog.post_id == post.id)
                .order_by(EventLog.id)
            )
        ).all():
            decisions.setdefault(version_id, []).append(
                {
                    "type": event.type,
                    "at": social_metrics.timestamp(event.created_at),
                    "user_id": user_id,
                }
            )
        timeline = []
        for version, meta, message, requested_at in rows:
            source = meta.source if meta else "legacy"
            request = message or (meta.request_text if meta else None)
            timeline.append(
                {
                    "at": social_metrics.timestamp(requested_at or version.created_at),
                    "type": "request"
                    if request and not version.instruction
                    else "revision"
                    if version.instruction
                    else source,
                    "data": request or version.instruction or "",
                    "version_id": version.id,
                }
            )
        for event in await session.scalars(
            select(EventLog)
            .where(
                EventLog.post_id == post.id,
                EventLog.business_id == post.business_id,
                EventLog.type.in_(
                    [
                        EventType.POST_SCHEDULED,
                        EventType.POST_REJECTED,
                        EventType.POST_PUBLISHED,
                        EventType.POST_FAILED,
                        EventType.POST_UNSCHEDULED,
                    ]
                ),
            )
            .order_by(EventLog.id)
        ):
            simulated = (event.payload or {}).get("dry_run")
            timeline.append(
                {
                    "at": social_metrics.timestamp(event.created_at),
                    "type": "simulation" if simulated else event.type,
                    "data": event.message
                    if event.type in {EventType.POST_SCHEDULED, EventType.POST_FAILED}
                    else "",
                }
            )
        timeline.sort(key=lambda event: event["at"])
        return json_response(
            {
                "topic": post.topic,
                "platform": post.platform,
                "plan_item_id": post.plan_item_id,
                "post": {
                    "id": post.id,
                    "platform": post.platform,
                    "text": post.text,
                    "version": post.updated_at.isoformat(),
                    "editable_live": post.platform == Platform.VK
                    and post.status == PostStatus.PUBLISHED
                    and self.container.settings.vk_is_business_account
                    and not (post.external_id or "").startswith("dry-run-"),
                },
                "timeline": timeline,
                "versions": [
                    {
                        "id": version.id,
                        "text": version.text,
                        "instruction": version.instruction,
                        "at": social_metrics.timestamp(version.created_at),
                        "source": meta.source if meta else "legacy",
                        "parent_id": meta.parent_id if meta else None,
                        "request": (message or meta.request_text) if meta else None,
                        "request_id": meta.request_id if meta else None,
                        "author_id": meta.author_id if meta else None,
                        "has_image": bool(meta and meta.image_path),
                        "image_source": meta.image_source if meta else None,
                        "image_model": meta.image_model if meta else None,
                        "image_prompt": meta.image_prompt if meta else None,
                        "media": self.preview_items(albums.get(version.id)),
                        "decisions": decisions.get(version.id, []),
                    }
                    for version, meta, message, _ in rows
                ],
            }
        )

    async def publication_history(self, session, company) -> web.Response:
        rows = await session.scalars(
            select(Post)
            .where(
                Post.business_id == company.id,
                Post.status.in_([PostStatus.PUBLISHED, PostStatus.REJECTED, PostStatus.SCHEDULED]),
            )
            .order_by(Post.id.desc())
            .limit(POST_LIMIT)
        )
        rows = list(rows)
        attachments = await self.attachment_index(session, [post.id for post in rows])
        return json_response(
            {
                "posts": [
                    {
                        "id": post.id,
                        "topic": post.topic,
                        "text": post.text,
                        "media": self.preview_items(attachments.get(post.id)),
                        "status": post.status,
                        "version": post.updated_at.isoformat(),
                        "platform": post.platform,
                        "published_at": post.published_at.replace(tzinfo=UTC).isoformat()
                        if post.published_at
                        else None,
                        "url": post.external_url,
                        "simulated": (post.external_id or "").startswith("dry-run-"),
                        "editable_live": post.platform == Platform.VK
                        and post.status == PostStatus.PUBLISHED
                        and self.container.settings.vk_is_business_account
                        and not (post.external_id or "").startswith("dry-run-"),
                        "has_image": bool(post.image_path),
                    }
                    for post in rows
                ]
            }
        )

    async def diagnostics(self, session, company) -> web.Response:
        from app.scheduler.setup import JOB_TITLES

        settings = self.container.settings
        channel = await ChannelRepo(session).get_active(company.id, Platform.TELEGRAM)
        publisher = self.container.publishing.get_publisher(Platform.TELEGRAM)
        check = await publisher.check_channel(channel) if channel and publisher else None
        scheduler = self.container.scheduler
        return json_response(
            {
                "channel": channel.external_id if channel else None,
                "channel_check": asdict(check) if check else None,
                "dry_run": self.container.publishing.dry_run,
                "counts": await PostRepo(session).count_by_status(company.id),
                "jobs": [
                    {
                        "name": JOB_TITLES.get(job.id, job.name),
                        "next": format_local(getattr(job, "next_run_time", None), settings.tz),
                    }
                    for job in scheduler.get_jobs()
                ]
                if scheduler
                else [],
            }
        )

    async def management_state(self, session, company) -> dict:
        settings = self.container.settings
        bot_user = (
            await self.container.bot.me() if self.container.bot and company.invite_code else None
        )
        return {
            "auto_reply": comments.auto_reply_enabled(company),
            "budget": get_budget(company),
            "spend": await month_spend(session, company.id, settings.tz),
            "photos": [
                {"id": photo.id, "caption": photo.caption}
                for photo in await MediaRepo(session).list_for_business(company.id, limit=30)
            ],
            "invite": accounts.invite_link(bot_user.username, company)
            if bot_user and bot_user.username
            else None,
            "clients": [
                {
                    "id": item.id,
                    "name": item.name or str(item.telegram_id),
                    "active": item.is_active,
                }
                for item in await UserRepo(session).list_by_business(company.id)
                if item.id != company.owner_id and not accounts.is_admin(item)
            ],
            "comments": [
                {
                    "id": item.id,
                    "author": item.author_name,
                    "text": item.text,
                    "reply": item.reply_text,
                    "status": item.status,
                }
                for item in await CommentRepo(session).list_for_business(company.id, limit=30)
            ],
            "events": [
                {
                    "type": item.type,
                    "text": item.message,
                    "date": item.created_at.replace(tzinfo=UTC).isoformat(),
                }
                for item in await EventLogRepo(session).list_recent(company.id)
            ],
        }

    async def upload_attachment(self, request, session, company) -> web.Response:
        filename = unquote(request.headers.get("X-Filename", ""))
        if Path(filename).suffix.lower() != ".mp4":
            data = await self.read_photo(request)
            if isinstance(data, web.Response):
                return data
            item, _ = await media.save_photo(session, company, data, caption=Path(filename).name)
        else:
            if request.content_type != "application/octet-stream" or not (
                0 < (request.content_length or 0) <= 50_000_000
            ):
                raise ValueError("Выберите MP4 размером до 50 МБ")
            folder = IMAGES_DIR / "post-videos" / str(company.id)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (uuid4().hex + ".mp4")
            try:
                async with asyncio.timeout(90):
                    try:
                        signature = await request.content.readexactly(12)
                    except asyncio.IncompleteReadError:
                        raise ValueError("Файл должен быть видео MP4") from None
                    if len(signature) < 12 or signature[4:8] != b"ftyp":
                        raise ValueError("Файл должен быть видео MP4")
                    with path.open("xb") as handle:
                        handle.write(signature)
                        async for chunk in request.content.iter_chunked(262144):
                            await asyncio.to_thread(handle.write, chunk)
                item = await MediaRepo(session).add(
                    MediaFile(
                        business_id=company.id, path=str(path), caption=Path(filename).name[:255]
                    )
                )
                await session.commit()
                return json_response({"id": item.id})
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        await session.commit()
        return json_response({"id": item.id})

    async def attachment_index(self, session, post_ids):
        if not post_ids:
            return {}
        rows = await session.scalars(
            select(PostAttachments).where(PostAttachments.post_id.in_(post_ids))
        )
        return {row.post_id: row.items for row in rows}

    @staticmethod
    def preview_items(items):
        return (
            [{"type": item["type"], "name": item["name"]} for item in items]
            if items is not None
            else None
        )

    async def upload_content(self, request, session, company) -> web.Response:
        """Добавляет картинку в раздел «Контент»: только изображения."""
        filename = unquote(request.headers.get("X-Filename", ""))
        data = await self.read_photo(request, keep_original=True)
        if isinstance(data, web.Response):
            return data
        try:
            item = await content_gallery.add_image(session, company, filename, bytes(data))
        except content_gallery.ContentError as exc:
            return json_response({"error": str(exc)}, 422)
        await session.commit()
        return json_response({"ok": True, "id": item.id})

    async def read_photo(self, request, *, keep_original: bool = False) -> bytes | web.Response:
        if request.content_type != "application/octet-stream":
            return json_response({"error": "Ожидалось изображение"}, 415)
        if (
            request.content_length is None
            or not 0 < request.content_length <= media.MAX_PHOTO_BYTES
        ):
            return json_response({"error": "Размер изображения должен быть до 20 МБ"}, 413)
        data = bytearray()
        async with asyncio.timeout(30):
            async for block in request.content.iter_chunked(64 * 1024):
                data.extend(block)
                if len(data) > media.MAX_PHOTO_BYTES:
                    return json_response({"error": "Размер изображения должен быть до 20 МБ"}, 413)
        if keep_original:
            return bytes(data)
        try:
            return await asyncio.to_thread(self.photo_as_jpeg, data)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
            return json_response({"error": "Не удалось прочитать изображение"}, 415)

    async def photo_action(self, request, session, company, user) -> web.Response:
        if request.method == "PUT":
            encoded = await self.read_photo(request)
            if isinstance(encoded, web.Response):
                return encoded
            photo, _ = await media.save_photo(session, company, encoded)
            await session.commit()
            return json_response({"id": photo.id})
        photo = await MediaRepo(session).get_for_business(int(request.query["id"]), company.id)
        if photo is None:
            return json_response({"error": "Фото не найдено"}, 404)
        path = Path(photo.path).resolve()
        if not path.is_relative_to(IMAGES_DIR.resolve()) or not path.is_file():
            return json_response({"error": "Фото недоступно"}, 404)
        if request.method == "GET":
            return web.FileResponse(path, headers=SECURITY)
        if request.content_length is None or request.content_length > 4096:
            return json_response({"error": "Слишком большой запрос"}, 413)
        data = await request.json()
        topic = str(data.get("topic", "")).strip()
        if not 0 < len(topic) <= 512:
            raise ValueError("Укажите тему до 512 символов")
        async with usage_scope(session, company.id):
            post = await media.create_post_with_photo(
                session,
                self.container.llm,
                self.container.images,
                company,
                photo,
                topic,
                source=RevisionSource("photo", author_id=user.id, request_text=topic),
            )
            await approval.submit_for_approval(session, post, None, None)
        return json_response({"id": post.id})

    async def replace_photo(self, request, session, company, user) -> web.Response:
        encoded = await self.read_photo(request)
        if isinstance(encoded, web.Response):
            return encoded
        post = await PostRepo(session).get_for_business(int(request.query["id"]), company.id)
        if post is None:
            return json_response({"error": "Пост не найден"}, 404)
        async with self.container.publishing.post_lock(post.id):
            await session.refresh(post)
            if post.updated_at.isoformat() != request.query.get("version"):
                raise approval.PostChangedError
            if post.status in {PostStatus.PUBLISHED, PostStatus.REJECTED}:
                raise ValueError("Этот пост нельзя изменить")
            photo, _ = await media.save_photo(session, company, encoded)
            await media.replace_post_photo(session, post, photo, author_id=user.id)
            await approval.submit_for_approval(session, post, None, None)
        return json_response({"ok": True})

    @staticmethod
    def photo_as_jpeg(data: bytearray) -> bytes:
        with Image.open(BytesIO(data)) as image:
            if (
                image.format not in {"JPEG", "PNG", "WEBP"}
                or image.width * image.height > 25_000_000
            ):
                raise UnidentifiedImageError("Используйте JPEG, PNG или WebP")
            image.thumbnail((2400, 2400))
            output = BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=92)
            return output.getvalue()

    async def manage_company(self, session, company, user, action, data) -> web.Response:
        if action == "comment":
            item = await session.scalar(
                select(Comment)
                .where(Comment.business_id == company.id, Comment.id == int(data["id"]))
                .with_for_update()
            )
            if item is None:
                return json_response({"error": "Комментарий не найден"}, 404)
            if data.get("action") == "skip":
                await comments.skip(session, item)
            elif data.get("action") == "send" and self.container.bot:
                from app.bot.handlers.comments import BotCommentReplier

                await comments.send_reply(
                    session,
                    item,
                    str(data.get("text", "")),
                    BotCommentReplier(self.container.bot),
                    user=user,
                )
            else:
                raise ValueError("Действие с комментарием недоступно")
        elif data.get("action") == "auto_reply":
            enabled = data.get("enabled")
            if type(enabled) is not bool:
                raise ValueError("Укажите, включать ли автоответы")
            comments.set_auto_reply(company, enabled)
        elif data.get("action") == "budget":
            value = data.get("value")
            if value is not None and (
                type(value) not in {int, float} or not 0 < value <= 1_000_000
            ):
                raise ValueError("Укажите лимит от 0 до 1 000 000 ₽ или оставьте поле пустым")
            set_budget(company, value)
        elif data.get("action") == "invite":
            if not accounts.is_admin(user):
                raise ValueError("Приглашения доступны администратору")
            await accounts.regenerate_invite(session, user, company)
        elif data.get("action") == "client_access":
            active = data.get("active")
            if type(active) is not bool:
                raise ValueError("Укажите состояние доступа")
            await accounts.set_client_access(session, user, company, int(data["id"]), active)
        else:
            raise ValueError("Неизвестная настройка")
        await session.commit()
        return json_response({"ok": True})

    async def state(self, session, identity, user, companies, active) -> web.Response:
        items = []
        materials = []
        content = []
        plans = []
        proposal = None
        management = None
        dashboard = None
        if active:
            dashboard = await self.dashboard(session, active.id)
            if active.owner_id == user.id:
                management = await self.management_state(session, active)
                content = [
                    {
                        "id": image.id,
                        "number": image.number,
                        "name": image.label,
                        "kind": image.kind,
                        "size": image.size,
                        "preview": image.size <= DOCUMENT_PREVIEW_MAX_BYTES,
                    }
                    for image in await content_gallery.list_content(session, active.id)
                ]
            pending = await session.get(PendingPostAction, (user.id, active.id))
            if pending is not None:
                post = await PostRepo(session).get_for_business(pending.proposal["id"], active.id)
                if post is not None and pending.proposal["version"] == post.updated_at.isoformat():
                    proposal = {**pending.proposal, "topic": post.topic}
            plans = [
                {
                    "id": plan.id,
                    "title": plan.title,
                    "status": plan.status,
                    "version": plan.updated_at.isoformat(),
                    "items": [
                        {
                            "topic": item.topic,
                            "rubric": item.rubric,
                            "when": item.planned_at.replace(tzinfo=UTC).isoformat()
                            if item.planned_at
                            else None,
                            "status": item.status,
                        }
                        for item in plan.items
                    ],
                }
                for plan in await content_plan.recent_plans(session, active.id)
            ]
            actionable = {
                PostStatus.DRAFT,
                PostStatus.PENDING_APPROVAL,
                PostStatus.APPROVED,
                PostStatus.SCHEDULED,
                PostStatus.FAILED,
            }
            results = await session.execute(
                select(Post, ContentPlanItem.planned_at)
                .outerjoin(ContentPlanItem, Post.plan_item_id == ContentPlanItem.id)
                .where(Post.business_id == active.id, Post.status.in_(actionable))
                .order_by(Post.id.desc())
                .limit(POST_LIMIT)
            )
            results = results.all()
            attachments = await self.attachment_index(session, [post.id for post, _ in results])
            for post, suggested_at in results:
                items.append(self.post_preview(post, attachments.get(post.id), suggested_at))
            docs = await session.scalars(
                select(ProfileDocument)
                .where(
                    ProfileDocument.business_id == active.id, ProfileDocument.status != "deleted"
                )
                .order_by(ProfileDocument.id)
            )
            materials = [
                {
                    "id": doc.id,
                    "number": document_image_number(doc),
                    "name": doc.filename,
                    "status": doc.status,
                    "size": doc.size,
                    "error": doc.error,
                    "preview": has_document_preview(doc),
                }
                for doc in docs
            ]
        timeline = await self.chat_state(session, user, active, items) if active else {}
        profiles = []
        for company in sorted(
            companies, key=lambda item: not (item.settings or {}).get("miniapp_default")
        ):
            channel = await ChannelRepo(session).get_active(company.id, Platform.TELEGRAM)
            vk = await ChannelRepo(session).get_active(company.id, Platform.VK)
            profiles.append(
                {
                    "id": company.id,
                    "name": company.name,
                    "details": business_profile.get_profile(company).model_dump(),
                    "editable": company.owner_id == user.id,
                    "invite_enabled": accounts.is_admin(user) and company.owner_id == user.id,
                    "channel": await self.channel_label(channel) if channel else "",
                    "website": (company.settings or {}).get("miniapp_website", ""),
                    "vk_channel": (vk.title or vk.external_id) if vk else "",
                    "vk_id": vk.external_id if vk else "",
                    "vk_enabled": self.container.vk_clients is not None,
                    "vk_business_account": self.container.settings.vk_is_business_account,
                    "is_default": bool((company.settings or {}).get("miniapp_default")),
                }
            )
        return json_response(
            {
                "user": identity.model_dump(),
                "active": active.id if active else None,
                "profiles": profiles,
                "posts": items,
                "plans": plans,
                "proposal": proposal,
                "management": management,
                "history": timeline.get("history", []),
                "chat_posts": timeline.get("chat_posts", []),
                "documents": materials,
                "content": content,
                "content_bytes_available": (
                    await content_gallery.available_bytes(session, active.id)
                    if active and active.owner_id == user.id
                    else 0
                ),
                "draft_question": timeline.get("draft_question"),
                "profile_chars_limit": business_profile.PROFILE_MAX_CHARS,
                "document_bytes_available": await remaining_bytes(session, active.id)
                if active
                else None,
                "storage": self.container.storage is not None,
                "stories": self.stories(),
                "dashboard": dashboard,
                "timezone": self.container.settings.timezone,
            }
        )

    async def chat_state(self, session, user, active, items=()):
        history = []
        messages = await AgentMessageRepo(session).last_n(user.id, 50, business_id=active.id)
        draft_dates = {}
        for message in messages:
            if not message.content or message.tool_calls or message.role == MessageRole.TOOL:
                continue
            entry = {
                "id": message.id,
                "text": message.content,
                "me": message.role == MessageRole.USER,
            }
            name = message.name or ""
            if message.role == MessageRole.ASSISTANT:
                if name.startswith("question:"):
                    entry["question_token"] = name.removeprefix("question:")
                elif name.startswith("draft:"):
                    draft_ref, _, when = name.removeprefix("draft:").partition("|")
                    if draft_ref.isdecimal():
                        entry.update(draft_id=int(draft_ref), draft_when=when or None)
                        if when:
                            draft_dates[int(draft_ref)] = when
                elif legacy := re.match(r"Черновик №(\d+) готов", message.content):
                    entry["draft_id"] = int(legacy[1])
            history.append(entry)
        for item in items:
            if item["suggested_at"] is None and item["id"] in draft_dates:
                item["suggested_at"] = draft_dates[item["id"]]
        missing = {entry["draft_id"] for entry in history if "draft_id" in entry}
        missing.difference_update(item["id"] for item in items)
        related = (
            list(
                await session.scalars(
                    select(Post).where(Post.business_id == active.id, Post.id.in_(missing))
                )
            )
            if missing
            else []
        )
        albums = await self.attachment_index(session, [post.id for post in related])
        return {
            "company": active.id,
            "history": history,
            "chat_posts": [self.post_preview(post, albums.get(post.id)) for post in related],
            "draft_question": await active_draft_options(self.agent_context(session, user, active)),
        }

    def post_preview(self, post: Post, attachments, suggested_at: datetime | None = None) -> dict:
        return {
            "id": post.id,
            "topic": post.topic,
            "text": post.text,
            "status": post.status,
            "platform": post.platform,
            "version": post.updated_at.isoformat(),
            "media": self.preview_items(attachments),
            "has_image": bool(post.image_path),
            "when": social_metrics.timestamp(post.scheduled_at) if post.scheduled_at else None,
            "suggested_at": social_metrics.timestamp(suggested_at) if suggested_at else None,
            "simulated": (post.external_id or "").startswith("dry-run-"),
        }

    async def dashboard(self, session, company_id: int) -> dict:
        tz = self.container.settings.tz
        today = to_local(utcnow(), tz).date()
        first = today - timedelta(days=6)
        start = to_utc_naive(datetime.combine(first, time.min, tz))
        published = (Post.business_id == company_id, Post.status == PostStatus.PUBLISHED)
        count = await session.scalar(select(func.count(Post.id)).where(*published))
        recent = await session.scalars(
            select(Post.published_at).where(*published, Post.published_at >= start)
        )
        publication_series = [0] * 7
        for at in recent:
            index = (to_local(at, tz).date() - first).days
            if 0 <= index < 7:
                publication_series[index] += 1

        points = (
            await session.execute(
                select(PostSnapshot.post_id, PostSnapshot.hour, PostSnapshot.views)
                .join(Post)
                .where(
                    *published,
                    PostSnapshot.hour >= start - timedelta(hours=1),
                    PostSnapshot.views.is_not(None),
                    PostSnapshot.deleted.is_(False),
                )
                .order_by(PostSnapshot.post_id, PostSnapshot.hour)
            )
        ).all()
        views_series = [0] * 7
        previous = None
        for post_id, at, views in points:
            index = (to_local(at, tz).date() - first).days
            if (
                0 <= index < 7
                and previous
                and previous[0] == post_id
                and at - previous[1] == timedelta(hours=1)
            ):
                views_series[index] += max(0, views - previous[2])
            previous = (post_id, at, views)

        latest_subscribers = (
            select(ChannelSnapshot.subscribers)
            .join(Channel)
            .where(
                Channel.business_id == company_id,
                Channel.is_active.is_(True),
                Channel.platform == Platform.TELEGRAM,
            )
            .order_by(ChannelSnapshot.hour.desc())
            .limit(1)
            .scalar_subquery()
        )
        subscribers = await session.scalar(select(func.coalesce(latest_subscribers, 0)))
        return {
            "published": count,
            "subscribers": subscribers,
            "views_7d": sum(views_series),
            "views_series": views_series,
            "publication_series": publication_series,
        }

    def stories(self) -> list[dict[str, str]]:
        path = self.container.settings.miniapp_stories_file
        if path:
            entries = json.loads(Path(path).read_text(encoding="utf-8"))
            return [
                {
                    "title": str(item["title"])[:80],
                    "text": str(item["text"])[:4000],
                    **(
                        {"image": item["image"]}
                        if isinstance(item.get("image"), str)
                        and item["image"].startswith("https://")
                        else {}
                    ),
                }
                for item in entries[:20]
            ]
        return [
            {
                "title": "Обновление",
                "image": "/miniapp/glass.webp",
                "text": "Профили компаний, согласование и расписание теперь доступны в одном интерфейсе.",
            }
        ]

    async def edit_profile(self, session, company, user, data) -> None:
        fields = {
            key: str(value)[:2000]
            for key, value in data.items()
            if key in business_profile.PROFILE_FIELD_TITLES and key != "tone"
        }
        if (company.settings or {}).get("miniapp_default"):
            fields.pop("name", None)
        if "name" in fields and not fields["name"].strip():
            raise ValueError("Укажите название")
        merged = {**business_profile.get_profile(company).model_dump(), **fields}
        used = business_profile.profile_chars(merged)
        if used > business_profile.PROFILE_MAX_CHARS:
            raise ValueError(
                f"Описание компании не больше {business_profile.PROFILE_MAX_CHARS} символов "
                f"суммарно по всем полям (сейчас {used}). Сократите текст."
            )
        await business_profile.update_profile_fields(session, company, fields)
        if (company.settings or {}).get("default_placeholder"):
            company.settings = {**company.settings, "default_placeholder": False}
        if "vk_channel" in data:
            raw = str(data["vk_channel"]).strip()
            current = await ChannelRepo(session).get_active(company.id, Platform.VK)
            if raw and not (current and raw in (current.external_id, current.title)):
                if not accounts.is_admin(user):
                    raise ValueError("Подключение сообщества ВКонтакте настраивает администратор")
                publisher = self.container.publishing.get_publisher(Platform.VK)
                await vk_channel.connect(
                    session, publisher if isinstance(publisher, VKPublisher) else None, company, raw
                )
            elif not raw and current:
                await vk_channel.disconnect(session, company)
        if data.get("channel"):
            raw_channel = str(data["channel"]).strip()
            channels = ChannelRepo(session)
            existing = await channels.get_active(company.id, Platform.TELEGRAM)
            if existing and raw_channel in {
                existing.external_id,
                await self.channel_label(existing),
            }:
                return
            channel = accounts.normalize_channel_id(raw_channel)
            if existing is None or existing.external_id != channel:
                bot = self.container.bot
                if bot is None:
                    raise ValueError("Проверка канала недоступна")
                try:
                    chat_id = int(channel) if channel.startswith("-100") else channel
                    resolved = await bot.get_chat(chat_id)
                    if resolved.type != "channel":
                        raise ValueError("Укажите канал Telegram")
                    me = await bot.me()
                    member = await bot.get_chat_member(chat_id, user.telegram_id)
                    bot_member = await bot.get_chat_member(chat_id, me.id)
                except TelegramAPIError:
                    raise ValueError("Добавьте бота в канал и проверьте его адрес") from None
                if member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
                    raise ValueError("Подключить канал может его администратор")
                if bot_member.status != ChatMemberStatus.CREATOR and not getattr(
                    bot_member, "can_post_messages", False
                ):
                    raise ValueError("Дайте боту право публиковать сообщения в канале")
                await channels.upsert(
                    company.id,
                    Platform.TELEGRAM,
                    str(resolved.id),
                    f"@{resolved.username}" if resolved.username else resolved.title or "",
                )
        elif "channel" in data:
            current = await ChannelRepo(session).get_active(company.id, Platform.TELEGRAM)
            if current is not None:
                current.is_active = False

    async def upload(self, request, session, company, user) -> web.Response:
        if self.container.storage is None:
            return json_response({"error": "Хранилище материалов не настроено"}, 503)
        if company.owner_id != user.id:
            return json_response({"error": "Файлы добавляет владелец"}, 403)
        from app.services.document_uploads import PROFILE_DOCUMENT_BYTES

        size = request.content_length
        if request.content_type != "application/octet-stream" or request.headers.get(
            "Content-Encoding"
        ):
            return json_response({"error": "Ожидался файл"}, 415)
        if size is not None and not 0 < size <= PROFILE_DOCUMENT_BYTES:
            return json_response({"error": "Допустимый размер до 100 МБ"}, 413)
        name = Path(unquote(request.headers.get("X-Filename", "")).replace("\\", "/")).name
        if (
            len(name) > 255
            or any(ord(c) < 32 for c in name)
            or Path(name).suffix.lower() not in PROFILE_FILE_EXTENSIONS
        ):
            return json_response(
                {"error": FORMAT_ERROR},
                415,
            )
        if self.active_uploads >= 2:
            return json_response({"error": "Подождите окончания текущих загрузок"}, 503)
        self.active_uploads += 1
        try:
            with stage_document(company.id) as path:
                count = 0
                async with asyncio.timeout(UPLOAD_TIMEOUT):
                    with path.open("wb") as file:
                        async for block in request.content.iter_chunked(262144):
                            count += len(block)
                            if count > PROFILE_DOCUMENT_BYTES:
                                return json_response({"error": "Файл превышает 100 МБ"}, 413)
                            file.write(block)
                await asyncio.to_thread(validate_profile_file, name, path)
                await save_document(session, company, name, path, storage=self.container.storage)
        except ProfileFileFormatError as exc:
            return json_response({"error": str(exc)}, 415)
        except ImageLimitError as exc:
            return json_response({"error": str(exc)}, 413)
        except DocumentQuotaError:
            return json_response({"error": "Файл не помещается в общую квоту 100 МБ"}, 413)
        except TimeoutError:
            return json_response({"error": "Время загрузки истекло"}, 408)
        finally:
            self.active_uploads -= 1
        return json_response({"ok": True})

    async def post_action(self, session, user, company, data) -> dict | None:
        post = await PostRepo(session).get_for_business(int(data["id"]), company.id)
        if post is None:
            raise ValueError("Пост не найден")
        action = data["action"]
        publishing = self.container.publishing
        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise approval.PostChangedError
        if action in {"publish", "approve"}:
            if action == "approve" and data.get("when"):
                action = "schedule"
            else:
                if publishing.dry_run and action == "approve":
                    raise ValueError("Включён тестовый режим. Пост в канал не отправлен.")
                result = await approval.approve_and_publish(
                    session,
                    post,
                    user,
                    publishing,
                    expected_version=version,
                    allow_draft=action == "approve",
                )
                if not result.ok:
                    raise ValueError(result.error or "Публикация не состоялась")
                return {
                    "ok": True,
                    "simulated": bool(result.result and result.result.dry_run),
                    "url": result.result.url if result.result else None,
                    **(
                        {"note": result.result.note} if result.result and result.result.note else {}
                    ),
                }
        async with publishing.post_lock(post.id):
            await session.refresh(post)
            if post.updated_at.isoformat() != version:
                raise approval.PostChangedError
            if action == "copy_to_vk":
                if post.platform != Platform.TELEGRAM or post.status == PostStatus.REJECTED:
                    raise ValueError("Этот пост недоступен для копирования из Telegram")
                if await ChannelRepo(session).get_active(company.id, Platform.VK) is None:
                    raise ValueError("Сначала подключите сообщество ВКонтакте")
                pending = await session.get(PendingPostAction, (user.id, company.id))
                if (
                    pending
                    and pending.proposal.get("action") == action
                    and pending.proposal.get("id") == post.id
                ):
                    await session.delete(pending)
                copy = await posts.clone_for_platform(session, post, Platform.VK, author_id=user.id)
                await approval.submit_for_approval(session, copy, None, None)
            elif action == "edit_published" and post.platform == Platform.VK:
                if company.owner_id != user.id:
                    raise ValueError("Редактировать публикацию может владелец профиля")
                try:
                    await publishing.edit_published(
                        session, post, str(data.get("text", "")), author_id=user.id
                    )
                except PublishError as exc:
                    raise ValueError(str(exc)) from None
            elif action == "submit" and post.status in {PostStatus.DRAFT, PostStatus.FAILED}:
                if post.status == PostStatus.FAILED:
                    posts.transition(post, PostStatus.DRAFT)
                await approval.submit_for_approval(session, post, None, None)
            elif action == "schedule" and post.status in {
                PostStatus.DRAFT,
                PostStatus.PENDING_APPROVAL,
                PostStatus.APPROVED,
                PostStatus.SCHEDULED,
                PostStatus.FAILED,
            }:
                when = datetime.fromisoformat(data["when"])
                if when.tzinfo is None:
                    raise ValueError("Укажите время публикации")
                if when <= datetime.now(UTC):
                    raise ValueError("Время публикации уже прошло")
                if post.status == PostStatus.DRAFT:
                    posts.transition(post, PostStatus.PENDING_APPROVAL)
                if post.status == PostStatus.FAILED:
                    await approval.approve(session, post, user)
                await approval.schedule(session, post, when, user)
            elif action == "reject" and post.status in {
                PostStatus.DRAFT,
                PostStatus.PENDING_APPROVAL,
                PostStatus.APPROVED,
                PostStatus.FAILED,
            }:
                if post.status == PostStatus.APPROVED:
                    posts.transition(post, PostStatus.DRAFT)
                await approval.reject(session, post, user)
            elif action == "cancel" and post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED}:
                post.scheduled_at = None
                posts.transition(post, PostStatus.DRAFT)
                await EventLogRepo(session).log(
                    EventType.POST_UNSCHEDULED, "", business_id=company.id, post_id=post.id
                )
                await approval.submit_for_approval(session, post, None, None)
            elif (
                action == "remove_photo"
                and post.image_path
                and (
                    post.status == PostStatus.DRAFT
                    or posts.can_transition(post.status, PostStatus.DRAFT)
                )
            ):
                if post.status != PostStatus.DRAFT:
                    posts.transition(post, PostStatus.DRAFT)
                album = await session.get(PostAttachments, post.id)
                if album is not None:
                    await session.delete(album)
                    await session.flush()
                post.image_path = None
                post.scheduled_at = None
                post.error = None
                await save_revision(
                    session,
                    post,
                    RevisionSource("photo", author_id=user.id),
                    instruction="Удаление изображения",
                    image_change=True,
                )
                await approval.submit_for_approval(session, post, None, None)
            elif action == "edit_text" and post.status in {
                PostStatus.DRAFT,
                PostStatus.PENDING_APPROVAL,
                PostStatus.APPROVED,
                PostStatus.SCHEDULED,
                PostStatus.FAILED,
            }:
                value = str(data["text"]).strip()
                album = await session.get(PostAttachments, post.id)
                media_ids = data.get("media")
                new_items = album.items if album else None
                if media_ids is not None:
                    if company.owner_id != user.id:
                        raise ValueError("Вложения меняет владелец профиля")
                    if not isinstance(media_ids, list) or len(media_ids) > 10:
                        raise ValueError("Добавьте до 10 вложений")
                    new_items = []
                    existing = (
                        album.items
                        if album
                        else (
                            [{"path": post.image_path, "type": "image", "name": "Фото"}]
                            if post.image_path
                            else []
                        )
                    )
                    for identifier in media_ids:
                        if type(identifier) is int and identifier > 0:
                            asset = await MediaRepo(session).get_for_business(
                                identifier, company.id
                            )
                            if not asset:
                                raise ValueError("Вложение недоступно")
                            new_items.append(
                                {
                                    "path": asset.path,
                                    "name": asset.caption or "Вложение",
                                    "type": "video"
                                    if Path(asset.path).suffix == ".mp4"
                                    else "image",
                                }
                            )
                        elif (
                            isinstance(identifier, str)
                            and identifier.isdecimal()
                            and int(identifier) < len(existing)
                        ):
                            new_items.append(existing[int(identifier)])
                        else:
                            raise ValueError("Вложение недоступно")
                has_media = bool(new_items) if new_items is not None else bool(post.image_path)
                platform = Platform(post.platform)
                if (
                    new_items
                    and any(item["type"] == "document" for item in new_items)
                    and any(item["type"] != "document" for item in new_items)
                ):
                    raise ValueError("Telegram принимает PDF и фотографии в отдельных публикациях")
                hard_limit = posts.PLATFORM_HARD_LIMITS[platform]
                limit = (
                    posts.TELEGRAM_CAPTION_LIMIT
                    if platform == Platform.TELEGRAM and has_media
                    else hard_limit
                )
                if len(value) > 32_000:
                    raise ValueError("Текст поста слишком длинный.")
                text_length = format_text(value)[1] if platform == Platform.TELEGRAM else len(value)
                rich = formatted_post(value) if platform == Platform.TELEGRAM else None
                raw_limit = 32_000 if rich is not None else hard_limit
                if (
                    (not text_length and not has_media)
                    or text_length > limit
                    or len(value) > raw_limit
                ):
                    raise ValueError(
                        f"Текст должен уложиться в {limit} символов. Посту нужен текст или вложение."
                    )
                if post.text != value or media_ids is not None:
                    if post.status != PostStatus.DRAFT:
                        posts.transition(post, PostStatus.DRAFT)
                    post.text = value
                    post.scheduled_at = None
                    post.error = None
                    if media_ids is not None:
                        await session.merge(PostAttachments(post_id=post.id, items=new_items))
                        post.image_path = None
                        await session.flush()
                    await save_revision(
                        session,
                        post,
                        RevisionSource("manual", author_id=user.id),
                        instruction="Ручная правка текста и вложений"
                        if media_ids is not None
                        else "Ручная правка текста",
                        image_change=media_ids is not None,
                    )
                    await approval.submit_for_approval(session, post, None, None)
            elif action == "revise" and post.status in {
                PostStatus.DRAFT,
                PostStatus.PENDING_APPROVAL,
                PostStatus.APPROVED,
                PostStatus.SCHEDULED,
                PostStatus.FAILED,
            }:
                instruction = str(data["instruction"]).strip()
                if not 0 < len(instruction) <= 2000:
                    raise ValueError("Введите правку")
                await posts.revise(
                    session,
                    self.container.llm,
                    company,
                    post,
                    instruction,
                    source=RevisionSource("manual", author_id=user.id, request_text=instruction),
                )
                await approval.submit_for_approval(session, post, None, None)
            else:
                raise ValueError("Действие недоступно для этого статуса")
            await session.commit()
