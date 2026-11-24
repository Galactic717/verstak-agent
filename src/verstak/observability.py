import json
import logging
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter("verstak_http_requests", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "verstak_http_request_duration_seconds", "HTTP request duration", ["method", "route"]
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ("run_id", "outcome", "error_type"):
            if field in record.__dict__:
                fields[field] = record.__dict__[field]
        try:
            from opentelemetry.trace import get_current_span

            span = get_current_span().get_span_context()
            if span.is_valid:
                fields["trace_id"] = f"{span.trace_id:032x}"
                fields["span_id"] = f"{span.span_id:016x}"
        except ImportError:
            pass
        return json.dumps(fields)


def configure_logging() -> None:
    logger = logging.getLogger("verstak")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
