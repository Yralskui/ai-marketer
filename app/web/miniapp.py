"""Telegram Mini App: профили, материалы, расписание и чат."""

from __future__ import annotations

import asyncio
import json
import logging
from base64 import b64encode
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, text, update

from app.agent.context import AgentContext
from app.agent.harness import answer
from app.config import IMAGES_DIR
from app.db.knowledge import MiniUserState, PendingPostAction, ProfileDocument
from app.db.models import (
    AgentMessage,
    Business,
    Comment,
    ContentPlanItem,
    ContentPlanStatus,
    EventLog,
    MessageRole,
    Platform,
    Post,
    PostRevision,
    PostStatus,
    UserRole,
)
from app.db.publications import RevisionDecision, RevisionProvenance
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
from app.ingestion.knowledge import MATERIAL_EXTENSIONS
from app.ingestion.website import WebsiteFetchError, fetch_site_text, pages_to_text
from app.llm.base import LLMError
from app.services import (
    accounts,
    analytics,
    approval,
    business_profile,
    comments,
    content_plan,
    default_profile,
    media,
    posts,
    reports,
    social_metrics,
)
from app.services.document_uploads import DocumentQuotaError, save_document, stage_document
from app.services.post_history import RevisionSource, save_revision
from app.services.usage import get_budget, month_spend, set_budget, usage_scope
from app.utils.time import format_local, to_local, to_utc_naive, utcnow
from app.web.telegram_auth import validate_init_data
from app.web.uploads import UPLOAD_TIMEOUT

if TYPE_CHECKING:
    from app.container import AppContainer

log = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / "miniapp"
POST_LIMIT = 75
SECURITY = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'self' https://telegram.org; "
    "style-src 'self'; img-src 'self' https: blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'self' https://web.telegram.org",
}


def json_response(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=SECURITY)


