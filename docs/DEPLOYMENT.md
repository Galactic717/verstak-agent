# Hosted demo on Render

The [Render Blueprint](../render.yaml) runs the FastAPI control plane, Celery worker and Celery Beat as separate Docker services in Frankfurt. PostgreSQL 16 stores runs, approvals, actions and LangGraph checkpoints. Render Key Value provides the Redis broker and rate limiter. Both backing stores use private connection strings. The worker uses the deterministic fixture model.

The Blueprint uses three `0.5c-512mb` service instances, one `256mb` Key Value instance, and PostgreSQL on `0.1c-256mb` with a 1 GB disk. Render's published rates put that configuration at about $37.30 per month before workspace or usage charges. Review the estimate in the Render Dashboard before applying the Blueprint. [Compute plans](https://render.com/docs/compute-plans), [Render's pricing example](https://render.com/articles/production-rails-hosting-guide#how-do-rails-hosting-costs-compare-across-platform-alternatives), [Render pricing](https://render.com/pricing).

## Validate the Blueprint

The [Blueprint JSON Schema](https://render.com/schema/render.yaml.json) supports account-free structural validation in a YAML editor. To also check the branch, plans and resource names in your Render workspace, use an authenticated Render CLI session. [Blueprint validation](https://render.com/docs/blueprint-spec#validating-blueprints).

```sh
render blueprints validate render.yaml
```

## Create the deployment

1. In the Render Dashboard, select **New → Blueprint**, link the repository and choose the checked release branch. Use `render.yaml` as the Blueprint path.
2. Generate two independent client secrets and paste the single JSON line below into the API service's `VERSTAK_CLIENTS_JSON` prompt. Save the two values in your password manager for the smoke test. Render generates the JWT signing secret and a separate metrics token.
3. Review the resource list and apply the Blueprint. Wait for the API, worker and Beat deployments to succeed. Copy the API's HTTPS URL from its service page.

```sh
python3 - <<'PY'
import json
import secrets

print(json.dumps({
    "intake": {
        "secret": secrets.token_hex(32),
        "tenant": "demo",
        "scopes": ["runs:read", "runs:write", "documents:read", "documents:write", "evals:read"],
    },
    "reviewer": {
        "secret": secrets.token_hex(32),
        "tenant": "demo",
        "scopes": ["runs:read", "approvals:write"],
    },
}))
PY
```

Each application service executes `alembic upgrade head` as its pre-deploy command. A PostgreSQL session advisory lock serializes the app and LangGraph schema setup across the three services. The database uses a direct connection for that lock. The document migration enables the supported `vector` extension. The API binds to Render's `PORT` and reports readiness on `/healthz`. [Pre-deploy commands](https://render.com/docs/deploys#pre-deploy-command), [PostgreSQL extensions](https://render.com/docs/postgresql-extensions), [Web service port binding](https://render.com/docs/web-services#port-binding).

## Smoke test

Set the API URL to the value shown in your Dashboard.

```sh
API_URL='https://your-service.onrender.com'
curl -fsS "$API_URL/healthz"
```

1. Open `/docs` at that URL and check that `GET /healthz` returns `{"status":"ok"}`.
2. Import the [Postman collection](postman_collection.json). Set its `base_url`, `client_id` to `intake` and `client_secret` to the corresponding generated secret. Get a token, submit a run and read it until it reaches `waiting_approval`.
3. Set `client_id` to `reviewer` with its own secret and get a reviewer token. Approve the same run, then poll it to `succeeded`.
4. Switch back to the intake client and replay the submission with the same idempotency key and body. Verify the response has the original run ID. Keep any demo credentials in a private Postman environment.

## Operate and extend

Automatic deploys on commits are disabled in the Blueprint. Use the same reviewed Git commit for all three application services. Keep schema migrations compatible with the preceding release so in-flight work continues during rolling deploys. Keep one Beat instance and scale the execution worker independently when needed.

Before upgrading a database from revision `0003_retry_gate` or earlier, pause intake and drain in-flight runs on the previous version. Revision `0004_verification_and_tracing` adds evidence reviews, and an existing approval checkpoint may have no stored review. For a pending proposal that needs the new evidence checks, reject it on the previous version before resubmitting on the upgraded version with a fresh idempotency key.

The API publishes `/metrics` for an authenticated scraper. Configure your scraper to send `Authorization: Bearer <VERSTAK_METRICS_TOKEN>` over HTTPS. The [Compose quickstart](../README.md) runs the Prometheus and Grafana dashboards locally.

The [n8n workflow exports](../n8n_workflows/README.md) can target the deployed API. Use the Render HTTPS URL in the OAuth2 token configuration and in the HTTP Request nodes. Each external integration uses its own credentials.

For an optional hosted MCP endpoint, create a separate Docker web service from the same repo in the same region. Set `VERSTAK_API_URL` to the deployed API's HTTPS base URL and `VERSTAK_MCP_ALLOWED_HOSTS` to the MCP service's public hostname and hostname with `:443`, separated by a comma. Use this Docker Command and the default TCP health check.

```sh
/bin/sh -ec 'exec verstak-mcp --transport streamable-http --host 0.0.0.0 --port "${PORT:-10000}"'
```

Connect a Streamable HTTP client to `/mcp` on the MCP service with a bearer token issued by the Verstak API.
