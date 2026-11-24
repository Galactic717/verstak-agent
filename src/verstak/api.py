from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import extract, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from verstak.auth import Principal, require
from verstak.auth import router as auth_router
from verstak.config import settings
from verstak.contracts import ApprovalRequest, EvalReport, RunReceipt, RunRequest, RunView
from verstak.db import Evaluation, Run, get_session
from verstak.documents import document_context
from verstak.documents import router as document_router
from verstak.limits import retry_after
from verstak.observability import HTTP_LATENCY, HTTP_REQUESTS, configure_logging
from verstak.store import tenant_run

configure_logging()
app = FastAPI(title="verstak-agent", version="0.1.0")
app.include_router(auth_router)
app.include_router(document_router)
FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,metrics")

Session = Annotated[AsyncSession, Depends(get_session)]
Reader = Annotated[Principal, Depends(require("runs:read"))]
Writer = Annotated[Principal, Depends(require("runs:write"))]
Reviewer = Annotated[Principal, Depends(require("approvals:write"))]
EvalReader = Annotated[Principal, Depends(require("evals:read"))]
EvalWriter = Annotated[Principal, Depends(require("evals:write"))]
RunKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[!-~]+$")
]
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}


async def _rate_limit(principal: Principal) -> None:
    try:
        wait = await retry_after(f"{principal.tenant}:{principal.subject}")
    except RedisError:
        raise HTTPException(503, "Rate limiter unavailable") from None
    if wait:
        raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": str(wait)})


