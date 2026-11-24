import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

RunStatus = Literal["queued", "running", "waiting_approval", "succeeded", "rejected", "failed"]
Budget = Annotated[Decimal, Field(ge=0, le=100, decimal_places=6, allow_inf_nan=False)]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(strip_whitespace=True, max_length=12_000)] = ""
    document_id: UUID | None = None
    max_steps: int = Field(default=3, ge=1, le=3)
    max_budget_usd: Budget = Decimal("0.05")

    @model_validator(mode="after")
    def check_input(self) -> Self:
        if not self.text and self.document_id is None:
            raise ValueError("provide text or document_id")
        return self

    @property
    def budget_micros(self) -> int:
        return int(self.max_budget_usd * 1_000_000)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"max_budget_usd"})
        payload["budget_micros"] = self.budget_micros
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool = Field(strict=True)


class RunReceipt(BaseModel):
    id: UUID


GroundingFinding = Literal["reference_not_in_source", "payment_not_in_source"]


class GroundingReview(BaseModel):
    accepted: bool
    findings: list[GroundingFinding]


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workflow: Literal["triage"] = "triage"
    status: RunStatus
    triage: dict[str, object] | None
    verification: GroundingReview | None = None
    approved: bool | None
    approved_by: str | None
    attempts: int
    error: str | None
    cost_usd: Decimal
    created_at: datetime
    updated_at: datetime


class EvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=100)
    cases: int = Field(ge=1, le=10_000)
    tool_f1: float = Field(ge=0, le=1)
    schema_validity: float = Field(ge=0, le=1)
    grounded_accuracy: float = Field(ge=0, le=1)
    p95_latency_ms: float = Field(ge=0, allow_inf_nan=False)
    cost_per_1000_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
