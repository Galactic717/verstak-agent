import json
import math
import re
from collections import Counter
from datetime import datetime
from hashlib import blake2b
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from verstak.documents.ingest import ParsedDocument, Sheet

EMBEDDING_DIMENSIONS = 256
CHUNK_CHARACTERS = 1_000
CHUNK_OVERLAP = 120
CONTEXT_CHARACTERS = 8_000
TOKEN_PATTERN = re.compile(r"[^\W_]+")


class Document(BaseModel):
    id: UUID
    filename: str
    media_type: str
    size_bytes: int
    created_at: datetime
    text: str
    tabular: list[Sheet] | None


class SearchHit(BaseModel):
    document_id: UUID
    filename: str
    chunk_number: int
    text: str
    score: float


def embedding_literal(content: str) -> str | None:
    """Make a reproducible sparse lexical vector without a model or external service."""
    buckets = [0.0] * EMBEDDING_DIMENSIONS
    for token, count in Counter(TOKEN_PATTERN.findall(content.casefold())).items():
        digest = blake2b(token.encode("utf-8"), digest_size=4, person=b"verstak").digest()
        position = int.from_bytes(digest[:2], "big") % EMBEDDING_DIMENSIONS
        sign = 1 if digest[2] & 1 else -1
        buckets[position] += sign * (1 + math.log(count))
    norm = math.hypot(*buckets)
    if norm == 0:
        return None
    return "[" + ",".join(f"{value / norm:.8g}" for value in buckets) + "]"


_SELECT_DOCUMENT = text(
    """
    SELECT id, filename, media_type, size_bytes, created_at, content AS text, tabular
    FROM documents
    WHERE tenant = :tenant AND id = :id
    """
)


async def get_document(session: AsyncSession, tenant: str, document_id: UUID) -> Document | None:
    result = await session.execute(_SELECT_DOCUMENT, {"tenant": tenant, "id": document_id})
    row = result.mappings().one_or_none()
    return Document.model_validate(row) if row else None


async def save_document(session: AsyncSession, tenant: str, document: ParsedDocument) -> Document:
    document_id = uuid4()
    rows = await session.execute(
        text(
            """
            INSERT INTO documents (id, tenant, filename, media_type, size_bytes, content, tabular)
            VALUES (:id, :tenant, :filename, :media_type, :size_bytes, :content, CAST(:tabular AS jsonb))
            RETURNING id, filename, media_type, size_bytes, created_at, content AS text, tabular
            """
        ),
        {
            "id": document_id,
            "tenant": tenant,
            "filename": document.filename,
            "media_type": document.media_type,
            "size_bytes": document.size_bytes,
            "content": document.text,
            "tabular": (
                json.dumps([sheet.model_dump() for sheet in document.tabular])
                if document.tabular is not None
                else None
            ),
        },
    )
    saved = Document.model_validate(rows.mappings().one())
    chunks = []
    step = CHUNK_CHARACTERS - CHUNK_OVERLAP
    for number, start in enumerate(range(0, len(document.text), step)):
        content = document.text[start : start + CHUNK_CHARACTERS].strip()
        if content:
            chunks.append(
                {
                    "tenant": tenant,
                    "id": document_id,
                    "number": number,
                    "content": content,
                    "embedding": embedding_literal(content),
                }
            )
        if start + CHUNK_CHARACTERS >= len(document.text):
            break
    await session.execute(
        text(
            """
            INSERT INTO document_chunks (tenant, document_id, chunk_number, content, embedding)
            VALUES (:tenant, :id, :number, :content, CAST(:embedding AS vector(256)))
            """
        ),
        chunks,
    )
    return saved