class MiniApp:
    def __init__(self, container: AppContainer):
        self.container = container
        self.active_uploads = 0

    def register(self, app: web.Application) -> None:
        app.router.add_get("/miniapp", self.asset)
        app.router.add_get(r"/miniapp/{asset:ui\.js|ui\.css|icons\.svg|glass\.webp}", self.asset)
        app.router.add_route(
            "*",
            "/miniapp/api/{action:state|profiles|select|profile|document|post|plan|chat|preview|settings|comment|photo|report|history|diagnostics|website|metrics|post-pool|versions}",
            self.api,
        )

    async def asset(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            ASSETS / request.match_info.get("asset", "index.html"), headers=SECURITY
        )

    async def api(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        method = request.method
        if request.headers.get("Origin") not in {None, self.container.settings.miniapp_public_url}:
            return json_response({"error": "Неверный источник запроса"}, 403)
        if (action, method) not in {
            ("state", "GET"),
            ("report", "GET"),
            ("history", "GET"),
            ("metrics", "GET"),
            ("post-pool", "GET"),
            ("versions", "GET"),
            ("diagnostics", "GET"),
            ("preview", "GET"),
            ("profiles", "POST"),
            ("select", "POST"),
            ("profile", "PATCH"),
            ("profile", "DELETE"),
            ("document", "PUT"),
            ("document", "DELETE"),
            ("document", "PATCH"),
            ("website", "POST"),
            ("post", "POST"),
            ("post", "PUT"),
            ("photo", "GET"),
            ("photo", "PUT"),
            ("photo", "POST"),
            ("plan", "POST"),
            ("chat", "POST"),
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
                if selection is None:
                    selection = MiniUserState(user_id=user.id, selected_id=user.business_id)
                    session.add(selection)
                    await session.flush()
                if (
                    not selection.default_created
                    and user.is_active
                    and (
                        not accounts.is_admin(user)
                        or user.telegram_id in self.container.settings.allowed_ids
                    )
                ):
                    await self.create_default(session, user, selection)
                elif user.is_active:
                    await self.fill_default_placeholder(session, user)
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
                    if not image_path:
                        return json_response({"error": "Изображение недоступно"}, 404)
                    path = Path(image_path).resolve()
                    if (
                        not path.is_relative_to(IMAGES_DIR.resolve())
                        or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
                        or not path.is_file()
                    ):
                        return json_response({"error": "Изображение недоступно"}, 404)
                    return web.FileResponse(path, headers=SECURITY)
                if action == "document" and method == "PUT":
                    company = allowed.get(int(request.query.get("company", 0)))
                    if company is None:
                        return json_response({"error": "Профиль недоступен"}, 404)
                    return await self.upload(request, session, company, user)
                if action == "photo":
                    if active is None or active.owner_id != user.id:
                        return json_response({"error": "Выберите свой профиль"}, 403)
                    return await self.photo_action(request, session, active, user)
                if action == "post" and method == "PUT":
                    if active is None or active.owner_id != user.id:
                        return json_response({"error": "Выберите свой профиль"}, 403)
                    return await self.replace_photo(request, session, active, user)
                if request.content_length is None or request.content_length > 32_768:
                    return json_response({"error": "Слишком большой запрос"}, 413)
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError("Ожидался объект")
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
                    if action in {"profile", "document", "website"} and company.owner_id != user.id:
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
                        if default_profile.is_preset(company):
                            return json_response(
                                {"error": "Стандартный профиль нельзя удалить"}, 403
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
                            selection.selected_id = None
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
                        if method == "DELETE" and default_profile.is_preset_document(doc):
                            return json_response(
                                {"error": "Базу знаний стандартного профиля удалить нельзя"},
                                403,
                            )
                        if method == "DELETE":
                            doc.status = "deleted"
                        elif doc.status == "failed":
                            doc.status = "queued"
                            doc.error = None
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
                        async with usage_scope(session, company.id):
                            if action == "chat":
                                message = str(data.get("text", "")).strip()
                                if not 0 < len(message) <= 4000:
                                    raise ValueError("Сообщение должно быть от 1 до 4000 символов")
                                ctx = AgentContext(
                                    session,
                                    user,
                                    company,
                                    self.container.settings,
                                    self.container.llm,
                                    self.container.images,
                                )
                                reply = await answer(ctx, message)
                                await session.commit()
                                return json_response(reply)
                            await self.post_action(session, user, company, data)
                await session.commit()
                return json_response({"ok": True})
        except approval.PostChangedError:
            return json_response(
                {"error": "Пост изменился. Обновите экран и проверьте черновик."}, 409
            )
        except LLMError:
            return json_response(
                {"error": "ИИ или поиск временно недоступен. Попробуйте ещё раз."}, 503
            )
        except (ValueError, KeyError, TypeError) as exc:
            return json_response({"error": str(exc)[:300]}, 400)
        except Exception:
            log.exception("Ошибка Mini App в маршруте %s", action)
            return json_response({"error": "Операция не завершилась. Попробуйте ещё раз."}, 500)

    async def create_default(self, session, user, selection) -> None:
        """Стандартный профиль САЛАВАТСТЕКЛО: карточка, пометка preset и база знаний."""
        path = self.container.settings.default_profile_file
        template = default_profile.load_template(path)
        company = await BusinessRepo(session).create(user.id, template.name)
        company.settings = {"miniapp_private": True, "preset": default_profile.PRESET_ID}
        await BusinessRepo(session).set_profile(company, template.model_dump())
        if default_profile.uses_bundled(path):
            await default_profile.seed_knowledge(session, company)
        if selection.selected_id is None:
            selection.selected_id = company.id
        selection.default_created = True

    async def fill_default_placeholder(self, session, user) -> None:
        """Профили-заглушки старых версий превращаем в стандартный профиль."""
        path = self.container.settings.default_profile_file
        for company in await BusinessRepo(session).list_owned(user.id):
            settings = company.settings or {}
            if not settings.get("default_placeholder") or settings.get("miniapp_archived"):
                continue
            if not business_profile.get_profile(company).is_filled():
                template = default_profile.load_template(path)
                await BusinessRepo(session).set_profile(company, template.model_dump())
            company.settings = {
                **settings,
                "default_placeholder": False,
                "preset": default_profile.PRESET_ID,
            }
            if default_profile.uses_bundled(path):
                await default_profile.seed_knowledge(session, company)

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
        if self.container.storage is None:
            return json_response({"error": "Хранилище материалов не настроено"}, 503)
        url = str(data.get("url", "")).strip()
        if not 0 < len(url) <= 2048:
            raise ValueError("Укажите адрес сайта")
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
        except WebsiteFetchError as exc:
            return json_response({"error": str(exc)}, 422)
        except TimeoutError:
            return json_response({"error": "Сайт не ответил вовремя"}, 504)
        except DocumentQuotaError:
            return json_response({"error": "В профиле не осталось места для материала"}, 413)
        finally:
            self.active_uploads -= 1
        return json_response({"ok": True})

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
        payload = await social_metrics.series(session, company.id, start, end, post_id)
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
                select(PostRevision, RevisionProvenance, AgentMessage.content)
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
        return json_response(
            {
                "topic": post.topic,
                "plan_item_id": post.plan_item_id,
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
                        "decisions": decisions.get(version.id, []),
                    }
                    for version, meta, message in rows
                ],
            }
        )

    async def publication_history(self, session, company) -> web.Response:
        rows = await session.scalars(
            select(Post)
            .where(
                Post.business_id == company.id,
                Post.status.in_([PostStatus.PUBLISHED, PostStatus.REJECTED]),
            )
            .order_by(Post.id.desc())
            .limit(POST_LIMIT)
        )
        return json_response(
            {
                "posts": [
                    {
                        "id": post.id,
                        "topic": post.topic,
                        "text": post.text,
                        "status": post.status,
                        "published_at": post.published_at.replace(tzinfo=UTC).isoformat()
                        if post.published_at
                        else None,
                        "url": post.external_url,
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

    async def read_photo(self, request) -> bytes | web.Response:
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
        plans = []
        proposal = None
        management = None
        if active:
            if active.owner_id == user.id:
                management = await self.management_state(session, active)
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
            for post, suggested_at in results:
                items.append(
                    {
                        "id": post.id,
                        "topic": post.topic,
                        "text": post.text,
                        "status": post.status,
                        "platform": post.platform,
                        "version": post.updated_at.isoformat(),
                        "has_image": bool(post.image_path),
                        "when": post.scheduled_at.replace(tzinfo=UTC).isoformat()
                        if post.scheduled_at
                        else None,
                        "suggested_at": suggested_at.replace(tzinfo=UTC).isoformat()
                        if suggested_at
                        else None,
                    }
                )
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
                    "name": doc.filename,
                    "status": doc.status,
                    "size": doc.size,
                    "error": doc.error,
                }
                for doc in docs
            ]
        history = []
        if active:
            messages = await AgentMessageRepo(session).last_n(user.id, 50, business_id=active.id)
            history = [
                {"text": message.content, "me": message.role == MessageRole.USER}
                for message in messages
                if message.content
                and message.role in {MessageRole.USER, MessageRole.ASSISTANT}
                and not message.tool_calls
            ]
        profiles = []
        for company in companies:
            channel = await ChannelRepo(session).get_active(company.id, Platform.TELEGRAM)
            profiles.append(
                {
                    "id": company.id,
                    "name": company.name,
                    "details": business_profile.get_profile(company).model_dump(),
                    "editable": company.owner_id == user.id,
                    "invite_enabled": accounts.is_admin(user) and company.owner_id == user.id,
                    "channel": channel.external_id if channel else "",
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
                "history": history,
                "documents": materials,
                "storage": self.container.storage is not None,
                "stories": self.stories(),
            }
        )

    def stories(self) -> list[dict[str, str]]:
        path = self.container.settings.miniapp_stories_file
        if path:
            entries = json.loads(Path(path).read_text(encoding="utf-8"))
            return [
                {"title": str(item["title"])[:80], "text": str(item["text"])[:4000]}
                for item in entries[:20]
            ]
        return [
            {
                "title": "Мини-приложение",
                "text": "Профили компаний, согласование и расписание теперь доступны в одном интерфейсе.",
            }
        ]

    async def edit_profile(self, session, company, user, data) -> None:
        fields = {
            key: str(value)[:2000]
            for key, value in data.items()
            if key in business_profile.PROFILE_FIELD_TITLES
        }
        if "name" in fields and not fields["name"].strip():
            raise ValueError("Укажите название")
        await business_profile.update_profile_fields(session, company, fields)
        if (company.settings or {}).get("default_placeholder"):
            company.settings = {**company.settings, "default_placeholder": False}
        if data.get("channel"):
            channel = accounts.normalize_channel_id(str(data["channel"]).strip())
            channels = ChannelRepo(session)
            existing = await channels.get_active(company.id, Platform.TELEGRAM)
            if existing is None or existing.external_id != channel:
                bot = self.container.bot
                if bot is None:
                    raise ValueError("Проверка канала недоступна")
                try:
                    chat_id = int(channel) if channel.startswith("-100") else channel
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
                await channels.upsert(company.id, Platform.TELEGRAM, channel)
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
            or Path(name).suffix.lower() not in MATERIAL_EXTENSIONS
        ):
            return json_response(
                {
                    "error": "Для визуалов используйте PDF, PNG, JPG или WebP. Презентации экспортируйте в PDF."
                },
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
                await save_document(session, company, name, path, storage=self.container.storage)
        except DocumentQuotaError:
            return json_response({"error": "Файл не помещается в общую квоту 100 МБ"}, 413)
        except TimeoutError:
            return json_response({"error": "Время загрузки истекло"}, 408)
        finally:
            self.active_uploads -= 1
        return json_response({"ok": True})

    async def post_action(self, session, user, company, data) -> None:
        post = await PostRepo(session).get_for_business(int(data["id"]), company.id)
        if post is None:
            raise ValueError("Пост не найден")
        action = data["action"]
        publishing = self.container.publishing
        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise approval.PostChangedError
        if action == "publish":
            result = await approval.approve_and_publish(
                session, post, user, publishing, expected_version=version
            )
            if not result.ok:
                raise ValueError(result.error or "Публикация не состоялась")
            return
        async with publishing.post_lock(post.id):
            await session.refresh(post)
            if post.updated_at.isoformat() != version:
                raise approval.PostChangedError
            if action == "approve" and post.status == PostStatus.PENDING_APPROVAL:
                await approval.approve(session, post, user)
            elif action == "schedule" and post.status in {
                PostStatus.PENDING_APPROVAL,
                PostStatus.APPROVED,
                PostStatus.SCHEDULED,
            }:
                when = datetime.fromisoformat(data["when"])
                if when.tzinfo is None:
                    raise ValueError("Укажите время публикации")
                await approval.schedule(session, post, when, user)
            elif action == "reject" and post.status in {
                PostStatus.DRAFT,
                PostStatus.PENDING_APPROVAL,
                PostStatus.FAILED,
            }:
                await approval.reject(session, post, user)
            elif action == "cancel" and post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED}:
                post.scheduled_at = None
                posts.transition(post, PostStatus.DRAFT)
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
