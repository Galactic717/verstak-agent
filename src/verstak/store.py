from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from verstak.config import settings
from verstak.contracts import RunStatus
from verstak.db import CaseAction, DeadLetter, ModelAttempt, Run, sessions

RUNNABLE = ("queued", "running")
TERMINAL = ("succeeded", "rejected", "failed")


async def tenant_run(
    session: AsyncSession, tenant: str, run_id: UUID, *, lock: bool = False
) -> Run | None:
    query = select(Run).where(Run.id == run_id, Run.tenant == tenant)
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


async def lease_due_runs() -> list[UUID]:
    async with sessions().begin() as session:
        query = (
            select(Run)
            .where(Run.status.in_(RUNNABLE), Run.dispatch_after <= func.now())
            .order_by(Run.dispatch_after)
            .limit(32)
            .with_for_update(skip_locked=True)
        )
        runs = (await session.scalars(query)).all()
        deadline = datetime.now(UTC) + timedelta(seconds=settings().dispatch_lease_seconds)
        for run in runs:
            run.dispatch_after = deadline
        return [run.id for run in runs]


class RunLimit(Exception):
    """A persisted step or spend limit prevents another model call."""


async def reserve_attempt(run_id: UUID, maximum_cost: int) -> int:
    async with sessions().begin() as session:
        run = await session.get_one(Run, run_id, with_for_update=True)
        if run.attempts >= run.max_steps:
            raise RunLimit("step_limit")
        if run.cost_micros + maximum_cost > run.budget_micros:
            raise RunLimit("budget_limit")
        run.attempts += 1
        run.cost_micros += maximum_cost
        session.add(
            ModelAttempt(run_id=run_id, sequence=run.attempts, reserved_micros=maximum_cost)
        )
        return run.attempts


async def settle_attempt(
    run_id: UUID,
    sequence: int,
    cost: int | None,
    input_tokens: int,
    output_tokens: int,
    triage: dict[str, object],
) -> bool:
    async with sessions().begin() as session:
        run = await session.get_one(Run, run_id, with_for_update=True)
        attempt = await session.get_one(ModelAttempt, (run_id, sequence))
        if attempt.charged_micros is None:
            charge = attempt.reserved_micros if cost is None else cost
            run.cost_micros += charge - attempt.reserved_micros
            attempt.charged_micros = charge
            attempt.input_tokens = input_tokens
            attempt.output_tokens = output_tokens
        within_budget = run.cost_micros <= run.budget_micros
        if within_budget:
            run.triage = triage
        return within_budget


async def saved_triage(run_id: UUID) -> dict[str, object] | None:
    async with sessions()() as session:
        return await session.scalar(select(Run.triage).where(Run.id == run_id))


async def apply_case(run_id: UUID, tenant: str, tool: str, payload: dict[str, object]) -> None:
    async with sessions().begin() as session:
        run = await tenant_run(session, tenant, run_id, lock=True)
        if run is None or run.approved is not True:
            raise RuntimeError("case action requires persisted approval")
        await session.execute(
            insert(CaseAction)
            .values(run_id=run_id, tenant=tenant, tool=tool, payload=payload)
            .on_conflict_do_nothing(index_elements=[CaseAction.run_id])
        )


async def record_progress(
    run_id: UUID,
    status: RunStatus,
    triage: dict[str, object] | None,
    error: str | None,
) -> None:
    async with sessions().begin() as session:
        run = await session.get_one(Run, run_id, with_for_update=True)
        if run.status in TERMINAL:
            return
        run.status = status
        run.triage = triage
        run.error = error
        run.worker_failures = 0
        run.retry_at = None
        if status in TERMINAL:
            run.finished_at = datetime.now(UTC)
        if status == "failed":
            session.add(DeadLetter(run_id=run_id, error=error or "workflow_failed"))


async def record_worker_failure(run_id: UUID, error: str) -> None:
    async with sessions().begin() as session:
        run = await session.get_one(Run, run_id, with_for_update=True)
        if run.status not in RUNNABLE:
            return
        run.worker_failures += 1
        run.error = error
        now = datetime.now(UTC)
        if run.worker_failures > settings().worker_retries:
            run.status = "failed"
            run.finished_at = now
            session.add(DeadLetter(run_id=run_id, error=error))
        else:
            retry = now + timedelta(seconds=min(60, 2**run.worker_failures))
            run.retry_at = retry
            run.dispatch_after = retry
