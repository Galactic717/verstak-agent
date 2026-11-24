from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from verstak.auth import Client, issue_token, read_token
from verstak.config import Settings
from verstak.contracts import RunRequest


def test_request_budget_is_preserved_in_integer_millionths() -> None:
    request = RunRequest(text="Review invoice", max_budget_usd=Decimal("0.002001"))
    assert request.budget_micros == 2001
    assert (
        request.fingerprint()
        == RunRequest.model_validate_json(request.model_dump_json()).fingerprint()
    )


def test_rejects_empty_unbounded_or_sub_millionth_requests() -> None:
    for data in (
        {"text": "   "},
        {"text": "hello", "max_steps": 4},
        {"text": "hello", "max_budget_usd": "0.0000001"},
    ):
        with pytest.raises(ValidationError):
            RunRequest.model_validate(data)


def test_file_only_request_is_valid() -> None:
    assert RunRequest(document_id=uuid4()).text == ""


def test_token_binds_tenant_and_current_scopes() -> None:
    client = Client(secret="example-client-secret", tenant="example", scopes={"runs:read"})
    settings = Settings(jwt_secret="t" * 32, clients_json="{}")
    token = issue_token("reader", client, settings, {"runs:read"})
    principal = read_token(token, settings, {"reader": client})
    assert principal.tenant == "example"
    assert principal.scopes == frozenset({"runs:read"})
    changed = client.model_copy(update={"scopes": {"documents:read"}})
    restricted = read_token(token, settings, {"reader": changed})
    assert restricted.scopes == frozenset()
