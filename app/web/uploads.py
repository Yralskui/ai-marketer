"""Страница загрузки и HTTP-приём документов для Telegram-бота."""

from __future__ import annotations

import asyncio
import logging
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from aiohttp import web
from sqlalchemy import text

from app.bot import texts
from app.db.models import Business
from app.db.repositories import UserRepo
from app.ingestion.knowledge import MATERIAL_EXTENSIONS
from app.services.accounts import is_admin
from app.services.document_uploads import (
    PROFILE_DOCUMENT_BYTES,
    DocumentQuotaError,
    UploadGrant,
    remaining_bytes,
    save_document,
    stage_document,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.container import AppContainer

log = logging.getLogger(__name__)
ASSETS = Path(__file__).parent
UPLOAD_TIMEOUT = 120
UPLOAD_PORT = 8080
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
}


class UploadServer:
    def __init__(self, container: AppContainer) -> None:
        self.container = container
        self.active_uploads = 0

    def application(self) -> web.Application:
        app = web.Application(client_max_size=PROFILE_DOCUMENT_BYTES)
        app.add_routes(
            [
                web.get("/upload", self.page),
                web.get("/upload.js", self.script),
                web.get("/upload.css", self.styles),
                web.post("/upload", self.upload),
            ]
        )
        return app

    async def page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(ASSETS / "upload.html", headers=HEADERS)

    async def script(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(ASSETS / "upload.js", headers=HEADERS)

    async def styles(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(ASSETS / "upload.css", headers=HEADERS)

    async def upload(self, request: web.Request) -> web.Response:
        if request.headers.get("Origin") != self.container.settings.upload_public_url:
            return response(403, "Откройте ссылку прямо из бота.")
        if request.content_type != "application/octet-stream" or request.headers.get(
            "Content-Encoding"
        ):
            return response(415, "Отправьте один файл через форму загрузки.")
        size = request.content_length
        if size is not None and not 0 < size <= PROFILE_DOCUMENT_BYTES:
            return response(413, "Размер файла должен быть от 1 байта до 100 МБ.")
        filename = Path(unquote(request.headers.get("X-Filename", "")).replace("\\", "/")).name
        suffix = Path(filename).suffix.lower()
        if (
            len(filename) > 255
            or any(ord(c) < 32 for c in filename)
            or suffix not in MATERIAL_EXTENSIONS
        ):
            return response(
                415,
                "Для визуалов используйте PDF, PNG, JPG или WebP. Текст: DOCX, TXT, MD, CSV, JSON, RTF.",
            )
        if self.active_uploads >= 2:
            return response(503, "Сервер занят. Запросите новую ссылку в боте через минуту.")
        auth = request.headers.get("Authorization", "")
        grant = (
            self.container.upload_links.consume(auth.removeprefix("Bearer "))
            if auth.startswith("Bearer ")
            else None
        )
        if grant is None:
            return response(401, "Ссылка истекла или уже использована. Запросите новую в боте.")

        self.active_uploads += 1
        try:
            return await self._receive(request, grant, filename)
        finally:
            self.active_uploads -= 1

    async def _receive(
        self, request: web.Request, grant: UploadGrant, filename: str
    ) -> web.Response:
        async with self.container.db.sessionmaker() as session:
            if await self._authorized_company(session, grant) is None:
                return response(403, "Нет доступа к профилю. Запросите новую ссылку в боте.")
            available = await remaining_bytes(session, grant.business_id)
        if available == 0 or (request.content_length or 0) > available:
            return response(413, "Файл превышает остаток лимита профиля в 100 МБ.")

        with stage_document(grant.business_id) as path:
            size = 0
            try:
                async with asyncio.timeout(UPLOAD_TIMEOUT):
                    with path.open("wb") as target:
                        async for chunk in request.content.iter_chunked(256 * 1024):
                            size += len(chunk)
                            if size > available:
                                return response(
                                    413, "Файл превышает остаток лимита профиля в 100 МБ."
                                )
                            target.write(chunk)
            except TimeoutError:
                return response(408, "Время загрузки истекло. Запросите новую ссылку в боте.")
            except ConnectionError:
                return response(400, "Передача файла прервалась.")
            if not size:
                return response(400, "Файл пуст.")
            async with self.container.db.sessionmaker() as session:
                company = await self._authorized_company(session, grant)
                if company is None:
                    return response(403, "Доступ к профилю изменился.")
                user = await UserRepo(session).get(grant.user_id)
                user_telegram_id = user.telegram_id
                try:
                    await save_document(
                        session, company, filename, path, storage=self.container.storage
                    )
                except DocumentQuotaError:
                    return response(413, "Файл превышает остаток лимита профиля в 100 МБ.")
        if self.container.notifier is not None:
            try:
                async with asyncio.timeout(5):
                    await self.container.notifier.send_text(
                        user_telegram_id, texts.UPLOAD_SAVED.format(name=escape(filename))
                    )
            except Exception:
                log.warning("Не удалось сообщить о загруженном документе")
        return response(200, "Документ добавлен.")

    async def _authorized_company(
        self, session: AsyncSession, grant: UploadGrant
    ) -> Business | None:
        user = await UserRepo(session).get(grant.user_id)
        if user is None or not user.is_active or user.business_id != grant.business_id:
            return None
        if is_admin(user) and user.telegram_id not in self.container.settings.allowed_ids:
            return None
        return await session.get(Business, grant.business_id)


def response(status: int, text: str) -> web.Response:
    result = web.Response(status=status, text=text, headers=HEADERS)
    if status >= 400:
        result.force_close()
    return result


async def start_upload_server(container: AppContainer) -> web.AppRunner | None:
    if not (container.settings.upload_public_url or container.settings.miniapp_public_url):
        return None
    app = UploadServer(container).application()

    async def ready(_request: web.Request) -> web.Response:
        try:
            async with asyncio.timeout(2), container.db.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            return response(503, "Unavailable")
        return response(200, "Ready")

    app.router.add_get("/readyz", ready)
    if container.settings.miniapp_public_url:
        from app.web.miniapp import MiniApp

        MiniApp(container).register(app)
    runner = web.AppRunner(
        app,
        access_log=None,
        auto_decompress=False,
    )
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", UPLOAD_PORT).start()
    except BaseException:
        await runner.cleanup()
        raise
    log.info("Страница загрузки документов запущена на порту %s", UPLOAD_PORT)
    return runner
