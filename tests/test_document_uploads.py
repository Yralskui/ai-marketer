"""Квота и права общей загрузки документов."""

from urllib.parse import quote

from aiohttp.test_utils import TestClient, TestServer

from app.container import build_container
from app.db.models import Business, SourceType
from app.db.repositories import SourceRepo
from app.services import document_uploads
from app.services.document_uploads import remaining_bytes, save_document, stage_document
from app.web.uploads import UploadServer


async def test_web_upload_is_one_use_and_shares_company_quota(
    session, db, owner, settings, monkeypatch, tmp_path
):
    user, company = owner
    other = Business(owner_id=user.id, name="Другая компания")
    session.add(other)
    await session.commit()

    monkeypatch.setattr(document_uploads, "PROFILE_DOCUMENT_BYTES", 20)
    monkeypatch.setattr(document_uploads, "DATA_DIR", tmp_path)
    with stage_document(company.id) as staged:
        staged.write_bytes(b"0123456789")
        await save_document(session, company, "first.txt", staged)
    settings.upload_public_url = "https://uploads.example"
    container = build_container(settings)
    await container.db.dispose()
    container.db = db

    async with TestClient(TestServer(UploadServer(container).application())) as client:

        async def send(token: str, body: bytes, *, origin: str = settings.upload_public_url) -> int:
            result = await client.post(
                "/upload",
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": origin,
                    "Content-Type": "application/octet-stream",
                    "X-Filename": quote("материал.txt"),
                },
            )
            await result.read()
            return result.status

        token = container.upload_links.issue(user.id, company.id)
        assert await send(token, b"\x00\xff23456789", origin="https://other.example") == 403
        assert await send(token, b"\x00\xff23456789") == 200
        assert await send(token, b"\x00\xff23456789") == 401
        assert await send(container.upload_links.issue(user.id, company.id), b"x") == 413
        assert await send(container.upload_links.issue(user.id, other.id), b"x") == 403

    files = list((tmp_path / "documents" / str(company.id)).glob("*.txt"))
    assert {path.read_bytes() for path in files} == {
        b"0123456789",
        bytes.fromhex("00ff") + b"23456789",
    }
    async with db.sessionmaker() as check:
        sources = await SourceRepo(check).list_for_business(company.id)
        assert [(source.origin, source.text) for source in sources] == [
            ("first.txt", ""),
            ("материал.txt", ""),
        ]
        assert await SourceRepo(check).list_for_business(other.id) == []


def test_expired_and_replaced_links_cannot_be_used(monkeypatch):
    monkeypatch.setattr(document_uploads, "monotonic", lambda: 1000)
    links = document_uploads.UploadLinks()
    old = links.issue(1, 2)
    latest = links.issue(1, 3)
    assert links.consume(old) is None
    monkeypatch.setattr(document_uploads, "monotonic", lambda: 1600)
    assert links.consume(latest) is None


async def test_legacy_documents_count_by_extracted_bytes(session, owner, monkeypatch):
    _, company = owner
    monkeypatch.setattr(document_uploads, "PROFILE_DOCUMENT_BYTES", 20)
    await SourceRepo(session).add(company.id, SourceType.DOCUMENT, "old.txt", "тест")
    await SourceRepo(session).add(company.id, SourceType.WEBSITE, "site", "not a document")
    assert await remaining_bytes(session, company.id) == 12
