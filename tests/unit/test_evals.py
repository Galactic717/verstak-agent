import asyncio
import json

import httpx
import pytest

from verstak.contracts import EvalReport as EvalSummary
from verstak.llm import Router
from verstak.llm.evals import GoldenCase, compare_reports, evaluate, load_cases


def test_fixture_scores_all_thirty_golden_cases() -> None:
    cases = load_cases()
    report = asyncio.run(evaluate(Router(), cases))

    assert report.cases == len({case.id for case in cases}) == 30
    assert report.schema_validity == 1
    assert report.tool_f1 >= 0.9
    assert report.grounded_accuracy >= 0.9
    assert report.cost_per_1000_usd == 0
    assert report.p95_latency_ms >= 0


def test_comparison_keeps_unknown_cost_distinct_from_zero() -> None:
    report = EvalSummary(
        provider="fixture",
        cases=30,
        tool_f1=0.9333,
        schema_validity=1,
        grounded_accuracy=0.9667,
        p95_latency_ms=0.0541,
        cost_per_1000_usd=0,
    )
    remote = EvalSummary.model_validate(
        {**report.model_dump(), "provider": "cloud", "cost_per_1000_usd": None}
    )

    table = compare_reports([report, remote])

    assert "| fixture | 30 | 0.9333 | 100.00% | 96.67% | 0.0541 | $0.000000 |" in table
    assert "| cloud | 30 | 0.9333 | 100.00% | 96.67% | 0.0541 | Unreported |" in table


def test_comparison_requires_same_case_count() -> None:
    report = EvalSummary(
        provider="fixture",
        cases=30,
        tool_f1=1,
        schema_validity=1,
        grounded_accuracy=1,
        p95_latency_ms=1,
    )
    sample = EvalSummary.model_validate({**report.model_dump(), "provider": "sample", "cases": 5})

    with pytest.raises(ValueError, match="same case count"):
        compare_reports([report, sample])


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
