import json

import httpx
import pytest

from verstak.api import app
from verstak.config import settings


async def test_authorization_transports_and_safe_configuration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "synthetic-api-key-for-test-only"
    secret = "synthetic-client-secret"
    monkeypatch.setenv("VERSTAK_JWT_SECRET", "synthetic-signing-secret-for-auth-test")
    monkeypatch.setenv(
        "VERSTAK_CLIENTS_JSON",
        json.dumps(
            {
                "test": {
                    "secret": secret,
                    "api_key": api_key,
                    "tenant": "auth_test",
                    "scopes": ["runs:read"],
                }
            }
        ),
    )
    settings.cache_clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://authorization.test"
    ) as client:
        try:
            grant = {"grant_type": "client_credentials"}
            result = await client.post("/oauth/token", data=grant, auth=("test", secret))
            assert result.status_code == 200
            assert result.json()["scope"] == "runs:read"
            unauthorized = await client.get(
                "/workflows", headers={"Authorization": "Bearer invalid"}
            )
            assert unauthorized.status_code == 401
            assert (
                await client.get("/workflows", headers={"X-API-Key": api_key})
            ).status_code == 200
            forbidden = await client.post(
                "/oauth/token", auth=("test", secret), data={**grant, "scope": "approvals:write"}
            )
            assert forbidden.status_code == 400
            assert (
                await client.post("/oauth/token", data=grant, auth=("test", "wrong"))
            ).status_code == 401
            monkeypatch.setenv("VERSTAK_CLIENTS_JSON", secret)
            settings.cache_clear()
            malformed = await client.post("/oauth/token", data=grant, auth=("test", secret))
            assert malformed.status_code == 503
            assert secret not in malformed.text
        finally:
            settings.cache_clear()
