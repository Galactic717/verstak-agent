import sys
from uuid import UUID

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from verstak.mcp_server import create_http_app

RUN_ID = UUID("790869b0-bfa6-4340-bbd8-50fb1d0570d0")


@pytest.mark.asyncio
async def test_stdio_mcp_initializes_without_an_http_server() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from verstak.mcp_server import main; main()", "--transport", "stdio"],
        env={"VERSTAK_MCP_API_TOKEN": "test-token"},
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "verstak-agent"
        response = await session.list_tools()
        assert "search_knowledge" in {tool.name for tool in response.tools}


@pytest.mark.asyncio
async def test_http_mcp_forwards_only_the_callers_credentials() -> None:
    requests: list[httpx.Request] = []

    async def backend(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"id": str(RUN_ID), "status": "queued"})

    app = create_http_app("http://api", ["mcp.test"], [], httpx.MockTransport(backend))
    async with app.router.lifespan_context(app):
        for token, key in (("first", "key-a"), ("second", "key-b")):
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    headers={"Authorization": f"Bearer {token}"},
                ) as client,
                streamable_http_client("http://mcp.test/mcp", http_client=client) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "search_knowledge",
                    "get_run",
                    "get_document",
                    "trigger_triage",
                }
                result = await session.call_tool(
                    "trigger_triage", {"text": "Route this invoice", "idempotency_key": key}
                )
                assert result.isError is False
    assert [request.headers["Authorization"] for request in requests] == [
        "Bearer first",
        "Bearer second",
    ]
    assert [request.headers["Idempotency-Key"] for request in requests] == ["key-a", "key-b"]
    assert all(request.url.path == "/workflows/triage/runs" for request in requests)


@pytest.mark.asyncio
async def test_http_mcp_denies_missing_bearer_and_unapproved_origins() -> None:
    app = create_http_app("http://api", ["mcp.test"], ["https://allowed.example"])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp.test"
        ) as client,
    ):
        assert (await client.post("/mcp", json={})).status_code == 401
        response = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer demo", "Origin": "https://other.example"},
            json={},
        )
        assert response.status_code == 403
