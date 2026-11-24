import pytest

from verstak.grounding import review_proposal
from verstak.llm import Triage


def invoice(reference: str | None, amount: int, currency: str) -> Triage:
    return Triage(
        category="invoice",
        tool="record_invoice",
        summary="Review invoice",
        reference=reference,
        amount_cents=amount,
        currency=currency,
    )


@pytest.mark.parametrize(
    "source,currency",
    [
        ("Invoice INV-1042 for USD 1,234.50.", "USD"),
        ("Рахунок inv-1042 на 1 234,50 EUR.", "EUR"),
        ("Рахунок INV-1042 на 1234,50 грн.", "UAH"),
    ],
)
def test_review_accepts_explicit_source_facts(source: str, currency: str) -> None:
    report = review_proposal(invoice("INV-1042", 123450, currency), source)
    assert report.accepted
    assert report.findings == []


def test_review_rejects_partial_reference_and_currency_swaps() -> None:
    report = review_proposal(
        invoice("INV-104", 12000, "EUR"),
        "Invoice INV-1042 for USD 120.00. Separate line in EUR 50.00.",
    )
    assert not report.accepted
    assert report.findings == ["reference_not_in_source", "payment_not_in_source"]


@pytest.mark.parametrize(
    "source,cents", [("Credit of -120.00 USD", 12000), ("Credit of −1 120.00 USD", 12000)]
)
def test_review_does_not_mistake_negative_amount_for_payment(source: str, cents: int) -> None:
    report = review_proposal(invoice(None, cents, "USD"), source)
    assert report.findings == ["payment_not_in_source"]
