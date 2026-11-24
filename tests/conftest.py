import asyncio
import json
import os
import sys
from collections.abc import Iterator
from uuid import uuid4

import pytest

from verstak.config import settings
from verstak.db import engine, sessions


@pytest.fixture
def workflow_stack(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    database = os.environ.get("VERSTAK_TEST_DATABASE_URL")
    broker = os.environ.get("VERSTAK_TEST_REDIS_URL")
    if not database or not broker:
        pytest.skip("set VERSTAK_TEST_DATABASE_URL and VERSTAK_TEST_REDIS_URL")
    tenant = f"test_{uuid4().hex}"
    client_secret = "workflow-fixture-secret"
    monkeypatch.setenv("VERSTAK_DATABASE_URL", database)
    monkeypatch.setenv("VERSTAK_REDIS_URL", broker)
    monkeypatch.setenv("VERSTAK_JWT_SECRET", "workflow-tests-signing-secret-value")
    monkeypatch.setenv("VERSTAK_LLM_PROVIDER", "fixture")
    monkeypatch.setenv(
        "VERSTAK_CLIENTS_JSON",
        json.dumps(
            {
                "operator": {"secret": client_secret, "tenant": tenant, "scopes": ["*"]},
                "reader": {"secret": client_secret, "tenant": tenant, "scopes": ["runs:read"]},
                "sender": {"secret": client_secret, "tenant": tenant, "scopes": ["runs:write"]},
                "external": {
                    "secret": client_secret,
                    "tenant": f"{tenant}_external",
                    "scopes": ["*"],
                },
            }
        ),
    )
    settings.cache_clear()
    sessions.cache_clear()
    engine.cache_clear()
    previous_policy = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous_policy)
        settings.cache_clear()
        sessions.cache_clear()
        engine.cache_clear()
