import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Lock

from fastapi import FastAPI
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import (
    Link,
    Span,
    SpanKind,
    StatusCode,
    get_current_span,
    get_tracer,
    get_tracer_provider,
    set_tracer_provider,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import Counter, Histogram
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_REQUESTS = Counter("verstak_http_requests", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "verstak_http_request_duration_seconds", "HTTP request duration", ["method", "route"]
)
_TRACE_LOCK = Lock()
_W3C = TraceContextTextMapPropagator()
_TRACEPARENT = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_METHODS = {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ("run_id", "outcome", "error_type"):
            if field in record.__dict__:
                fields[field] = record.__dict__[field]
        span = get_current_span().get_span_context()
        if span.is_valid:
            fields["trace_id"] = f"{span.trace_id:032x}"
            fields["span_id"] = f"{span.span_id:016x}"
        return json.dumps(fields)


def configure_logging() -> None:
    logger = logging.getLogger("verstak")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def configure_tracing(service_name: str) -> bool:
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true" or not (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    ):
        return False
    with _TRACE_LOCK:
        if not isinstance(get_tracer_provider(), TracerProvider):
            # A fixed resource avoids exporting command-line arguments and host attributes.
            provider = TracerProvider(resource=Resource({"service.name": service_name}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            set_tracer_provider(provider)
    return True


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    _W3C.inject(carrier)
    return carrier.get("traceparent")


def _parent_context(traceparent: str) -> Context:
    if _TRACEPARENT.fullmatch(traceparent) is None:
        return Context()
    return _W3C.extract({"traceparent": traceparent})


@contextmanager
def trace_span(
    name: str,
    *,
    parent: str | None = None,
    link: str | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
) -> Iterator[Span]:
    context = _parent_context(parent) if parent is not None else None
    links = []
    if link is not None:
        linked = get_current_span(_parent_context(link)).get_span_context()
        if linked.is_valid:
            links.append(Link(linked))
    with get_tracer("verstak").start_as_current_span(
        name,
        context=context,
        kind=kind,
        links=links,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except Exception:
            span.set_status(StatusCode.ERROR)
            raise


class _TraceHTTP:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in {"/healthz", "/metrics"}:
            await self.app(scope, receive, send)
            return
        method = scope["method"] if scope["method"] in _METHODS else "_OTHER"
        parent = Headers(scope=scope).get("traceparent", "")
        with trace_span(f"HTTP {method}", parent=parent, kind=SpanKind.SERVER) as span:
            span.set_attribute("http.request.method", method)

            async def send_response(message: Message) -> None:
                if message["type"] == "http.response.start":
                    status: int = message["status"]
                    span.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        span.set_status(StatusCode.ERROR)
                await send(message)

            try:
                await self.app(scope, receive, send_response)
            finally:
                route = getattr(scope.get("route"), "path", None)
                if isinstance(route, str):
                    span.set_attribute("http.route", route)
                    span.update_name(f"{method} {route}")


def instrument_api(app: FastAPI) -> None:
    if configure_tracing("verstak-api"):
        app.add_middleware(_TraceHTTP)
