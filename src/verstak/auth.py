import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated
from uuid import uuid4

import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.openapi.models import OAuthFlowClientCredentials, OAuthFlows
from fastapi.security import OAuth2
from fastapi.security.utils import get_authorization_scheme_param
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

from verstak.config import Settings, settings

SCOPES = {
    "runs:read": "Read tenant runs",
    "runs:write": "Submit tenant runs",
    "approvals:write": "Decide pending tenant approvals",
    "documents:read": "Read tenant documents and search knowledge",
    "documents:write": "Ingest tenant documents",
    "evals:read": "Read tenant evaluation reports",
    "evals:write": "Record tenant evaluation reports",
}
ISSUER = "verstak-agent"
ClientID = Annotated[str, StringConstraints(min_length=1, max_length=200)]
oauth2 = OAuth2(
    flows=OAuthFlows(
        clientCredentials=OAuthFlowClientCredentials(tokenUrl="/oauth/token", scopes=SCOPES)
    ),
    auto_error=False,
)
router = APIRouter(tags=["authorization"])


class Client(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    secret: str = Field(min_length=12, repr=False)
    tenant: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    scopes: set[str]
    api_key: str | None = Field(default=None, min_length=24, repr=False)


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    scopes: frozenset[str]


@lru_cache
def clients(raw: str) -> dict[str, Client]:
    return TypeAdapter(dict[ClientID, Client]).validate_json(raw)


def allowed(client: Client) -> set[str]:
    return set(SCOPES) if "*" in client.scopes else client.scopes.intersection(SCOPES)


def issue_token(subject: str, client: Client, cfg: Settings, scopes: set[str]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": ISSUER,
            "sub": subject,
            "tenant": client.tenant,
            "scope": " ".join(sorted(scopes)),
            "iat": now,
            "exp": now + timedelta(seconds=cfg.token_seconds),
            "jti": str(uuid4()),
        },
        cfg.jwt_secret,
        algorithm="HS256",
    )


def read_token(token: str, cfg: Settings, registered: dict[str, Client]) -> Principal:
    claims = jwt.decode(
        token,
        cfg.jwt_secret,
        algorithms=["HS256"],
        issuer=ISSUER,
        audience=ISSUER,
        options={"require": ["exp", "iat", "sub", "tenant", "scope"]},
    )
    subject = claims["sub"]
    client = registered.get(subject)
    if client is None or client.tenant != claims["tenant"] or not isinstance(claims["scope"], str):
        raise InvalidTokenError("invalid client claims")
    scopes = set(claims["scope"].split()).intersection(allowed(client))
    return Principal(subject, client.tenant, frozenset(scopes))


def _configured() -> tuple[Settings, dict[str, Client]]:
    cfg = settings()
    if len(cfg.jwt_secret.encode()) < 32:
        raise HTTPException(503, "Authorization has not been configured")
    try:
        return cfg, clients(cfg.clients_json)
    except ValidationError:
        raise HTTPException(503, "Authorization configuration is invalid") from None


def _same_secret(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def _unauthorized() -> HTTPException:
    return HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})


class Authorize:
    def __init__(self, scope: str) -> None:
        self.scope = scope

    async def __call__(
        self,
        request: Request,
        authorization: Annotated[str | None, Depends(oauth2)],
    ) -> Principal:
        cfg, registered = _configured()
        scheme, token = get_authorization_scheme_param(authorization)
        principal: Principal | None = None
        if scheme.lower() == "bearer" and token:
            try:
                principal = read_token(token, cfg, registered)
            except InvalidTokenError:
                raise _unauthorized() from None
        elif api_key := request.headers.get("X-API-Key"):
            for subject, client in registered.items():
                if client.api_key and _same_secret(api_key, client.api_key):
                    principal = Principal(subject, client.tenant, frozenset(allowed(client)))
                    break
        if principal is None:
            raise _unauthorized()
        if self.scope not in principal.scopes:
            raise HTTPException(403, "Insufficient scope")
        return principal


def require(scope: str) -> Authorize:
    return Authorize(scope)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    scope: str


@router.post("/oauth/token")
async def token_endpoint(
    request: Request,
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    scope: Annotated[str, Form()] = "",
) -> TokenResponse:
    if grant_type != "client_credentials":
        raise HTTPException(400, "unsupported_grant_type")
    # Swagger UI supports both HTTP Basic and form-based client authentication.
    scheme, credentials = get_authorization_scheme_param(request.headers.get("Authorization"))
    if scheme.lower() == "basic":
        import base64
        from binascii import Error
        from urllib.parse import unquote_plus

        try:
            name, secret = base64.b64decode(credentials, validate=True).decode().split(":", 1)
            client_id, client_secret = unquote_plus(name), unquote_plus(secret)
        except (ValueError, Error):
            raise _unauthorized() from None
    cfg, registered = _configured()
    client = registered.get(client_id or "")
    if client_id is None or client is None or not _same_secret(client_secret or "", client.secret):
        raise _unauthorized()
    requested = set(scope.split()) if scope.strip() else allowed(client)
    if not requested <= allowed(client):
        raise HTTPException(400, "invalid_scope")
    return TokenResponse(
        access_token=issue_token(client_id, client, cfg, requested),
        expires_in=cfg.token_seconds,
        scope=" ".join(sorted(requested)),
    )
