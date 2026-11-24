import os
import time
from typing import TypedDict
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.e2e


class Reference(TypedDict):
    refType: str
    traceID: str
    spanID: str


class JaegerSpan(TypedDict):
    operationName: str
    spanID: str
    references: list[Reference]
    tags: list[dict[str, object]]


def _await_status(api: httpx.Client, run_id: str, expected: str) -> None:
    deadline = time.monotonic() + 30
    status = "unknown"
    while time.monotonic() < deadline:
        response = api.get(f"/runs/{run_id}")
        response.raise_for_status()
        status = response.json()["status"]
        if status == expected:
            return
        if status in {"succeeded", "rejected", "failed"}:
            break
        time.sleep(0.25)
    pytest.fail(f"Run finished polling in state {status!r}, expected {expected!r}")


def _await_spans(
    jaeger: httpx.Client, trace_id: str, names: set[str], private_marker: str, run_id: str
) -> dict[str, JaegerSpan]:
    deadline = time.monotonic() + 30
    by_name: dict[str, JaegerSpan] = {}
    while time.monotonic() < deadline:
        response = jaeger.get(f"/api/traces/{trace_id}")
        if response.is_success and (data := response.json()["data"]):
            spans: list[JaegerSpan] = data[0]["spans"]
            by_name = {span["operationName"]: span for span in spans}
            if names <= by_name.keys():
                assert len(spans) == len(by_name), "Duplicate operation spans in Jaeger"
                assert not any(name.startswith("fastapi.") for name in by_name)
                assert all(tag["key"] != "url.path" for span in spans for tag in span["tags"])
                assert private_marker not in response.text and run_id not in response.text
                return by_name
        time.sleep(0.25)
    pytest.fail(f"Jaeger did not receive the spans {sorted(names - by_name.keys())}")


def _has_reference(span: JaegerSpan, kind: str, trace_id: str, previous: JaegerSpan) -> bool:
    return any(
        reference["refType"] == kind
        and reference["traceID"] == trace_id
        and reference["spanID"] == previous["spanID"]
        for reference in span["references"]
    )


def test_jaeger_links_api_to_celery_across_approval() -> None:
    api_url = os.environ.get("VERSTAK_E2E_BASE_URL")
    jaeger_url = os.environ.get("VERSTAK_TRACE_JAEGER_URL")
    if not api_url or not jaeger_url:
        pytest.skip("Set VERSTAK_E2E_BASE_URL and VERSTAK_TRACE_JAEGER_URL for the tracing overlay")

    submission_trace, approval_trace, private_marker = (uuid4().hex for _ in range(3))
    with httpx.Client(base_url=api_url, timeout=5) as api:
        token = api.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.environ.get("VERSTAK_E2E_CLIENT_ID", "demo"),
                "client_secret": os.environ.get("VERSTAK_E2E_CLIENT_SECRET", "verstak-demo-secret"),
            },
        )
        token.raise_for_status()
        api.headers["Authorization"] = "Bearer " + token.json()["access_token"]
        submission = api.post(
            "/workflows/triage/runs",
            params={"private_query": private_marker},
            headers={
                "Idempotency-Key": f"trace-{private_marker}",
                "Traceparent": f"00-{submission_trace}-0000000000000011-01",
            },
            json={"text": f"Please record invoice INV-54321 for USD 120.25. Ref {private_marker}"},
        )
        submission.raise_for_status()
        run_id = submission.json()["id"]
        _await_status(api, run_id, "waiting_approval")
        decision = api.post(
            f"/approvals/{run_id}",
            headers={"Traceparent": f"00-{approval_trace}-0000000000000022-01"},
            json={"approved": True},
        )
        decision.raise_for_status()
        _await_status(api, run_id, "succeeded")

    with httpx.Client(base_url=jaeger_url, timeout=5) as jaeger:
        submit_server = "POST /workflows/triage/runs"
        approve_server = "POST /approvals/{run_id}"
        submit = _await_spans(
            jaeger,
            submission_trace,
            {
                submit_server,
                "workflow.dispatch",
                "workflow.execute",
                "workflow.classify",
                "workflow.verify",
            },
            private_marker,
            run_id,
        )
        approve = _await_spans(
            jaeger,
            approval_trace,
            {
                approve_server,
                "workflow.approval",
                "workflow.dispatch",
                "workflow.execute",
                "workflow.act",
            },
            private_marker,
            run_id,
        )

    assert _has_reference(
        submit["workflow.dispatch"], "CHILD_OF", submission_trace, submit[submit_server]
    )
    assert _has_reference(
        submit["workflow.execute"], "CHILD_OF", submission_trace, submit["workflow.dispatch"]
    )
    assert _has_reference(
        approve["workflow.dispatch"], "CHILD_OF", approval_trace, approve["workflow.approval"]
    )
    assert _has_reference(
        approve["workflow.execute"], "CHILD_OF", approval_trace, approve["workflow.dispatch"]
    )
    assert _has_reference(
        approve["workflow.approval"], "FOLLOWS_FROM", submission_trace, submit[submit_server]
    )
