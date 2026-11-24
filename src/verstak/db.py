from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from verstak.config import settings
from verstak.contracts import RunStatus


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("tenant", "idempotency_key", name="uq_run_idempotency"),
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_approval', 'succeeded', 'rejected', 'failed')",
            name="ck_run_status",
        ),
        Index(
            "ix_runs_dispatch",
            "dispatch_after",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_runs_tenant_created", "tenant", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(100))
    submitted_by: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    input_text: Mapped[str] = mapped_column(Text)
    document_id: Mapped[UUID | None]
    status: Mapped[RunStatus] = mapped_column(String(32), server_default="queued")
    max_steps: Mapped[int]
    budget_micros: Mapped[int] = mapped_column(BigInteger)
    cost_micros: Mapped[int] = mapped_column(BigInteger, server_default="0")
    attempts: Mapped[int] = mapped_column(server_default="0")
    worker_failures: Mapped[int] = mapped_column(server_default="0")
    triage: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    verification: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    traceparent: Mapped[str | None] = mapped_column(String(55))
    approved: Mapped[bool | None]
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(80))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def cost_usd(self) -> Decimal:
        return Decimal(self.cost_micros) / 1_000_000


class ModelAttempt(Base):
    __tablename__ = "model_attempts"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(primary_key=True)
    reserved_micros: Mapped[int] = mapped_column(BigInteger)
    charged_micros: Mapped[int | None] = mapped_column(BigInteger)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    proposal: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    verification: Mapped[dict[str, object] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaseAction(Base):
    __tablename__ = "case_actions"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(100))
    tool: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeadLetter(Base):
    __tablename__ = "dead_letters"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), primary_key=True)
    error: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Evaluation(Base):
    __tablename__ = "evaluations"
    __table_args__ = (Index("ix_evaluations_tenant_created", "tenant", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(100))
    report: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@lru_cache
def engine() -> AsyncEngine:
    url = settings().database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    # Celery tasks may run on different asyncio loops. Each checkout owns its connection.
    return create_async_engine(url, poolclass=NullPool)


@lru_cache
def sessions() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with sessions()() as session:
        yield session
