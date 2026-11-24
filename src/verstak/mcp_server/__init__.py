import argparse
import os
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, TypeAdapter, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from verstak.contracts import Budget, RunRequest

JsonObject = dict[str, object]
JsonResult = list[JsonObject] | JsonObject
_RESULT = TypeAdapter(JsonResult)
_LOOPBACK_HOSTS = ["localhost:*", "127.0.0.1:*", "[::1]:*"]
_LOOPBACK_ORIGINS = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]


def _bearer(authorization: str) -> str | None:
    words = authorization.split()
    if len(words) != 2 or words[0].lower() != "bearer":
        return None
    return words[1]


class _RequireBearer:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if _bearer(headers.get("authorization", "")) is None:
                reply = Response(
                    "Bearer token required", 401, headers={"WWW-Authenticate": "Bearer"}
                )
                await reply(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _create_server(
    api_url: str,
    local_token: str | None = None,
    security: TransportSecuritySettings | None = None,
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    server = FastMCP(
        "verstak-agent",
        instructions="Search tenant knowledge, read workflow runs, and submit triage for review.",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )

    async def request_api(
        method: str,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
        payload: JsonObject | None = None,
        idempotency_key: str | None = None,
    ) -> JsonResult:
        request = server.get_context().request_context.request
        token = (
            _bearer(request.headers.get("authorization", ""))
            if request is not None
            else local_token
        )
        if not token:
            raise ToolError("A Verstak API bearer token is required")
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(
                base_url=api_url,
                transport=client_transport,
                timeout=20,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method, path, headers=headers, params=query, json=payload
                )
        except httpx.RequestError:
            raise ToolError("Verstak API is unavailable") from None
        if response.status_code >= 500:
            raise ToolError("Verstak API is unavailable")
        if not response.is_success:
            messages = {
                401: "Verstak API credentials are invalid or expired",
                403: "The caller lacks the required API scope",
                404: "The requested record was not found",
                409: "The idempotency key was already used with different input",
                422: "Verstak API rejected the tool input",
                429: "The tenant has reached its API rate limit",
            }
            raise ToolError(messages.get(response.status_code, "Verstak API rejected the request"))
        try:
            return _RESULT.validate_json(response.content)
        except ValidationError:
            raise ToolError("Verstak API returned an unexpected response") from None

    @server.tool()
    async def search_knowledge(
        query: Annotated[str, Field(min_length=1, max_length=2_000)],
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> JsonResult:
        """Search the caller's indexed documents and return ranked source excerpts."""
        return await request_api("GET", "/knowledge/search", query={"q": query, "limit": limit})

    @server.tool()
    async def get_run(run_id: UUID) -> JsonResult:
        """Read one workflow run visible to the caller's tenant."""
        return await request_api("GET", f"/runs/{run_id}")

    @server.tool()
    async def get_document(document_id: UUID) -> JsonResult:
        """Read one uploaded document visible to the caller's tenant."""
        return await request_api("GET", f"/documents/{document_id}")

    @server.tool()
    async def trigger_triage(
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[!-~]+$")],
        text: Annotated[str, Field(max_length=12_000)] = "",
        document_id: UUID | None = None,
        max_steps: Annotated[int, Field(ge=1, le=3)] = 3,
        max_budget_usd: Budget = Decimal("0.05"),
    ) -> JsonResult:
        """Submit triage for human review. Reuse the same key when retrying a submission."""
        try:
            run = RunRequest(
                text=text,
                document_id=document_id,
                max_steps=max_steps,
                max_budget_usd=max_budget_usd,
            )
        except ValidationError:
            raise ToolError(
                "A triage request needs text or a document ID and valid limits"
            ) from None
        return await request_api(
            "POST",
            "/workflows/triage/runs",
            payload=run.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )

    return server


def create_http_app(
    api_url: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    server = _create_server(api_url, security=security, client_transport=client_transport)
    app = server.streamable_http_app()
    app.add_middleware(_RequireBearer)
    return app


def _configured_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve Verstak's scoped workflow and knowledge tools"
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default=os.environ.get("VERSTAK_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("VERSTAK_MCP_PORT", "8001"))
    args = parser.parse_args()
    api_url = os.environ.get("VERSTAK_API_URL", "http://127.0.0.1:8000")
    if args.transport == "stdio":
        token = os.environ.get("VERSTAK_MCP_API_TOKEN")
        if not token:
            parser.error("VERSTAK_MCP_API_TOKEN is required for stdio")
        _create_server(api_url, local_token=token).run(transport="stdio")
        return
    app = create_http_app(
        api_url,
        _configured_list("VERSTAK_MCP_ALLOWED_HOSTS", _LOOPBACK_HOSTS),
        _configured_list("VERSTAK_MCP_ALLOWED_ORIGINS", _LOOPBACK_ORIGINS),
    )
    uvicorn.run(app, host=args.host, port=args.port)
