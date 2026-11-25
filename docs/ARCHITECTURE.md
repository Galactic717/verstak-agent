# Architecture

verstak-agent turns a tenant's inbound request into a typed proposal, a source-grounded evidence review, and a human-approved database action. The API and MCP server are stateless. A Redis message makes a run eligible for execution, while PostgreSQL holds the authoritative state for dispatch, verification, approval, checkpoints, and effects.

## Data flow

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI
    participant PG as PostgreSQL
    participant Worker as Celery
    participant Graph as LangGraph
    Client->>API: Submit with Idempotency-Key
    API->>PG: Insert run and due timestamp
    API-->>Client: 202 + run ID
    Worker->>PG: Lease due run and publish to Redis
    Worker->>PG: Acquire run advisory lock
    Worker->>Graph: Classify with tenant document context
    Graph->>PG: Reserve cost, persist result, save checkpoint
    Graph->>PG: Persist source evidence review
    Note over Graph: Unsupported fields trigger a bounded correction
    Graph-->>Worker: Interrupt for review
    Worker->>PG: Mark waiting_approval
    Client->>API: Approve or reject
    API->>PG: Persist decision and make run due
    Worker->>Graph: Resume same checkpoint thread
    Graph->>PG: Save terminal checkpoint
    Worker->>PG: Commit terminal run status and case action if approved
