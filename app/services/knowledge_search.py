"""Поиск только в материалах активного профиля."""

from collections import OrderedDict
from hashlib import sha256
from time import monotonic

from openai import OpenAIError
from pgvector.sqlalchemy import Vector
from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.knowledge import EMBEDDING_DIMENSIONS, KnowledgeChunk, ProfileDocument
from app.ingestion.knowledge import KnowledgeModels
from app.llm.base import LLMError

TOP_K = 6
MAX_COSINE_DISTANCE = 0.65
QUERY_CACHE_SIZE = 256
QUERY_CACHE_TTL = 600
_query_vectors: OrderedDict[tuple[str, str, int, str], tuple[float, list[float]]] = OrderedDict()


async def query_vector(settings: Settings, business_id: int, query: str) -> list[float]:
    key = (
        settings.llm_base_url,
        settings.embedding_model,
        business_id,
        sha256(query.encode()).hexdigest(),
    )
    now = monotonic()
    cached = _query_vectors.get(key)
    if cached is not None and cached[0] > now:
        _query_vectors.move_to_end(key)
        return cached[1]
    models = KnowledgeModels(settings)
    try:
        (vector,) = await models.embed([query])
    except OpenAIError as exc:
        raise LLMError("Поиск по материалам временно недоступен") from exc
    finally:
        await models.client.close()
    _query_vectors[key] = (monotonic() + QUERY_CACHE_TTL, vector)
    _query_vectors.move_to_end(key)
    if len(_query_vectors) > QUERY_CACHE_SIZE:
        _query_vectors.popitem(last=False)
    return vector


async def retrieve(session: AsyncSession, settings: Settings, business_id: int, query: str) -> str:
    stmt = (
        select(KnowledgeChunk.content, ProfileDocument.filename)
        .join(ProfileDocument)
        .where(ProfileDocument.business_id == business_id, ProfileDocument.status == "ready")
    )
    if not query.strip() or session.get_bind().dialect.name != "postgresql":
        return ""
    if await session.scalar(stmt.with_only_columns(KnowledgeChunk.id).limit(1)) is None:
        return ""
    vector = await query_vector(settings, business_id, query.strip())
    distance = cast(KnowledgeChunk.embedding, Vector(EMBEDDING_DIMENSIONS)).cosine_distance(vector)
    rows = await session.execute(
        stmt.where(distance <= MAX_COSINE_DISTANCE)
        .order_by(distance, KnowledgeChunk.id)
        .limit(TOP_K)
    )
    chunks = []
    available = 8_000
    for text, name in rows:
        chunk = f"[{name}]\n{text[:2500]}"
        chunks.append(chunk[:available])
        available -= len(chunks[-1]) + 2
        if available <= 0:
            break
    return "\n\n".join(chunks)
