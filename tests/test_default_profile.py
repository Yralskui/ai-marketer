"""Стандартный профиль САЛАВАТСТЕКЛО: шаблон, база знаний, защита от удаления."""

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.container import build_container
from app.db.knowledge import KnowledgeChunk, ProfileDocument
from app.db.models import Business
from app.services import default_profile
from app.web.miniapp import MiniApp
from app.worker import process
from tests.test_miniapp import BOT_TOKEN, signed_user


def test_bundled_template_and_knowledge():
    template = default_profile.load_template()
    assert template.name == "САЛАВАТСТЕКЛО"
    assert template.products and template.visual_style and template.restrictions

    chunks = default_profile.knowledge_chunks()
    assert len(chunks) >= 25
    for section in default_profile.SECTIONS:
        assert any(f"Раздел: {section}" in chunk for chunk in chunks)
    assert all(chunk.startswith("## ") and "<!--" not in chunk for chunk in chunks)
    text = default_profile.knowledge_text()
    for fact in ("MultiComfort° Light Blue 55/38", "#196C7F", "968708", "30 июня 1962"):
        assert fact in text
    # Название бренда в кавычках как продукт — нарушение брендбука
    assert "«САЛАВАТСТЕКЛО»" not in text


async def test_preset_is_created_seeded_and_locked(settings, db):
    settings.bot_token = BOT_TOKEN
    settings.miniapp_open_signup = True
    container = build_container(settings)
    await container.db.dispose()
    container.db = db
    app = web.Application()
    MiniApp(container).register(app)
    headers = {"Authorization": "Telegram " + signed_user(501)}
    async with TestClient(TestServer(app)) as client:
        api = "/miniapp/api/"
        state = await (await client.get(api + "state", headers=headers)).json()
        company = state["active"]
        assert state["profiles"][0]["name"] == "САЛАВАТСТЕКЛО"
        assert [doc["name"] for doc in state["documents"]] == [default_profile.DOCUMENT_NAME]
        document = state["documents"][0]["id"]

        response = await client.delete(api + "profile", headers=headers, json={"company": company})
        assert response.status == 403
        response = await client.delete(
            api + "document", headers=headers, json={"company": company, "id": document}
        )
        assert response.status == 403

        # Повторный вход не создаёт второй профиль и второй материал
        state = await (await client.get(api + "state", headers=headers)).json()
        assert [p["id"] for p in state["profiles"]] == [company]
        assert len(state["documents"]) == 1

        # Свой профиль удалить по-прежнему можно
        response = await client.post(api + "profiles", headers=headers, json={"name": "Своя"})
        own = (await response.json())["id"]
        response = await client.delete(api + "profile", headers=headers, json={"company": own})
        assert response.status == 200

    async with db.sessionmaker() as session:
        business = await session.get(Business, company)
        assert default_profile.is_preset(business)


class FakeModels:
    def __init__(self):
        self.embedded: list[str] = []

    async def embed(self, texts):
        self.embedded.extend(texts)
        return [[0.0, 1.0] for _ in texts]

    async def analyze(self, path, profile):  # pragma: no cover - не должен вызываться
        raise AssertionError("встроенная база знаний не разбирается моделью")


class FakeStorage:
    def __init__(self):
        self.saved: dict[str, str] = {}

    async def download(self, key, path):  # pragma: no cover - не должен вызываться
        raise AssertionError("встроенная база знаний не скачивается из S3")

    async def put_markdown(self, key, content):
        self.saved[key] = content

    async def delete(self, key):
        self.saved.pop(key, None)


async def test_worker_indexes_bundled_knowledge_without_model(db):
    async with db.sessionmaker() as session:
        from app.db.repositories import BusinessRepo, UserRepo

        user = await UserRepo(session).get_or_create(9001, "Owner")
        business = await BusinessRepo(session).create(user.id, "САЛАВАТСТЕКЛО")
        business.settings = {"preset": default_profile.PRESET_ID}
        document = await default_profile.seed_knowledge(session, business)
        again = await default_profile.seed_knowledge(session, business)
        assert again.id == document.id
        document.status = "processing"
        await session.commit()
        await session.refresh(document)

    models, storage = FakeModels(), FakeStorage()
    await process(db, storage, models, document)

    async with db.sessionmaker() as session:
        stored = await session.get(ProfileDocument, document.id)
        assert stored.status == "ready", stored.error
        chunks = (
            await session.scalars(
                select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
            )
        ).all()
    assert len(chunks) == len(default_profile.knowledge_chunks()) == len(models.embedded)
    assert len(storage.saved) == len(chunks)
