# Triage evaluations

`goldens.json` contains 30 synthetic requests across invoice, support, and general intake. The cases include missing payment fields, unrelated retrieved material, English and Ukrainian requests, and requests whose intent is implicit.

```bash
uv run verstak-eval --output eval-report.json --details eval-cases.json
```

The command prints an aggregate report and can write the same payload to a file. The optional details file contains per-case classifications, elapsed time, cost where known, and error codes. Each case measures a single classification call using the worker's provider adapter and output schema.

| Metric | Definition |
| --- | --- |
| `tool_f1` | Micro-F1 on the proposed tool. Missing proposals count as false negatives. Incorrect proposals count as false negatives and false positives. |
| `schema_validity` | Fraction of cases producing a valid `Triage` object. |
| `grounded_accuracy` | Fraction of cases where the extracted reference, amount in cents, and currency all match the golden values, including expected omissions. |
| `p95_latency_ms` | Nearest-rank 95th percentile for individual classification calls, including failures. |
| `cost_per_1000_usd` | Estimated token cost using configured prices, scaled to 1,000 calls. The value is `null` if any call has unknown cost. |

Publish the aggregate report to a running API using a bearer token with permission to write evals.

```bash
curl --fail-with-body http://localhost:8000/evals \
  -H "Authorization: Bearer $VERSTAK_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @eval-report.json
```

The default provider is `fixture`. To compare with a model hosted on vLLM or Ollama, select `openai-compatible` and supply the full chat completions endpoint and the served model name. Run the same golden dataset once per provider. The dataset contains synthetic text, and a run against a remote endpoint submits that text to the configured provider. [vLLM structured outputs](https://docs.vllm.ai/en/latest/features/structured_outputs/) · [Ollama compatibility](https://docs.ollama.com/api/openai-compatibility)

```bash
VERSTAK_LLM_PROVIDER=openai-compatible \
VERSTAK_LLM_URL=http://localhost:8001/v1/chat/completions \
VERSTAK_LLM_MODEL=verifine-slm \
uv run verstak-eval --output eval-verifine.json
```
