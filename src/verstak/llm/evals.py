import argparse
import asyncio
import math
from importlib.resources import files
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .router import InvalidModelOutput, PermanentModelError, Router, TransientModelError
from .triage import TOOL_BY_CATEGORY, Category, Tool


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    context: str = ""
    category: Category
    reference: str | None = None
    amount_cents: int | None = None
    currency: str | None = None


class CaseResult(BaseModel):
    id: str
    expected_tool: Tool
    proposed_tool: Tool | None = None
    grounded: bool = False
    latency_ms: float
    cost_micros: int | None = None
    error: Literal["invalid_model_output", "transient_provider_error", "provider_error"] | None = (
        None
    )


class EvalReport(BaseModel):
    provider: str
    cases: int
    tool_f1: float
    schema_validity: float
    grounded_accuracy: float
    p95_latency_ms: float
    cost_per_1000_usd: float | None
    results: list[CaseResult] = Field(exclude=True)


def load_cases(path: Path | None = None) -> list[GoldenCase]:
    if path is not None:
        case_bytes = path.read_bytes()
    else:
        packaged = files("verstak.llm").joinpath("goldens.json")
        if packaged.is_file():
            case_bytes = packaged.read_bytes()
        else:
            case_bytes = (
                Path(__file__).resolve().parents[3] / "evals" / "goldens.json"
            ).read_bytes()
    try:
        cases = TypeAdapter(list[GoldenCase]).validate_json(case_bytes)
    except ValidationError:
        raise ValueError("Golden case file does not match the case schema") from None
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Golden cases must contain at least one case and unique IDs")
    return cases


async def _evaluate_case(router: Router, case: GoldenCase) -> CaseResult:
    started = perf_counter()
    expected_tool = TOOL_BY_CATEGORY[case.category]
    try:
        result = await router.classify(case.text, case.context, "")
    except InvalidModelOutput:
        error: Literal["invalid_model_output", "transient_provider_error", "provider_error"] = (
            "invalid_model_output"
        )
    except TransientModelError:
        error = "transient_provider_error"
    except PermanentModelError:
        error = "provider_error"
    else:
        return CaseResult(
            id=case.id,
            expected_tool=expected_tool,
            proposed_tool=result.triage.tool,
            grounded=(result.triage.reference, result.triage.amount_cents, result.triage.currency)
            == (case.reference, case.amount_cents, case.currency),
            latency_ms=(perf_counter() - started) * 1000,
            cost_micros=result.cost_micros,
        )
    return CaseResult(
        id=case.id,
        expected_tool=expected_tool,
        latency_ms=(perf_counter() - started) * 1000,
        error=error,
    )


async def evaluate(router: Router, cases: list[GoldenCase]) -> EvalReport:
    if not cases:
        raise ValueError("At least one eval case is required")
    results = [await _evaluate_case(router, case) for case in cases]
    count = len(results)
    valid = sum(result.proposed_tool is not None for result in results)
    matches = sum(result.proposed_tool == result.expected_tool for result in results)
    latency_ms = sorted(result.latency_ms for result in results)[math.ceil(count * 0.95) - 1]
    priced = [result.cost_micros for result in results if result.cost_micros is not None]
    return EvalReport(
        provider=f"{router.provider}:{router.model}" if router.model else router.provider,
        cases=count,
        tool_f1=round(2 * matches / (count + valid), 4),
        schema_validity=round(valid / count, 4),
        grounded_accuracy=round(sum(result.grounded for result in results) / count, 4),
        p95_latency_ms=round(latency_ms, 4),
        cost_per_1000_usd=round(sum(priced) / count / 1000, 6) if len(priced) == count else None,
        results=results,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate triage against golden requests.")
    parser.add_argument("--cases", type=Path, help="Use a custom JSON case file.")
    parser.add_argument("--output", type=Path, help="Also save the JSON report to a file.")
    parser.add_argument("--details", type=Path, help="Save a separate JSON array of case results.")
    options = parser.parse_args()
    try:
        router = Router.from_env()
        cases = load_cases(options.cases)
        scored = asyncio.run(evaluate(router, cases))
        report = scored.model_dump_json(indent=2) + "\n"
        if options.output:
            options.output.write_text(report, encoding="utf-8")
        if options.details:
            details = TypeAdapter(list[CaseResult]).dump_json(scored.results, indent=2)
            options.details.write_bytes(details + b"\n")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(report, end="")


if __name__ == "__main__":
    main()