```

One transaction inserts the submission and dispatch timestamp. Conflicting submissions with the same tenant and idempotency key return HTTP 409 when the normalized request fingerprints differ. Redis carries run IDs. If a broker delivery disappears or the dispatcher loses its connection after committing a lease, a later tick picks up the due row.

The worker holds a PostgreSQL advisory lock for each run using a dedicated connection. LangGraph saves state with synchronous checkpoint durability and uses the run ID as its thread ID. Each typed proposal commits with its cost settlement. A restart after settlement can reuse the proposal even when the run has no call slots remaining. A worker may still lose a provider response before settlement, in which case the reservation stays charged against the run's allowance. [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

The independent evidence agent checks references as complete tokens and looks for the proposed amount and currency together in the submitted text or attached document. The graph stores both the review and the accepted proposal before the approval interrupt. Unsupported values produce field-specific retry feedback within the same model-call and budget limits. The approver evaluates the proposal's category, summary, and business context. Retrieved knowledge provides classification context while the submitted source supplies evidence for action-driving fields.

Model proposals pick one of three fixed case types. An approval transaction locks the run, checks the authenticated tenant and approval scope, records the deciding identity, and queues the resume. An identical repeat returns the saved decision. After the terminal graph checkpoint, the worker commits the case and the successful run status in one PostgreSQL transaction. The case has a unique run ID. If a previous worker already saved a case in a separate transaction, the next delivery uses it to recover the terminal run status.

## Persistence

```mermaid
erDiagram
    DOCUMENTS ||--o{ DOCUMENT_CHUNKS : contains
    DOCUMENTS o|..o{ WORKFLOW_RUNS : referenced_by
    WORKFLOW_RUNS ||--o{ MODEL_ATTEMPTS : reserves
    WORKFLOW_RUNS ||--o| CASE_ACTIONS : produces
    WORKFLOW_RUNS ||--o| DEAD_LETTERS : records
    WORKFLOW_RUNS ||..o{ CHECKPOINTS : thread_id
    WORKFLOW_RUNS {
        uuid id PK
        string tenant
        string idempotency_key
        string status
        datetime dispatch_after
        datetime retry_at
        boolean approved
    }
    MODEL_ATTEMPTS {
        uuid run_id PK, FK
        int sequence PK
        bigint reserved_micros
        bigint charged_micros
        json proposal
        json verification
    }
    CASE_ACTIONS {
        uuid run_id PK, FK
        string tenant
        string tool
        json payload
    }
    EVALUATIONS {
        uuid id PK
        string tenant
        json report
    }
```

Alembic owns the application schema and initializes LangGraph's separately versioned checkpoint tables after migration. Run the migration service once per release with the same database URL as the API and workers.

Documents are parsed within file, page, row, and extracted-text limits. PDF extraction requires a text layer. Sheet formulas are not evaluated. The search query obtains tenant-local BM25 statistics from PostgreSQL lexemes and combines BM25 and pgvector HNSW ranks with reciprocal rank fusion. Corpus statistics are computed at query time, which suits the demo's small libraries. The included 256-dimensional feature hashing is lexical, with the potential for collisions and limited synonym recall. [PostgreSQL text-search operators](https://www.postgresql.org/docs/16/functions-textsearch.html) · [pgvector hybrid search](https://github.com/pgvector/pgvector#hybrid-search)

## Failure behavior

| Failure | Server behavior |
| --- | --- |
| Worker dies or broker loses a delivery | The run remains due after the lease expires. A worker reloads the checkpoint and continues while holding the advisory lock. |
| Worker fails around the case commit | Before commit, the worker reuses the terminal checkpoint to retry finalization. After commit, the case and successful run status are durable together. |
| PostgreSQL drops a connection | A failed transaction rolls back. After PostgreSQL returns, the run is eligible through its lease or retry deadline. |
| Provider times out or returns a retryable HTTP response | The worker records a retry deadline and backs off exponentially. Stale deliveries honor the deadline. Existing reservations count toward the budget. |
| Model produces invalid structured output | Up to two corrections fit within a three-call run limit. Exhaustion records a failed run and a dead-letter row. |
| A structurally valid proposal contains an unsupported reference, amount, or currency | The evidence agent records the finding and requests a correction. Only an accepted proposal is exposed for approval. |
| The next reservation exceeds the run budget | The worker stops before sending the model request. Provider pricing and billed-token behavior affect estimates. |
| Reviewer denies the proposal | The run becomes `rejected` and no case action is inserted. |

The local case write uses a PostgreSQL primary key as its idempotency boundary. For future external side effects, the downstream service must honor its own idempotency key or provide reconciliation.

The optional OpenTelemetry exporter links HTTP submission to dispatch and worker spans through the durable outbox. After human review, a span link connects the new approval/resume trace to the submission trace. [Trace setup](TRACING.md)

## Threat model

The protected assets are tenant requests and documents, model credentials, reviewer authority, evaluation provenance, and the integrity of the case action. The relevant external actors are unauthenticated clients, authenticated clients from another tenant or with narrower scopes, document submitters, and any service that could intercept deployment traffic. PostgreSQL, Redis, workers, and their operator are inside the trusted application boundary.

| Boundary or scenario | Enforcement and operational assumptions |
| --- | --- |
| Forged identity or cross-tenant read | Client-credentials tokens bind signed tenant and scope claims to the current client registration. Run, document, search, approval, and eval queries filter by that tenant. A submission-only token receives a receipt. The app database role is trusted across tenants. [Auth](../src/verstak/auth.py) · [API](../src/verstak/api.py) · [Document repository](../src/verstak/documents/repository.py) |
| Embedded instructions in user text or document | The router passes the material as data and validates a bounded typed proposal. Approval and the persisted decision are checked before the fixed SQL write. External model endpoints receive bounded excerpts, so the deployed endpoint must be approved for that data. [Model router](../src/verstak/llm/router.py) · [Graph](../src/verstak/agents.py) · [Action store](../src/verstak/store.py) |
| MCP overreach | The tools call fixed REST routes using the request's bearer token. Host and origin checks protect the Streamable HTTP surface. Search uses bound SQL parameters. [MCP server](../src/verstak/mcp_server/__init__.py) |
| Upload exhaustion or spreadsheet formulas | Bounded parsing limits file and archive expansion, extracted text, and tabular shape. The optional n8n Sheets import uses raw values. [Ingestion](../src/verstak/documents/ingest.py) · [Sheets workflow](../n8n_workflows/02_excel_openpyxl_to_sheets_pipeline.json) |
| Replay, forged approval, or runaway spend | Tenant-scoped row locks and a distinct approval scope protect decisions. Case keys, Redis quotas, and persisted model reservations bound repeated effects and admission. [API](../src/verstak/api.py) · [Quota](../src/verstak/limits.py) · [Store](../src/verstak/store.py) |
| Public access to development interfaces | Compose binds published ports to loopback and uses demo credentials. A hosted installation needs TLS ingress, per-client secrets, operator-only metrics and dashboards, network isolation for Redis and PostgreSQL, and a retention policy for stored prompts and documents. [Compose](../docker-compose.yml) |

## Run the integration checks

Use disposable demo data. The tests create uniquely scoped tenants in the configured instance.

```bash
cp .env.example .env
docker compose up -d --wait postgres redis
export VERSTAK_DATABASE_URL='postgresql://verstak:verstak-demo-db-password@127.0.0.1:55432/verstak'
export VERSTAK_TEST_DATABASE_URL="$VERSTAK_DATABASE_URL"
export VERSTAK_TEST_REDIS_URL='redis://127.0.0.1:56379/0'
uv run --locked alembic upgrade head
uv run --locked pytest tests/unit tests/integration tests/bdd --cov=verstak

docker compose up --build -d --wait
uv run --locked playwright install chromium
VERSTAK_E2E_BASE_URL=http://127.0.0.1:8000 uv run --locked pytest tests/e2e
```

The crash-injection cases interrupt the worker inside the test process against real PostgreSQL and Redis. They cover failures before and after case finalization, a lost checkpoint after model settlement, and provider timeout, malformed output, and budget limits.
