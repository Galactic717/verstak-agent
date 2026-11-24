# n8n workflows

Start the demo stack from the repository root. Open n8n at `http://localhost:5678` and create the local owner. For each workflow, choose **Import from File** and select its JSON export. The same files can be imported together with the CLI.

```bash
docker compose exec n8n n8n import:workflow --separate --input=/opt/verstak/workflows
```

## Credentials

The HTTP Request nodes use credentials you add in the n8n editor. Bind them to the named nodes after import. The credentials below match the local `.env.example`. Use independently generated credentials if you share the instance.

| Credential | Configuration | Used by |
| --- | --- | --- |
| `Verstak API` | **OAuth2 API**, Client Credentials, access token URL `http://api:8000/oauth/token`, authentication **Body**, client `n8n`, demo secret `n8n-demo-secret` | Create triage, read status, parse workbook, read eval summary |
| `Verstak reviewer API` | Same OAuth2 settings, client `reviewer`, demo secret `reviewer-demo-secret` | Persist reviewer decision |
| `Verstak intake webhook` | **Header Auth**, name `X-Verstak-Webhook-Key`, value of your choice | Triage and workbook webhook nodes |
| `Verstak approval webhook` | **Header Auth**, name `X-Verstak-Approval-Key`, separate value available only to reviewers | Approval webhook node |
| `Verstak Slack bot` | **Slack API** bot token with `chat:write`. Invite the bot to the chosen channel and select the channel in both Slack nodes | Review alerts and daily digest |
| `Verstak Google Sheets` | **Google Sheets OAuth2 API** with write access to the destination spreadsheet | Upsert sheet rows |

Publish the workflows once credentials and destinations are bound. The examples use the production path `/webhook/`. When listening for a one-off test from the editor, use the generated `/webhook-test/` URL.

## 01 · Triage and approval

Send a stable `Idempotency-Key` for each business event. The webhook returns the API status and body. The workflow polls for review for up to 60 seconds and posts the run ID to Slack. Read the proposal with an authorized `GET /runs/{id}` request, then send the Boolean approval decision through the separate reviewer webhook.

```bash
curl -fsS http://localhost:5678/webhook/verstak/triage \
  -H "X-Verstak-Webhook-Key: $INTAKE_KEY" \
  -H 'Idempotency-Key: invoice-inv-042' \
  -H 'Content-Type: application/json' \
  -d '{"text":"Please record invoice INV-042 for USD 125.50."}'

curl -fsS "http://localhost:5678/webhook/verstak/approvals/$RUN_ID" \
  -H "X-Verstak-Approval-Key: $APPROVAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"approved":true}'
```

Slack receives a run ID. A reviewer submits the decision separately through the authenticated webhook. Each approval is attributed to the configured reviewer API client. The equivalent Telegram integration can replace the alert node with **Telegram → Send Message**, keeping the decision endpoint and access controls.

## 02 · Workbook to Google Sheets

Set the spreadsheet ID in **Upsert sheet rows**. Create the `Verstak Imports` tab with `source_key`, `document_id`, `sheet`, `row`, `row_json` headers in that order. The API validates and parses the workbook. The workflow writes raw values and matches on the event key, sheet and row, so a sequential replay of the same event updates the same rows. Serialize events with the same key when ingesting concurrently. Use a fresh key for each revised workbook.

```bash
curl -fsS http://localhost:5678/webhook/verstak/workbook \
  -H "X-Verstak-Webhook-Key: $INTAKE_KEY" \
  -H 'Idempotency-Key: prices-2026-11' \
  -F 'file=@prices.xlsx'
```

## 03 · Daily evaluation and cost digest

The workflow reads the tenant's run count, status distribution, recorded model cost, p95 run time and latest published evaluation from `GET /evals`. It posts the digest at 09:00 in the workflow timezone. Set the timezone in Workflow Settings before publishing.
