import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Celery
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from opentelemetry.trace import SpanKind
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from verstak.agents import WorkflowState, build_graph, checkpoint_state
from verstak.config import settings
from verstak.db import DeadLetter, Run, engine, sessions
from verstak.documents import document_context, search_context
from verstak.llm import Router, TransientModelError
from verstak.observability import (
    configure_logging,
    configure_tracing,
    current_traceparent,
    trace_span,
)
from verstak.store import (
    RUNNABLE,
    lease_due_runs,
    record_progress,
    record_worker_failure,
    recover_committed_case,
)

configure_logging()
logger = logging.getLogger(__name__)
config = settings()
app = Celery("verstak", broker=config.redis_url)
app.conf.update(
    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 300},
    beat_schedule={
        "dispatch-due-runs": {
            "task": "verstak.dispatch",
            "schedule": config.dispatch_seconds,
            "options": {"expires": config.dispatch_seconds * 2},
        }
    },
)


async def _begin_run(run_id: UUID) -> Run | None:
    async with sessions().begin() as session:
        run = await session.get(Run, run_id, with_for_update=True)
        if run is None or run.status not in RUNNABLE:
            return None
        now = datetime.now(UTC)
        if run.retry_at is not None and run.retry_at > now:
            return None
        if await recover_committed_case(session, run):
            return None
        if run.status == "running" and run.error is None:
            run.worker_failures += 1
        if run.worker_failures > settings().worker_retries:
            run.status = "failed"
            run.error = "worker_loss"
            run.finished_at = now
            session.add(DeadLetter(run_id=run.id, error=run.error))
            return None
        run.status = "running"
        run.error = None
        run.retry_at = None
        return run


async def _inputs(run: Run) -> WorkflowState:
    async with sessions()() as session:
        document = ""
        if run.document_id is not None:
            document = await document_context(session, run.tenant, run.document_id) or ""
        query = (run.input_text or document)[:2_000]
        context = await search_context(session, run.tenant, query)
    return {
        "run_id": str(run.id),
        "tenant": run.tenant,
        "text": (run.input_text + "\n" + document).strip()[:16_000],
        "context": context[:8_000],
    }


async def _execute(run: Run) -> None:
    cfg: RunnableConfig = {"configurable": {"thread_id": str(run.id)}, "recursion_limit": 12}
    async with AsyncPostgresSaver.from_conn_string(settings().database_url) as saver:
        graph = build_graph(saver, Router.from_env())
        snapshot = await graph.aget_state(cfg)
        if not snapshot.values:
            state = checkpoint_state(
                await graph.ainvoke(await _inputs(run), cfg, durability="sync")
            )
        elif any(task.interrupts for task in snapshot.tasks):
            if run.approved is None:
                state = checkpoint_state(snapshot.values)
            else:
                state = checkpoint_state(
                    await graph.ainvoke(Command(resume=run.approved), cfg, durability="sync")
                )
        elif snapshot.next:
            state = checkpoint_state(await graph.ainvoke(None, cfg, durability="sync"))
        else:
            state = checkpoint_state(snapshot.values)

    checkpoint_outcome = state.get("outcome")
    outcome = "succeeded" if checkpoint_outcome == "case_pending" else checkpoint_outcome
    triage = state.get("triage") if outcome != "failed" else None
    if outcome is None and triage is None:
        raise RuntimeError("incomplete workflow checkpoint")
    await record_progress(run.id, outcome or "waiting_approval", triage, state.get("error"))
    logger.info("workflow checkpoint applied", extra={"run_id": str(run.id), "outcome": outcome})


async def process_run(run_id: UUID) -> None:
    # Keep a session-level lock on a dedicated connection across graph and effect transactions.
    # Closing the connection releases it if the process exits mid-run.
    async with engine().connect() as connection:
        acquired = await connection.scalar(
            text("select pg_try_advisory_lock(hashtextextended(:id, 0))"), {"id": str(run_id)}
        )
        if not acquired:
            return
        await connection.commit()
        run = await _begin_run(run_id)
        if run is None:
            return
        try:
            await _execute(run)
        except TransientModelError:
            await record_worker_failure(run.id, "provider_unavailable")
        except SQLAlchemyError:
            # The existing lease becomes eligible again once PostgreSQL recovers.
            raise
        except Exception as exc:
            logger.error(
                "workflow execution failed",
                extra={"run_id": str(run.id), "error_type": type(exc).__name__},
            )
            await record_worker_failure(run.id, "execution_error")


def _dispatch() -> None:
    configure_tracing("verstak-worker")
    for dispatch in asyncio.run(lease_due_runs()):
        with trace_span("workflow.dispatch", parent=dispatch.traceparent, kind=SpanKind.PRODUCER):
            app.send_task(
                "verstak.execute",
                args=[str(dispatch.run_id)],
                kwargs={"traceparent": current_traceparent()},
            )


def _process(run_id: str, traceparent: str | None = None) -> None:
    configure_tracing("verstak-worker")
    with trace_span("workflow.execute", parent=traceparent, kind=SpanKind.CONSUMER):
        asyncio.run(process_run(UUID(run_id)))


dispatch_task: Any = app.task(name="verstak.dispatch")(_dispatch)
execute_task: Any = app.task(name="verstak.execute")(_process)
