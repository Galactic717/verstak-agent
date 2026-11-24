import re
import unicodedata

from verstak.contracts import GroundingFinding, GroundingReview
from verstak.llm.triage import Triage

_MONEY = r"(?:[-−]\s*)?(?:[0-9]{1,3}(?:[., ][0-9]{3})+|[0-9]+)(?:[.,][0-9]{1,2})?"
_FRACTION = re.compile(r"[.,]([0-9]{1,2})$")
_ALIASES = {"UAH": r"\bгрн\b", "EUR": "€", "USD": r"\bus\$", "GBP": "£"}
_CORRECTIONS: dict[GroundingFinding, str] = {
    "reference_not_in_source": "Copy the complete reference from the input, or use null.",
    "payment_not_in_source": "Use a currency and amount that appear together in the input, "
    "or set both fields to null.",
}


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _read_cents(value: str) -> int | None:
    decimal = _FRACTION.search(value)
    integer = value[: decimal.start()] if decimal else value
    digits = re.sub(r"[., ]", "", integer)
    if len(digits) > 13:
        return None
    return 100 * int(digits) + (int(decimal[1].ljust(2, "0")) if decimal else 0)


def _matches_payment(source: str, amount_cents: int, currency: str) -> bool:
    code = re.escape(currency.lower())
    currency_pattern = rf"\b{code}\b"
    if alias := _ALIASES.get(currency):
        currency_pattern = rf"(?:{currency_pattern}|{alias})"
    paired = re.compile(
        rf"(?P<prefix>{currency_pattern})\s*(?P<following>{_MONEY})(?!\w|[.,][0-9])"
        rf"|(?<![\w.,−-])(?P<preceding>{_MONEY})\s*(?P<suffix>{currency_pattern})"
    )
    for candidate in paired.finditer(source):
        amount = candidate["following"] or candidate["preceding"]
        if amount.startswith(("-", "−")):
            continue
        if _read_cents(amount) == amount_cents:
            return True
    return False


def review_proposal(proposal: Triage, source: str) -> GroundingReview:
    """Check action-driving scalar fields against the submitted request and attached document."""
    normalized = _normalized(source)
    findings: list[GroundingFinding] = []
    if proposal.reference is not None:
        identifier = re.escape(_normalized(proposal.reference))
        if not re.search(rf"(?<![\w-]){identifier}(?![\w-])", normalized):
            findings.append("reference_not_in_source")
    if proposal.amount_cents is not None and proposal.currency is not None:
        if not _matches_payment(normalized, proposal.amount_cents, proposal.currency):
            findings.append("payment_not_in_source")
    return GroundingReview(accepted=not findings, findings=findings)


def correction_prompt(review: GroundingReview) -> str:
    return " ".join(_CORRECTIONS[finding] for finding in review.findings)
