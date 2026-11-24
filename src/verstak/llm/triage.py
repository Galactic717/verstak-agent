import re
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Category = Literal["invoice", "support", "other"]
Tool = Literal["record_invoice", "route_support", "record_request"]
TOOL_BY_CATEGORY: dict[Category, Tool] = {
    "invoice": "record_invoice",
    "support": "route_support",
    "other": "record_request",
}


class Triage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    category: Category
    tool: Tool
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    reference: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
        | None
    ) = None
    amount_cents: Annotated[int, Field(ge=0, le=10**14)] | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.tool != TOOL_BY_CATEGORY[self.category]:
            raise ValueError("Tool must match the category")
        if (self.amount_cents is None) != (self.currency is None):
            raise ValueError("Amount and currency must be provided together")
        if self.amount_cents is not None and self.category != "invoice":
            raise ValueError("Payment fields apply only to invoice triage")
        return self


_SUPPORT = re.compile(
    r"\b(?:support|broken|error|cannot|can't|failed|refund|dispute|outage|login|password|"
    r"won't|not working|charged twice|duplicate charge)\b|помилк|не (?:працює|можу)|повернення",
    re.IGNORECASE,
)
_INVOICE = re.compile(r"\b(?:invoice|inv-[a-z0-9]+|рахунок|інвойс)\b", re.IGNORECASE)
_REFERENCE = re.compile(r"\b(?:INV|TKT|REQ)-[a-z0-9]+(?:-[a-z0-9]+)*\b", re.IGNORECASE)
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
_CURRENCY = r"USD|EUR|UAH|GBP"
_PAYMENT = re.compile(
    rf"\b(?P<leading_currency>{_CURRENCY})\s+(?P<leading_amount>{_NUMBER})(?!\w|[.,]\d)"
    rf"|(?<![\w.,-])(?P<trailing_amount>{_NUMBER})\s+(?P<trailing_currency>{_CURRENCY})\b",
    re.IGNORECASE,
)


def fixture_triage(text: str) -> Triage:
    """Classify with local rules and extract fields from the submitted text."""
    category: Category = "other"
    if _SUPPORT.search(text):
        category = "support"
    elif _INVOICE.search(text):
        category = "invoice"

    reference = _REFERENCE.search(text)
    amount_cents = None
    currency = None
    payment = _PAYMENT.search(text) if category == "invoice" else None
    if payment:
        amount = payment["leading_amount"] or payment["trailing_amount"]
        if len(amount) <= 20:
            cents = int(Decimal(amount.replace(",", "")) * 100)
            if cents <= 10**14:
                amount_cents = cents
                currency = (payment["leading_currency"] or payment["trailing_currency"]).upper()

    return Triage(
        category=category,
        tool=TOOL_BY_CATEGORY[category],
        summary=" ".join(text.split())[:240] or "Business request",
        reference=reference[0] if reference and len(reference[0]) <= 100 else None,
        amount_cents=amount_cents,
        currency=currency,
    )
