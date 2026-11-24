# Distributed traces

Enable the optional OpenTelemetry exporter and the local Jaeger collector after copying `.env.example` to `.env`.

```bash
docker compose -f docker-compose.yml -f compose.tracing.yml up --build -d
```

Submit and approve a fresh run using the README walkthrough. In the local Jaeger UI, look under `verstak-api` for `POST /workflows/triage/runs` and `POST /approvals/{run_id}`. Allow time for batch export. [Run walkthrough](../README.md#submit-and-approve-a-run) · [Local Jaeger](http://localhost:16686)

The submission trace spans the API, `workflow.dispatch`, `workflow.execute`, and the workflow stages reached before review. The approval starts a second phase with a span link to the submission and its own dispatch and execution spans. The API persists a bounded W3C `traceparent` with each runnable phase, and Celery propagates it into the consumer task. Worker logs include trace and span IDs when a span is active.

API traces emit the HTTP method, route template and response status. The application omits payloads, credentials, raw URLs, query strings, baggage and exception messages from traces.

For a deployed collector, set `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to its full OTLP/HTTP URL, such as `https://collector.example/v1/traces`, in both the API and worker processes. The SDK also accepts `OTEL_EXPORTER_OTLP_ENDPOINT` as a base URL. Export stays disabled without an endpoint and can be switched off with `OTEL_SDK_DISABLED=true`. The local Jaeger overlay binds to loopback ports `16686` and `4318` and keeps up to 10,000 traces in memory. `VERSTAK_JAEGER_PORT` and `VERSTAK_OTLP_PORT` override the host ports. [OpenTelemetry exporter configuration](https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/)

Run the tracing smoke test against the local overlay to check both parent chains, the approval link, and trace data minimization.

```bash
VERSTAK_E2E_BASE_URL=http://localhost:8000 \
  VERSTAK_TRACE_JAEGER_URL=http://localhost:16686 \
  uv run pytest tests/e2e/test_tracing.py
```
