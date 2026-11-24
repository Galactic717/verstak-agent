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

Record an aggregate report in the local Verstak API using a bearer token with permission to write evals.

```bash
curl --fail-with-body http://localhost:8000/evals \
  -H "Authorization: Bearer $VERSTAK_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @eval-report.json
```

## Reproduce the CPU comparison

```bash
uv sync --locked
VERSTAK_LLM_PROVIDER=fixture VERSTAK_LLM_MODEL='' \
  uv run --frozen verstak-eval --cases evals/goldens.json --output eval-fixture.json

(
  set -e
  qwen_container="verstak-eval-qwen3-$$"
  docker run --detach --rm --name "$qwen_container" --cpus 4 --memory 6g \
    --publish 127.0.0.1::11434 --env OLLAMA_NUM_PARALLEL=1 \
    --env OLLAMA_MAX_LOADED_MODELS=1 --env OLLAMA_CONTEXT_LENGTH=4096 \
    --volume verstak-eval-local-models:/root/.ollama \
    ollama/ollama@sha256:1cabd20e1762a7a8444bf4a7110b346d923cd77ab6c97c984434140638c913ec
  trap 'docker stop "$qwen_container" >/dev/null' EXIT
  ollama_address="$(docker port "$qwen_container" 11434/tcp)"
  curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-max-time 30 \
    "http://$ollama_address/api/tags" >/dev/null
  docker exec "$qwen_container" ollama pull qwen3:4b-q4_K_M
  docker exec "$qwen_container" ollama pull qwen3:1.7b-q4_K_M
  docker exec "$qwen_container" ollama list

  run_qwen() {
    local model="$1" report="$2"
    curl --fail --silent --show-error "http://$ollama_address/api/chat" \
      -H 'Content-Type: application/json' \
      --data "$(printf '{"model":"%s","messages":[{"role":"user","content":"Reply Ready"}],"think":false,"stream":false,"options":{"num_predict":16}}' "$model")" \
      >/dev/null
    docker exec "$qwen_container" ollama ps
    VERSTAK_LLM_PROVIDER=ollama VERSTAK_LLM_URL="http://$ollama_address/api/chat" \
      VERSTAK_LLM_MODEL="$model" VERSTAK_LLM_TIMEOUT_SECONDS=60 \
      VERSTAK_LLM_MAX_OUTPUT_TOKENS=512 VERSTAK_LLM_API_KEY='' \
      VERSTAK_LLM_INPUT_USD_PER_MILLION=0 VERSTAK_LLM_OUTPUT_USD_PER_MILLION=0 \
      uv run --frozen verstak-eval --cases evals/goldens.json --output "$report"
  }

  run_qwen qwen3:4b-q4_K_M eval-qwen-4b.json
  run_qwen qwen3:1.7b-q4_K_M eval-qwen-1.7b.json
)

uv run --frozen verstak-eval --compare eval-fixture.json eval-qwen-1.7b.json eval-qwen-4b.json
```

The Ollama adapter applies the triage schema, sets `think: false`, and uses temperature 0 and seed 42. Both Qwen models use stock chat weights with Q4_K_M quantization. The comparison checks case counts. Reuse the same cases file for each run. [Ollama chat fields](https://docs.ollama.com/api/chat) · [Qwen3 tags](https://ollama.com/library/qwen3/tags)

## Measured local results

The 24 November 2026 runs used an AMD Ryzen 7 7800X3D with Ollama 0.40.18 in Docker Desktop. Both model runs used a four-CPU limit, 6 GiB of memory, a 4,096-token context, one request at a time, and a warm-up call before timing. The rules fixture ran in host Python. All three used the same 30-case corpus.

| Provider | Tool-call F1 | Schema validity | Grounded field accuracy | p95 latency |
| --- | ---: | ---: | ---: | ---: |
| [Rules fixture](results/fixture.json) | 0.9333 | 100% | 96.67% | 0.0518 ms |
| [Qwen3-1.7B Q4_K_M](results/qwen3-1.7b-cpu.json) | 0.7586 | 93.33% | 46.67% | 8.60 s |
| [Qwen3-4B Q4_K_M](results/qwen3-4b-cpu.json) | 0.9153 | 96.67% | 96.67% | 14.23 s |

On this corpus, Qwen3-4B extracted more fields correctly. Qwen3-1.7B had lower p95 latency. Both models ran without API fees. Each Qwen JSON report preserves `null` for the estimate per 1,000 calls because schema failures left some token usage unknown.

The corpus SHA-256 was `e0c2bef9ff8b2720ca2776640598d58c140515d025aabadc015282f4ac8fe803`. Ollama reported model IDs `8f68893c685c` for 1.7B and `2bfd38a7daaf` for 4B.
