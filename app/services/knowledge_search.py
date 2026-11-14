"""Поиск только в материалах активного профиля."""

import operator
import re
from collections import OrderedDict
from functools import reduce
from hashlib import sha256
from time import monotonic

from openai import OpenAIError
from pgvector.sqlalchemy import Vector
from sqlalchemy import case, cast, func, select
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


# --- гибридный поиск: смысл (вектор) + ключевые слова ---------------------------------------
KEYWORD_CONFIG = "russian"
MAX_TERMS = 8
MIN_TERM_LENGTH = 3
CANDIDATES = 12
RRF_K = 60  # чем больше, тем мягче разница между местами в рейтингах
_STOP_WORDS = frozenset(
    [
        "это",
        "как",
        "что",
        "для",
        "при",
        "или",
        "так",
        "его",
        "она",
        "они",
        "были",
        "быть",
        "есть",
        "нам",
        "вам",
        "нас",
        "вас",
        "все",
        "всё",
        "где",
        "когда",
        "который",
        "которая",
        "которые",
        "чтобы",
        "только",
        "если",
        "еще",
        "ещё",
        "очень",
        "надо",
        "нужно",
        "про",
        "над",
        "под",
        "без",
        "между",
        "после",
        "перед",
        "может",
        "можно",
        "через",
        "также",
        "там",
        "тут",
        "уже",
        "ему",
        "ней",
        "них",
    ]
)
_WORD = re.compile(r"[\w-]+", re.UNICODE)


def query_terms(query: str) -> list[str]:
    """Значимые слова запроса: без служебных, не короче 3 букв, без повторов, не больше 8."""
    terms: list[str] = []
    for word in _WORD.findall(query.lower()):
        word = word.strip("-")
        if len(word) >= MIN_TERM_LENGTH and word not in _STOP_WORDS and word not in terms:
            terms.append(word)
    return terms[:MAX_TERMS]


def min_keyword_hits(terms: list[str]) -> int:
    """Совпадение одного слова ещё не значит, что смысл тот же: нужны минимум два слова.

    Если в запросе значимое слово всего одно (название, артикул), хватает его одного.
    """
    return 2 if len(terms) >= 2 else 1


def fuse(vector_ids: list[int], keyword_ids: list[int], limit: int) -> list[int]:
    """Reciprocal Rank Fusion: фрагмент, найденный обоими способами, поднимается выше.

    Вектор остаётся основным: ключевые слова только добавляют и переставляют, и то лишь для
    фрагментов, где совпало достаточно слов (см. `min_keyword_hits`).
    """
    scores: dict[int, float] = {}
    for ranking in (vector_ids, keyword_ids):
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:limit]


async def keyword_candidates(session: AsyncSession, base, terms: list[str]) -> list[int]:
    """Фрагменты, где совпало достаточно слов запроса (с учётом словоформ русского языка)."""
    if not terms:
        return []
    document = func.to_tsvector(KEYWORD_CONFIG, KnowledgeChunk.content)
    matches = [
        case((document.op("@@")(func.plainto_tsquery(KEYWORD_CONFIG, term)), 1), else_=0)
        for term in terms
    ]
    hits = reduce(operator.add, matches)  # сколько слов запроса нашлось во фрагменте
    rows = await session.execute(
        base.with_only_columns(KnowledgeChunk.id)
        .where(hits >= min_keyword_hits(terms))
        .order_by(hits.desc(), KnowledgeChunk.id)
        .limit(CANDIDATES)
    )
    return [row[0] for row in rows]


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
    vector_ids = [
        row[0]
        for row in await session.execute(
            stmt.with_only_columns(KnowledgeChunk.id)
            .where(distance <= MAX_COSINE_DISTANCE)
            .order_by(distance, KnowledgeChunk.id)
            .limit(CANDIDATES)
        )
    ]
    keyword_ids = await keyword_candidates(session, stmt, query_terms(query))
    chosen = fuse(vector_ids, keyword_ids, TOP_K)
    if not chosen:
        return ""
    rows = {
        chunk_id: (text, name)
        for chunk_id, text, name in await session.execute(
            stmt.with_only_columns(
                KnowledgeChunk.id, KnowledgeChunk.content, ProfileDocument.filename
            ).where(KnowledgeChunk.id.in_(chosen))
        )
    }
    chunks = []
    available = 8_000
    for chunk_id in chosen:
        text, name = rows[chunk_id]
        chunk = f"[{name}]\n{text[:2500]}"
        chunks.append(chunk[:available])
        available -= len(chunks[-1]) + 2
        if available <= 0:
            break
    return "\n\n".join(chunks)
