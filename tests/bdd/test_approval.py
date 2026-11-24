import asyncio
from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, scenarios, then, when
from sqlalchemy import select

from verstak.api import app
from verstak.db import CaseAction, sessions
from verstak.workers import process_run

pytestmark = pytest.mark.integration
scenarios("approval.feature")


@pytest.fixture
def operator(workflow_stack: None) -> Iterator[TestClient]:
    with TestClient(app) as client:
        token = client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "operator",
                "client_secret": "workflow-fixture-secret",
            },
        )
        assert token.status_code == 200, token.text
        client.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
        yield client


@given("a run is waiting for human approval", target_fixture="run_id")
def waiting(operator: TestClient) -> UUID:
    response = operator.post(
        "/workflows/triage/runs",
        headers={"Idempotency-Key": "denial"},
        json={"text": "My support ticket is still open."},
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["id"])
    asyncio.run(process_run(run_id))
    assert operator.get(f"/runs/{run_id}").json()["status"] == "waiting_approval"
    return run_id


@when("the reviewer denies the action")
def deny(operator: TestClient, run_id: UUID) -> None:
    response = operator.post(f"/approvals/{run_id}", json={"approved": False})
    assert response.status_code == 200, response.text
    asyncio.run(process_run(run_id))


@then("the run is rejected without a business action")
def rejected(operator: TestClient, run_id: UUID) -> None:
    async def recorded() -> bool:
        async with sessions()() as session:
            return (
                await session.scalar(select(CaseAction).where(CaseAction.run_id == run_id))
                is not None
            )

    assert operator.get(f"/runs/{run_id}").json()["status"] == "rejected"
    assert not asyncio.run(recorded())
