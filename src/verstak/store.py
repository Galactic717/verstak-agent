from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from verstak.config import settings
from verstak.contracts import GroundingReview, RunStatus
from verstak.db import CaseAction, DeadLetter, ModelAttempt, Run, sessions
from verstak.llm import Triage
from verstak.observability import trace_span

RUNNABLE = ("queued", "running")
TERMINAL = ("succeeded", "rejected", "failed")


async def tenant_run(
    session: AsyncSession, tenant: str, run_id: UUID, *, lock: bool = False
) -> Run | None:
    query = select(Run).where(Run.id == run_id, Run.tenant == tenant)
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


@dataclass(frozen=True)
class Dispatch:
    run_id: UUID
    traceparent: str | None


async def lease_due_runs() -> list[Dispatch]:
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
        return [Dispatch(run.id, run.traceparent) for run in runs]


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
            if run.cost_micros <= run.budget_micros:
                attempt.proposal = triage
        return run.cost_micros <= run.budget_micros


@dataclass(frozen=True)
class Proposal:
    sequence: int | None
    fields: dict[str, object]


async def saved_proposal(run_id: UUID) -> Proposal | None:
    async with sessions()() as session:
        run = await session.get_one(Run, run_id)
        attempt = await session.get(ModelAttempt, (run_id, run.attempts))
        if attempt is not None and attempt.proposal is not None:
            if attempt.verification is None or attempt.verification.get("accepted") is True:
                return Proposal(attempt.sequence, attempt.proposal)
        if run.triage is not None and run.verification is None:
            # In-flight runs created before this migration may already have a settled proposal.
            return Proposal(None, run.triage)
        return None


async def record_verification(
    run_id: UUID, sequence: int | None, fields: dict[str, object], review: GroundingReview
) -> None:
    report = review.model_dump(mode="json")
    async with sessions().begin() as session:
        run = await session.get_one(Run, run_id, with_for_update=True)
        if sequence is not None:
            attempt = await session.get_one(ModelAttempt, (run_id, sequence))
            attempt.verification = report
        run.verification = report
        run.triage = fields if review.accepted else None


def _set_progress(
    run: Run,
    status: RunStatus,
    triage: dict[str, object] | None,
    error: str | None,
    finished_at: datetime | None,
) -> None:
    run.status = status
    run.triage = triage
    run.error = error
    run.worker_failures = 0
    run.retry_at = None
    run.finished_at = finished_at


async def recover_committed_case(session: AsyncSession, run: Run) -> bool:
    if run.status != "running" or run.approved is not True:
        return False
    action = await session.get(CaseAction, run.id)
    if action is None or action.tenant != run.tenant:
        return False
    # Older workers could commit the case before recording terminal progress.
    _set_progress(run, "succeeded", action.payload, None, action.created_at)
    return True


async def record_progress(
    run_id: UUID,
    status: RunStatus,
    triage: dict[str, object] | None,
    error: str | None,
) -> None:
    with trace_span("workflow.act") if status == "succeeded" else nullcontext():
        async with sessions().begin() as session:
            run = await session.get_one(Run, run_id, with_for_update=True)
            if run.status in TERMINAL:
                return
            if status == "succeeded":
                if run.approved is not True or triage is None:
                    raise RuntimeError("case action requires persisted approval and a proposal")
                tool = Triage.model_validate(triage).tool
                await session.execute(
                    insert(CaseAction)
                    .values(run_id=run.id, tenant=run.tenant, tool=tool, payload=triage)
                    .on_conflict_do_nothing(index_elements=[CaseAction.run_id])
                )
            finished_at = datetime.now(UTC) if status in TERMINAL else None
            _set_progress(run, status, triage, error, finished_at)
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
