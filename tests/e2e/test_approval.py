import os
import time
from uuid import uuid4

import pytest
from playwright.sync_api import APIRequestContext, Page, expect

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def stack_url() -> str:
    url = os.environ.get("VERSTAK_E2E_BASE_URL")
    if not url:
        pytest.skip("VERSTAK_E2E_BASE_URL must point to a running demo stack")
    return url.rstrip("/")


def wait_for_status(
    request: APIRequestContext,
    url: str,
    headers: dict[str, str],
    expected: str,
) -> None:
    deadline = time.monotonic() + 45
    status = "unknown"
    while time.monotonic() < deadline:
        response = request.get(url, headers=headers, timeout=5000)
        assert response.ok, response.text()
        status = response.json()["status"]
        if status == expected:
            return
        if status in {"succeeded", "rejected", "failed"}:
            break
        time.sleep(0.25)
    pytest.fail(f"Run finished polling in state {status!r}, expected {expected!r}")


@pytest.mark.parametrize(("approved", "outcome"), [(True, "succeeded"), (False, "rejected")])
def test_triage_approval_from_running_stack(
    stack_url: str, page: Page, approved: bool, outcome: str
) -> None:
    page.goto(f"{stack_url}/docs")
    expect(page.locator(".swagger-ui .info .title")).to_contain_text("Verstak", ignore_case=True)
    expect(page.locator('[data-path="/workflows/triage/runs"]')).to_be_visible()

    token = page.request.post(
        f"{stack_url}/oauth/token",
        form={
            "grant_type": "client_credentials",
            "client_id": os.environ.get("VERSTAK_E2E_CLIENT_ID", "demo"),
            "client_secret": os.environ.get("VERSTAK_E2E_CLIENT_SECRET", "verstak-demo-secret"),
        },
    )
    assert token.ok, token.text()
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    submission_headers = {**headers, "Idempotency-Key": f"playwright-{uuid4()}"}
    body = {"text": "Please record invoice INV-2026-014 for USD 350.00."}
    url = f"{stack_url}/workflows/triage/runs"
    submission = page.request.post(url, headers=submission_headers, data=body)
    assert submission.ok, submission.text()
    run_id = submission.json()["id"]

    repeated = page.request.post(url, headers=submission_headers, data=body)
    assert repeated.ok, repeated.text()
    assert repeated.json()["id"] == run_id

    run_url = f"{stack_url}/runs/{run_id}"
    wait_for_status(page.request, run_url, headers, "waiting_approval")
    decision = page.request.post(
        f"{stack_url}/approvals/{run_id}", headers=headers, data={"approved": approved}
    )
    assert decision.ok, decision.text()
    wait_for_status(page.request, run_url, headers, outcome)
