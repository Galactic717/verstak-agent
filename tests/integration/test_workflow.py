import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from verstak.api import app
from verstak.config import settings
from verstak.db import CaseAction, DeadLetter, Run, sessions
from verstak.llm import InvalidModelOutput, Router, TransientModelError
from verstak.store import lease_due_runs, record_progress, settle_attempt
from verstak.workers import process_run

pytestmark = pytest.mark.integration
INVOICE = "Finance request. Invoice INV-1042 for USD 120.00 needs review."


@pytest.fixture
async def client(workflow_stack: None) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://workflow.test"
    ) as http:
        http.headers.update(await authorization(http))
        yield http


async def authorization(client: httpx.AsyncClient, subject: str = "operator") -> dict[str, str]:
    response = await client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": subject,
            "client_secret": "workflow-fixture-secret",
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def start(client: httpx.AsyncClient, **fields: object) -> UUID:
    response = await client.post(
        "/workflows/triage/runs",
        headers={"Idempotency-Key": "first"},
        json={"text": INVOICE, **fields},
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["id"])


async def status(client: httpx.AsyncClient, run_id: UUID) -> dict:
    response = await client.get(f"/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def action_count(run_id: UUID) -> int:
    async with sessions()() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(CaseAction).where(CaseAction.run_id == run_id)
            )
        )


async def expire_backoff(run_id: UUID) -> None:
    async with sessions().begin() as session:
        run = await session.get_one(Run, run_id)
        run.retry_at = None


async def test_idempotency_conflict_rbac_and_tenant_boundary(client: httpx.AsyncClient) -> None:
    run_ids = await asyncio.gather(*(start(client) for _ in range(3)))
    assert len(set(run_ids)) == 1
    run_id = run_ids[0]
    conflicting = await client.post(
        "/workflows/triage/runs", headers={"Idempotency-Key": "first"}, json={"text": "different"}
    )
    assert conflicting.status_code == 409
    reader = await authorization(client, "reader")
    external = await authorization(client, "external")
    assert (await client.get(f"/runs/{run_id}", headers=reader)).status_code == 200
    assert (await client.get(f"/runs/{run_id}", headers=external)).status_code == 404
    assert (
        await client.post(f"/approvals/{run_id}", headers=reader, json={"approved": True})
    ).status_code == 403
    assert (await client.post(f"/approvals/{run_id}", json={"approved": True})).status_code == 409


async def test_file_only_run_read_views_and_write_only_replay(client: httpx.AsyncClient) -> None:
    upload = await client.post(
        "/documents",
        files={"file": ("synthetic.csv", b"invoice,currency\nINV-918,USD\n", "text/csv")},
    )
    assert upload.status_code in (200, 201), upload.text
    document_id = upload.json()["id"]
    sender = await authorization(client, "sender")
    headers = {**sender, "Idempotency-Key": "file"}
    submit = "/workflows/triage/runs"
    result = await client.post(submit, headers=headers, json={"document_id": document_id})
    assert result.status_code == 202, result.text
    run_id = UUID(result.json()["id"])
    assert result.json() == {"id": str(run_id)}
    await process_run(run_id)
    assert (await status(client, run_id))["status"] == "waiting_approval"
    replay = await client.post(submit, headers=headers, json={"document_id": document_id})
    assert replay.json() == {"id": str(run_id)}
    assert (await client.get(f"/runs/{run_id}", headers=sender)).status_code == 403
    missing = await client.post(
        submit, headers={"Idempotency-Key": "missing"}, json={"document_id": str(uuid4())}
    )
    assert missing.status_code == 404
    assert (await client.get("/workflows")).json()[0]["id"] == "triage"
    assert (await client.get("/runs")).json()[0]["id"] == str(run_id)
    assert (await client.get("/approvals")).json()[0]["id"] == str(run_id)
    assert (await client.get("/healthz")).json()["status"] == "ok"


