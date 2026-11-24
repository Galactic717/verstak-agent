import asyncio
import json
from decimal import Decimal
from typing import Literal

import httpx
import pytest

from verstak.llm import InvalidModelOutput, PermanentModelError, Router, TransientModelError


def test_ollama_uses_native_chat_with_bounded_structured_output() -> None:
    def send(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert payload["stream"] is False
        assert payload["think"] is False
        assert payload["options"]["num_predict"] == 128
        assert payload["options"]["temperature"] == 0
        assert payload["format"]["properties"]["category"]["type"] == "string"
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": '{"category":"other","tool":"record_request","summary":"Question"}',
                },
                "done": True,
                "prompt_eval_count": 21,
                "eval_count": 6,
            },
        )

    router = Router(
        provider="ollama",
        url="http://127.0.0.1:11434/api/chat",
        model="qwen3:4b-q4_K_M",
        max_output_tokens=128,
        transport=httpx.MockTransport(send),
    )
    result = asyncio.run(router.classify("Contact details", "", ""))

    assert result.triage.tool == "record_request"
    assert result.input_tokens == 21
    assert result.output_tokens == 6
    assert result.cost_micros == 0


def test_default_fixture_classifies_invoice_without_using_context_as_source() -> None:
    router = Router()
    result = asyncio.run(
        router.classify(
            "Invoice INV-718 totals USD 52.30.",
            "Agency invoice INV-446 totals USD 921.00.",
            "",
        )
    )

    assert result.triage.category == "invoice"
    assert result.triage.tool == "record_invoice"
    assert result.triage.reference == "INV-718"
    assert result.triage.amount_cents == 5230
    assert result.triage.currency == "USD"
    assert result.cost_micros == router.max_cost_micros("", "", "") == 0


@pytest.mark.parametrize(
    ("provider", "response", "credential_header"),
    [
        (
            "openai-compatible",
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"category":"other","tool":"record_request","summary":"Question"}'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            },
            "authorization",
        ),
        (
            "anthropic",
            {
                "content": [
                    {
                        "type": "text",
                        "text": '{"category":"other","tool":"record_request","summary":"Question"}',
                    }
                ],
                "usage": {"input_tokens": 11, "output_tokens": 5},
            },
            "x-api-key",
        ),
        (
            "gemini",
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"category":"other","tool":"record_request","summary":"Question"}'
                                }
                            ]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 11,
                    "candidatesTokenCount": 4,
                    "thoughtsTokenCount": 1,
                },
            },
            "x-goog-api-key",
        ),
    ],
)
def test_provider_normalizes_response_and_usage(
    provider: Literal["openai-compatible", "anthropic", "gemini"],
    response: dict[str, object],
    credential_header: str,
) -> None:
    def send(request: httpx.Request) -> httpx.Response:
        assert request.headers[credential_header].endswith("fake-key")
        assert "Ask a question" in request.content.decode()
        return httpx.Response(200, json=response)

    router = Router(
        provider=provider,
        url="https://provider.invalid/classify",
        model="test",
        api_key="fake-key",
        input_usd_per_million=Decimal("2.50"),
        output_usd_per_million=Decimal("3"),
        transport=httpx.MockTransport(send),
    )
    result = asyncio.run(router.classify("Ask a question", "", ""))

    assert result.triage.tool == "record_request"
    assert result.input_tokens == 11
    assert result.output_tokens == 5
    assert result.cost_micros == 43
    assert router.max_cost_micros("Ask a question", "", "") > result.cost_micros


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, TransientModelError), (502, TransientModelError), (401, PermanentModelError)],
)
def test_provider_errors_do_not_include_response_body(status: int, error: type[Exception]) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, text="Private provider message")
    )
    router = Router(
        provider="openai-compatible",
        url="https://provider.invalid",
        model="test",
        transport=transport,
    )

    with pytest.raises(error) as raised:
        asyncio.run(router.classify("Private user message", "", ""))

    assert "Private" not in str(raised.value)


@pytest.mark.parametrize(
    "content",
    [
        "Not JSON",
        json.dumps({"category": "invoice", "tool": "record_request", "summary": "Mismatch"}),
        json.dumps(
            {"category": "invoice", "tool": "record_invoice", "summary": "x", "amount_cents": "20"}
        ),
    ],
)
def test_model_output_requires_valid_schema(content: str) -> None:
    router = Router(
        provider="openai-compatible",
        url="https://provider.invalid",
        model="test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": content}}]}
            )
        ),
    )

    with pytest.raises(InvalidModelOutput):
        asyncio.run(router.classify("Invoice INV-002", "", ""))


def test_unknown_usage_keeps_reservation() -> None:
    content = {"category": "support", "tool": "route_support", "summary": "Broken export"}
    router = Router(
        provider="openai-compatible",
        url="https://provider.invalid",
        model="test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
            )
        ),
    )
    result = asyncio.run(router.classify("Please fix my export", "", ""))

    assert result.triage.tool == "route_support"
    assert result.cost_micros is None