@app.middleware("http")
async def measure(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path in {"/healthz", "/metrics"}:
        return await call_next(request)
    start = perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        method = request.method if request.method in HTTP_METHODS else "OTHER"
        HTTP_REQUESTS.labels(method, route, str(status)).inc()
        HTTP_LATENCY.labels(method, route).observe(perf_counter() - start)


@app.get("/healthz", tags=["system"])
async def health(session: Session) -> dict[str, str]:
    try:
        await session.execute(text("select 1"))
        async with Redis.from_url(settings().redis_url, socket_connect_timeout=2) as redis:
            await redis.ping()
    except (SQLAlchemyError, RedisError):
        raise HTTPException(503, "A required service is unavailable") from None
    return {"status": "ok"}


@app.get("/workflows", tags=["workflows"])
async def workflows(_: Reader) -> list[dict[str, str]]:
    return [{"id": "triage", "name": "Business request triage"}]


@app.post("/workflows/triage/runs", status_code=202, tags=["runs"])
async def submit(
    body: RunRequest, key: RunKey, principal: Writer, session: Session
) -> RunView | RunReceipt:
    await _rate_limit(principal)
    fingerprint = body.fingerprint()
    async with session.begin():
        if body.document_id is not None:
            if await document_context(session, principal.tenant, body.document_id) is None:
                raise HTTPException(404, "Document not found")
        new_id = uuid4()
        returned = await session.scalar(
            insert(Run)
            .values(
                id=new_id,
                tenant=principal.tenant,
                submitted_by=principal.subject,
                idempotency_key=key,
                request_hash=fingerprint,
                input_text=body.text,
                document_id=body.document_id,
                max_steps=body.max_steps,
                budget_micros=body.budget_micros,
            )
            .on_conflict_do_nothing(constraint="uq_run_idempotency")
            .returning(Run)
        )
        run = returned or await session.scalar(
            select(Run).where(Run.tenant == principal.tenant, Run.idempotency_key == key)
        )
        if run is None:
            raise HTTPException(503, "Submission could not be resolved")
        if run.request_hash != fingerprint:
            raise HTTPException(409, "Idempotency key was used for a different request")
    if "runs:read" not in principal.scopes:
        return RunReceipt(id=run.id)
    return RunView.model_validate(run)


@app.get("/runs", tags=["runs"])
async def list_runs(
    principal: Reader, session: Session, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[RunView]:
    query = select(Run).where(Run.tenant == principal.tenant).order_by(Run.created_at.desc())
    return [RunView.model_validate(run) for run in await session.scalars(query.limit(limit))]


@app.get("/runs/{run_id}", tags=["runs"])
async def get_run(run_id: UUID, principal: Reader, session: Session) -> RunView:
    run = await tenant_run(session, principal.tenant, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return RunView.model_validate(run)


@app.get("/approvals", tags=["approvals"])
async def pending_approvals(principal: Reviewer, session: Session) -> list[RunView]:
    query = (
        select(Run)
        .where(Run.tenant == principal.tenant, Run.status == "waiting_approval")
        .order_by(Run.created_at)
        .limit(100)
    )
    return [RunView.model_validate(run) for run in await session.scalars(query)]


@app.post("/approvals/{run_id}", tags=["approvals"])
async def decide(
    run_id: UUID, body: ApprovalRequest, principal: Reviewer, session: Session
) -> RunView:
    await _rate_limit(principal)
    async with session.begin():
        run = await tenant_run(session, principal.tenant, run_id, lock=True)
        if run is None:
            raise HTTPException(404, "Run not found")
        if run.approved is not None:
            if run.approved != body.approved:
                raise HTTPException(409, "Approval was already decided")
            return RunView.model_validate(run)
        if run.status != "waiting_approval":
            raise HTTPException(409, "Run is not waiting for approval")
        run.approved = body.approved
        run.approved_by = principal.subject
        run.approved_at = datetime.now(UTC)
        run.dispatch_after = run.approved_at
        run.status = "queued"
        await session.flush()
        await session.refresh(run)
        return RunView.model_validate(run)


class EvalView(BaseModel):
    runs_last_24h: int
    statuses: dict[str, int]
    model_cost_usd: Decimal
    p95_run_latency_ms: float | None
    latest: EvalReport | None


async def _eval_view(session: AsyncSession, tenant: str | None = None) -> EvalView:
    cutoff = datetime.now(UTC) - timedelta(days=1)
    filters = [Run.created_at >= cutoff]
    latest = select(Evaluation.report).order_by(Evaluation.created_at.desc()).limit(1)
    if tenant is not None:
        filters.append(Run.tenant == tenant)
        latest = latest.where(Evaluation.tenant == tenant)
    grouped = await session.execute(
        select(Run.status, func.count(Run.id), func.sum(Run.cost_micros))
        .where(*filters)
        .group_by(Run.status)
    )
    statuses: dict[str, int] = {}
    total_micros = 0
    for status, count, cost in grouped:
        statuses[status] = count
        total_micros += int(cost or 0)
    p95 = await session.scalar(
        select(
            func.percentile_cont(0.95).within_group(
                extract("epoch", Run.finished_at - Run.created_at)
            )
        ).where(*filters, Run.finished_at.is_not(None))
    )
    report = await session.scalar(latest)
    return EvalView(
        runs_last_24h=sum(statuses.values()),
        statuses=statuses,
        model_cost_usd=Decimal(total_micros) / 1_000_000,
        p95_run_latency_ms=float(p95) * 1_000 if p95 is not None else None,
        latest=EvalReport.model_validate(report) if report is not None else None,
    )


@app.get("/evals", tags=["evaluations"])
async def read_evaluations(principal: EvalReader, session: Session) -> EvalView:
    return await _eval_view(session, principal.tenant)


@app.post("/evals", status_code=201, tags=["evaluations"])
async def record_evaluation(
    report: EvalReport, principal: EvalWriter, session: Session
) -> EvalReport:
    async with session.begin():
        session.add(
            Evaluation(id=uuid4(), tenant=principal.tenant, report=report.model_dump(mode="json"))
        )
    return report


@app.get("/metrics", include_in_schema=False)
async def metrics(session: Session) -> Response:
    registry = CollectorRegistry()
    report = await _eval_view(session)
    runs = Gauge(
        "verstak_runs", "Runs submitted in the last 24 hours", ["status"], registry=registry
    )
    for status, count in report.statuses.items():
        runs.labels(status).set(count)
    Gauge(
        "verstak_model_cost_usd",
        "Model cost and unsettled reservations in USD over 24 hours",
        registry=registry,
    ).set(float(report.model_cost_usd))
    if report.p95_run_latency_ms is not None:
        Gauge(
            "verstak_run_latency_seconds",
            "P95 elapsed run time including approval",
            registry=registry,
        ).set(report.p95_run_latency_ms / 1000)
    if report.latest is not None:
        Gauge("verstak_eval_tool_f1", "Latest evaluation tool-call F1", registry=registry).set(
            report.latest.tool_f1
        )
        Gauge(
            "verstak_eval_schema_validity_ratio",
            "Latest evaluation schema validity",
            registry=registry,
        ).set(report.latest.schema_validity)
        Gauge(
            "verstak_eval_latency_seconds",
            "Latest evaluation P95 model-call latency",
            registry=registry,
        ).set(report.latest.p95_latency_ms / 1000)
    return Response(generate_latest() + generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
