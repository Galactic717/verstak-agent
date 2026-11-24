import asyncio
import json

import httpx
import pytest

from verstak.llm import Router
from verstak.llm.evals import GoldenCase, evaluate, load_cases


def test_fixture_scores_all_thirty_golden_cases() -> None:
    cases = load_cases()
    report = asyncio.run(evaluate(Router(), cases))

    assert report.cases == len({case.id for case in cases}) == 30
    assert report.schema_validity == 1
    assert report.tool_f1 >= 0.9
    assert report.grounded_accuracy >= 0.9
    assert report.cost_per_1000_usd == 0
    assert report.p95_latency_ms >= 0


def test_invalid_output_counts_as_missed_tool_and_unknown_cost() -> None:
    def send(request: httpx.Request) -> httpx.Response:
        content = "invalid JSON"
        if "Contact details" in request.content.decode():
            content = json.dumps(
                {"category": "other", "tool": "record_request", "summary": "Contact details"}
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    router = Router(
        provider="openai-compatible",
        model="test",
        url="https://provider.invalid",
        transport=httpx.MockTransport(send),
    )
    report = asyncio.run(
        evaluate(
            router,
            [
                GoldenCase(id="valid", text="Contact details", category="other"),
                GoldenCase(id="invalid", text="Broken request", category="support"),
            ],
        )
    )

    assert report.tool_f1 == pytest.approx(0.6667)
    assert report.schema_validity == 0.5
    assert report.grounded_accuracy == 0.5
    assert report.cost_per_1000_usd is None
    assert report.results[1].error == "invalid_model_output"
    assert "results" not in report.model_dump()
