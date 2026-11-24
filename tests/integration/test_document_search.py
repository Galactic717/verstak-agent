import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from verstak.documents import document_context, search_context
from verstak.documents.ingest import parse_document
from verstak.documents.repository import get_document, save_document, search_documents


def _exercise_database(scenario: Callable[[AsyncSession], Awaitable[None]]) -> None:
    url = os.environ.get("VERSTAK_TEST_DATABASE_URL")
    if not url:
        pytest.skip("VERSTAK_TEST_DATABASE_URL must point to a migrated pgvector test database")

    async def exercise(dsn: str) -> None:
        db = create_async_engine(
            dsn.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
        )
        try:
            async with AsyncSession(db) as session:
                await scenario(session)
        finally:
            await db.dispose()

    # Psycopg's async socket support on Windows requires the selector event loop.
    asyncio.run(
        exercise(url), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None
    )


@pytest.mark.integration
def test_pgvector_search_and_document_reads_isolate_tenants() -> None:
    async def exercise(session: AsyncSession) -> None:
        first = "test-docs-a-" + uuid4().hex
        second = "test-docs-b-" + uuid4().hex
        first_doc = await save_document(
            session, first, parse_document("policy.txt", b"Quasar withholding invoice limit")
        )
        second_doc = await save_document(
            session, second, parse_document("private.txt", b"Quasar withholding confidential")
        )
        hits = await search_documents(session, first, "quasar withholding")
        assert [hit.document_id for hit in hits] == [first_doc.id]
        assert hits[0].score > 1 / 61  # The lexical and vector rankings both contribute.
        assert await get_document(session, first, second_doc.id) is None
        assert await document_context(session, first, second_doc.id) is None
        assert await document_context(session, first, first_doc.id) == first_doc.text
        assert await get_document(session, second, second_doc.id) == second_doc
        context = await search_context(session, first, "quasar withholding")
        assert first_doc.text in context
        assert second_doc.text not in context

    _exercise_database(exercise)


@pytest.mark.integration
def test_bm25_weights_rare_terms_and_normalizes_for_chunk_length() -> None:
    async def exercise(session: AsyncSession) -> None:
        tenant = "test-bm25-" + uuid4().hex
        await save_document(session, tenant, parse_document("common.txt", b"routine"))
        await save_document(session, tenant, parse_document("common2.txt", b"routine report"))
        await save_document(session, tenant, parse_document("common3.txt", b"routine policy"))
        long = await save_document(
            session, tenant, parse_document("long.txt", b"quasar " + b"discussion " * 20)
        )
        short = await save_document(session, tenant, parse_document("short.txt", b"quasar"))
        await session.execute(
            text("UPDATE document_chunks SET embedding = NULL WHERE tenant = :tenant"),
            {"tenant": tenant},
        )
        hits = await search_documents(session, tenant, "routine quasar")
        assert len(hits) == 5  # Bag-of-words matching includes chunks containing either term.
        assert hits[0].document_id == short.id
        assert long.id in {hit.document_id for hit in hits[1:]}

    _exercise_database(exercise)