# Robertson-Sparck Jones IDF and BM25 length normalization (k1 = 1.2, b = 0.75).
# Index the disjunction of normalized query lexemes, then score with per-tenant statistics.
_SEARCH = text(
    """
    WITH query_terms AS MATERIALIZED (
        SELECT term
        FROM unnest(tsvector_to_array(to_tsvector('simple', :query))) AS terms(term)
        ORDER BY term
        LIMIT 24
    ), query_filter AS (
        SELECT CAST(string_agg(CAST(array_to_tsvector(ARRAY[term]) AS text), ' | ') AS tsquery)
            AS disjunction
        FROM query_terms
    ), document_lengths AS MATERIALIZED (
        SELECT c.id, (
            SELECT coalesce(sum(cardinality(word.positions)), 0) FROM unnest(c.lexemes) AS word
        ) AS token_count
        FROM document_chunks AS c
        WHERE c.tenant = :tenant
    ), corpus AS (
        SELECT count(*) AS num_docs, avg(token_count) AS avg_length FROM document_lengths
    ), matching_terms AS (
        SELECT c.id, cardinality(word.positions) AS term_frequency,
            count(*) OVER (PARTITION BY word.lexeme) AS document_frequency
        FROM document_chunks AS c
        CROSS JOIN query_filter
        CROSS JOIN LATERAL unnest(c.lexemes) AS word
        JOIN query_terms ON query_terms.term = word.lexeme
        WHERE c.tenant = :tenant AND c.lexemes @@ query_filter.disjunction
    ), bm25 AS (
        SELECT m.id, sum(
            ln(1.0 + (corpus.num_docs - m.document_frequency + 0.5) / (m.document_frequency + 0.5))
            * (m.term_frequency * 2.2)
            / (m.term_frequency + 1.2 * (0.25 + 0.75 * d.token_count / nullif(corpus.avg_length, 0)))
        ) AS relevance
        FROM matching_terms AS m
        JOIN document_lengths AS d ON d.id = m.id
        CROSS JOIN corpus
        GROUP BY m.id
        ORDER BY relevance DESC, m.id
        LIMIT :candidates
    ), lexical AS (
        SELECT id, row_number() OVER (ORDER BY relevance DESC, id) AS position
        FROM bm25
    ), nearest AS MATERIALIZED (
        SELECT id, embedding <=> CAST(:embedding AS vector(256)) AS distance
        FROM document_chunks
        WHERE tenant = :tenant AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:embedding AS vector(256))
        LIMIT :candidates
    ), vector_hits AS (
        SELECT id, row_number() OVER (ORDER BY distance, id) AS position
        FROM nearest
        WHERE distance < 0.9
    ), fused AS (
        SELECT id, sum(1.0 / (60 + position)) AS score
        FROM (
            SELECT id, position FROM lexical
            UNION ALL
            SELECT id, position FROM vector_hits
        ) AS hits
        GROUP BY id
    )
    SELECT d.id AS document_id, d.filename, c.chunk_number, c.content AS text, f.score
    FROM fused AS f
    JOIN document_chunks AS c ON c.id = f.id AND c.tenant = :tenant
    JOIN documents AS d ON d.id = c.document_id AND d.tenant = :tenant
    ORDER BY f.score DESC, f.id
    LIMIT :limit
    """
)


async def search_documents(
    session: AsyncSession, tenant: str, query: str, limit: int = 5
) -> list[SearchHit]:
    embedding = embedding_literal(query)
    if embedding is None:
        return []
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    result = await session.execute(
        _SEARCH,
        {
            "tenant": tenant,
            "query": query,
            "embedding": embedding,
            "candidates": limit * 8,
            "limit": limit,
        },
    )
    return [SearchHit.model_validate(row) for row in result.mappings()]


async def document_context(session: AsyncSession, tenant: str, document_id: UUID) -> str | None:
    result = await session.execute(
        text("SELECT left(content, :limit) FROM documents WHERE tenant = :tenant AND id = :id"),
        {"limit": CONTEXT_CHARACTERS, "tenant": tenant, "id": document_id},
    )
    content: str | None = result.scalar_one_or_none()
    return content


async def search_context(session: AsyncSession, tenant: str, query: str) -> str:
    hits = await search_documents(session, tenant, query[:2_000], limit=4)
    return "\n\n".join(f"Source {hit.filename} ({hit.document_id})\n{hit.text}" for hit in hits)