async def test_dispatch_leases_and_redis_quota(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERSTAK_RATE_CAPACITY", "1")
    monkeypatch.setenv("VERSTAK_RATE_PER_MINUTE", "1")
    settings.cache_clear()
    run_id = await start(client)
    response = await client.post(
        "/workflows/triage/runs", headers={"Idempotency-Key": "quota"}, json={"text": INVOICE}
    )
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert run_id in {dispatch.run_id for dispatch in await lease_due_runs()}
    assert run_id not in {dispatch.run_id for dispatch in await lease_due_runs()}


@pytest.mark.chaos
@pytest.mark.parametrize("after_commit", [False, True], ids=["before-commit", "after-commit"])
@pytest.mark.parametrize("interruption", range(5))
async def test_case_and_status_commit_together_across_worker_failures(
    client: httpx.AsyncClient, interruption: int, after_commit: bool
) -> None:
    run_id = await start(client)
    await process_run(run_id)
    waiting = await status(client, run_id)
    assert waiting["status"] == "waiting_approval", waiting
    assert waiting["triage"]["tool"] == "record_invoice"
    assert waiting["attempts"] == 1
    assert await action_count(run_id) == 0
    decision = await client.post(f"/approvals/{run_id}", json={"approved": True})
    assert decision.status_code == 200, decision.text

    async def interrupt_finalization(*args: object, **kwargs: object) -> None:
        if after_commit:
            await record_progress(*args, **kwargs)
        if interruption % 2:
            raise OperationalError("connection lost", None, OSError("database interrupted"))
        raise RuntimeError("injected worker interruption at case finalization")

    with patch("verstak.workers.record_progress", side_effect=interrupt_finalization):
        if interruption % 2:
            with pytest.raises(OperationalError):
                await process_run(run_id)
        else:
            await process_run(run_id)
    assert (await status(client, run_id))["status"] == ("succeeded" if after_commit else "running")
    assert await action_count(run_id) == int(after_commit)
    if not after_commit:
        async with AsyncPostgresSaver.from_conn_string(settings().database_url) as saver:
            checkpoint = await saver.aget({"configurable": {"thread_id": str(run_id)}})
        assert checkpoint is not None
        assert checkpoint["channel_values"]["outcome"] == "case_pending"

    await expire_backoff(run_id)
    await asyncio.gather(process_run(run_id), process_run(run_id))
    finished = await status(client, run_id)
    assert finished["status"] == "succeeded", finished
    assert finished["attempts"] == 1
    assert await action_count(run_id) == 1
    assert (await client.post(f"/approvals/{run_id}", json={"approved": True})).status_code == 200
    assert (await client.post(f"/approvals/{run_id}", json={"approved": False})).status_code == 409


async def test_existing_case_is_recovered_when_the_worker_retry_limit_is_reached(
    client: httpx.AsyncClient,
) -> None:
    run_id = await start(client)
    await process_run(run_id)
    proposal = (await status(client, run_id))["triage"]
    decision = await client.post(f"/approvals/{run_id}", json={"approved": True})
    assert decision.status_code == 200, decision.text

    async with sessions().begin() as session:
        run = await session.get_one(Run, run_id)
        run.status = "running"
        run.worker_failures = settings().worker_retries
        session.add(
            CaseAction(run_id=run_id, tenant=run.tenant, tool=proposal["tool"], payload=proposal)
        )

    await process_run(run_id)

    finished = await status(client, run_id)
    assert finished["status"] == "succeeded", finished
    assert finished["triage"] == proposal
    assert await action_count(run_id) == 1
    async with sessions()() as session:
        assert await session.get(DeadLetter, run_id) is None


@pytest.mark.chaos
async def test_malformed_output_corrected_and_step_limit_enforced(
    client: httpx.AsyncClient,
) -> None:
    valid = await Router.from_env().classify(INVOICE, "", "")
    run_id = await start(client, max_steps=2)
    with patch.object(
        Router, "classify", AsyncMock(side_effect=[InvalidModelOutput(), valid])
    ) as model:
        await process_run(run_id)
    assert model.await_count == 2
    assert model.await_args.args[-1]
    run = await status(client, run_id)
    assert run["status"] == "waiting_approval", run
    assert run["attempts"] == 2

    other = await client.post(
        "/workflows/triage/runs",
        headers={"Idempotency-Key": "limit"},
        json={"text": INVOICE, "max_steps": 3},
    )
    exhausted = UUID(other.json()["id"])
    with patch.object(Router, "classify", AsyncMock(side_effect=InvalidModelOutput)) as loop:
        await process_run(exhausted)
    assert loop.await_count == 3
    run = await status(client, exhausted)
    assert (run["status"], run["error"]) == ("failed", "invalid_model_output")
    async with sessions()() as session:
        assert (await session.get_one(DeadLetter, exhausted)).error == "invalid_model_output"


@pytest.mark.chaos
@pytest.mark.parametrize(
    "fault,field",
    [
        ({"reference": "INV-104"}, "reference"),
        ({"reference": "INV-10420"}, "reference"),
        ({"reference": "REQ-1042"}, "reference"),
        ({"amount_cents": 0}, "amount"),
        ({"amount_cents": 120}, "amount"),
        ({"amount_cents": 12001}, "amount"),
        ({"currency": "EUR"}, "currency"),
        ({"currency": "GBP"}, "currency"),
        ({"currency": "UAH"}, "currency"),
        ({"reference": "INV-9999", "amount_cents": 19999}, "reference"),
    ],
)
async def test_evidence_agent_requests_correction_before_human_review(
    client: httpx.AsyncClient,
    fault: dict[str, object],
    field: str,
) -> None:
    run_id = await start(client, max_steps=2)
    valid = await Router.from_env().classify(INVOICE, "", "")
    ungrounded = replace(valid, triage=valid.triage.model_copy(update=fault))
    with patch.object(Router, "classify", AsyncMock(side_effect=[ungrounded, valid])) as model:
        await process_run(run_id)
    assert model.await_count == 2
    assert field in model.await_args.args[-1]
    result = await status(client, run_id)
    assert result["status"] == "waiting_approval"
    assert result["triage"]["reference"] == "INV-1042"
    assert result["verification"] == {"accepted": True, "findings": []}
    assert await action_count(run_id) == 0


@pytest.mark.chaos
async def test_unverified_payment_cannot_reach_approval(client: httpx.AsyncClient) -> None:
    run_id = await start(client, max_steps=3)
    valid = await Router.from_env().classify(INVOICE, "", "")
    ungrounded = replace(valid, triage=valid.triage.model_copy(update={"amount_cents": 19999}))
    with patch.object(Router, "classify", AsyncMock(return_value=ungrounded)) as model:
        await process_run(run_id)
    result = await status(client, run_id)
    assert model.await_count == result["attempts"] == 3
    assert (result["status"], result["error"]) == ("failed", "ungrounded_model_output")
    assert result["triage"] is None
    assert result["verification"] == {"accepted": False, "findings": ["payment_not_in_source"]}
    assert (await client.post(f"/approvals/{run_id}", json={"approved": True})).status_code == 409
    assert await action_count(run_id) == 0


@pytest.mark.chaos
async def test_budget_rejection_happens_before_provider_call(client: httpx.AsyncClient) -> None:
    run_id = await start(client, max_budget_usd="0.000002")
    with (
        patch.object(Router, "max_cost_micros", return_value=3),
        patch.object(Router, "classify", AsyncMock()) as provider,
    ):
        await process_run(run_id)
    provider.assert_not_called()
    run = await status(client, run_id)
    assert (run["status"], run["attempts"], run["error"]) == ("failed", 0, "budget_limit")


@pytest.mark.chaos
async def test_provider_timeout_is_retried_from_checkpoint(client: httpx.AsyncClient) -> None:
    run_id = await start(client)
    with patch.object(Router, "classify", AsyncMock(side_effect=TransientModelError("timeout"))):
        await process_run(run_id)
    run = await status(client, run_id)
    assert (run["status"], run["attempts"], run["error"]) == ("running", 1, "provider_unavailable")
    with patch.object(Router, "classify", AsyncMock()) as duplicate:
        await process_run(run_id)
    duplicate.assert_not_called()
    await expire_backoff(run_id)
    await process_run(run_id)
    run = await status(client, run_id)
    assert (run["status"], run["attempts"], run["error"]) == ("waiting_approval", 2, None)


@pytest.mark.chaos
async def test_crash_after_model_settlement_uses_the_saved_result(
    client: httpx.AsyncClient,
) -> None:
    run_id = await start(client, max_steps=1)

    async def after_commit(*args: object, **kwargs: object) -> None:
        await settle_attempt(*args, **kwargs)
        raise RuntimeError("interrupted between model settlement and graph checkpoint")

    with patch("verstak.agents.settle_attempt", side_effect=after_commit):
        await process_run(run_id)
    assert (await status(client, run_id))["status"] == "running"
    await expire_backoff(run_id)
    with patch.object(Router, "classify", AsyncMock()) as provider:
        await process_run(run_id)
    provider.assert_not_called()
    run = await status(client, run_id)
    assert (run["status"], run["attempts"]) == ("waiting_approval", 1)
    assert run["triage"]["tool"] == "record_invoice"


async def test_evaluation_digest_and_metrics(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = {
        "provider": "fixture",
        "cases": 30,
        "tool_f1": 0.95,
        "schema_validity": 1,
        "grounded_accuracy": 0.96,
        "p95_latency_ms": 0.42,
        "cost_per_1000_usd": "0",
    }
    response = await client.post("/evals", json=sent)
    assert response.status_code == 201, response.text
    report = await client.get("/evals")
    assert report.status_code == 200, report.text
    assert report.json()["latest"] == sent
    response = await client.get("/metrics")
    assert response.status_code == 200, response.text
    assert "verstak_eval_tool_f1" in response.text
    assert "verstak_http_request_duration_seconds_bucket" in response.text
    monkeypatch.setenv("VERSTAK_METRICS_TOKEN", "private-monitoring-token")
    settings.cache_clear()
    assert (await client.get("/metrics")).status_code == 401
    authorized = await client.get(
        "/metrics", headers={"Authorization": "Bearer private-monitoring-token"}
    )
    assert authorized.status_code == 200
