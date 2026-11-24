import json
import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from verstak import observability


@pytest.fixture
def traces(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "get_tracer", provider.get_tracer)
    try:
        yield exporter
    finally:
        provider.shutdown()


def test_trace_context_crosses_dispatch_and_links_approval(traces: InMemorySpanExporter) -> None:
    assert observability.current_traceparent() is None
    with observability.trace_span("submit"):
        queued = observability.current_traceparent()
        assert queued is not None and len(queued) == 55
    with observability.trace_span("dispatch", parent=queued, kind=SpanKind.PRODUCER):
        delivery = observability.current_traceparent()
    with observability.trace_span("execute", parent=delivery, kind=SpanKind.CONSUMER):
        record = logging.makeLogRecord({"msg": "checkpoint saved"})
        log = json.loads(observability.JsonFormatter().format(record))
        assert log["trace_id"] == queued.split("-")[1]
    with observability.trace_span("approve", link=queued) as span:
        assert span.get_span_context().trace_id != int(queued.split("-")[1], 16)
    submit, dispatch, execute, approve = traces.get_finished_spans()
    assert submit.context is not None and dispatch.context is not None
    assert dispatch.parent is not None and dispatch.parent.is_remote
    assert dispatch.parent.trace_id == submit.context.trace_id
    assert dispatch.parent.span_id == submit.context.span_id
    assert execute.parent is not None and execute.parent.is_remote
    assert execute.parent.trace_id == dispatch.context.trace_id
    assert execute.parent.span_id == dispatch.context.span_id
    assert [link.context.trace_id for link in approve.links] == [submit.context.trace_id]
    assert [link.context.span_id for link in approve.links] == [submit.context.span_id]


def test_http_span_contains_route_template_and_status_only(
    monkeypatch: pytest.MonkeyPatch, traces: InMemorySpanExporter
) -> None:
    monkeypatch.setattr(observability, "configure_tracing", lambda _service: True)
    app = FastAPI()
    observability.instrument_api(app)

    @app.post("/runs/{run_id}", status_code=202)
    def handle(run_id: str) -> dict[str, str]:
        return {"id": run_id}

    @app.get("/healthz")
    def health() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/healthz")
        response = client.post(
            "/runs/private-id?query=private-query",
            headers={
                "Authorization": "Bearer private-token",
                "traceparent": "00-00000000000000000000000000000011-0000000000000022-01",
                "tracestate": "vendor=private-tracestate",
                "baggage": "customer=private-baggage",
            },
            json={"text": "private-text"},
        )
    assert response.status_code == 202
    (span,) = traces.get_finished_spans()
    assert span.name == "POST /runs/{run_id}"
    assert span.parent is not None and span.parent.span_id == 0x22
    assert span.context is not None and span.context.trace_id == 0x11
    assert not span.parent.trace_state
    assert dict(span.attributes or {}) == {
        "http.request.method": "POST",
        "http.route": "/runs/{run_id}",
        "http.response.status_code": 202,
    }


def test_invalid_parent_and_failed_span_export_no_exception_payload(
    traces: InMemorySpanExporter,
) -> None:
    with (
        pytest.raises(RuntimeError),
        observability.trace_span(
            "workflow.execute", parent="untrusted value", link="invalid value"
        ),
    ):
        raise RuntimeError("sensitive error")
    (span,) = traces.get_finished_spans()
    assert span.parent is None and not span.links
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert not span.events


def test_tracing_disabled_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert observability.configure_tracing("verstak-test") is False
    app = FastAPI()
    observability.instrument_api(app)
    assert app.user_middleware == []


def test_installs_one_exporter_provider_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    providers: list[TracerProvider] = []
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    monkeypatch.setattr(observability, "set_tracer_provider", providers.append)
    monkeypatch.setattr(observability, "get_tracer_provider", lambda: next(iter(providers), None))
    try:
        assert observability.configure_tracing("verstak-test") is True
        assert observability.configure_tracing("verstak-test") is True
        (provider,) = providers
        assert dict(provider.resource.attributes) == {"service.name": "verstak-test"}
    finally:
        for provider in providers:
            provider.shutdown()
