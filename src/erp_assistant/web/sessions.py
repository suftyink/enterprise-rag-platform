import base64
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Annotated, Protocol, cast
from urllib.parse import urlencode

import httpx
from fastapi import Depends, HTTPException, Request, Response, status
from redis.asyncio import Redis

from erp_assistant.api.dependencies import get_identity_access, request_trace_id
from erp_assistant.core.config import Settings, get_settings
from erp_assistant.identity import ActorContext, IdentityAccess, IdentityDenied

SESSION_COOKIE = "erp_internal_session"
AUTH_COOKIE = "erp_internal_auth"


@dataclass(frozen=True)
class WebSession:
    credential: str
    csrf_token: str


@dataclass(frozen=True)
class OidcTransaction:
    state: str
    verifier: str
    redirect_uri: str


class WebSessionStore(Protocol):
    async def put_session(self, session_id: str, value: WebSession, ttl: int) -> None: ...

    async def get_session(self, session_id: str) -> WebSession | None: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def put_transaction(
        self, transaction_id: str, value: OidcTransaction, ttl: int
    ) -> None: ...

    async def pop_transaction(self, transaction_id: str) -> OidcTransaction | None: ...


class RedisWebSessionStore:
    def __init__(self, client: Redis) -> None:
        self._client = client

    async def put_session(self, session_id: str, value: WebSession, ttl: int) -> None:
        await self._client.set(
            f"internal-web:session:{session_id}",
            json.dumps(asdict(value), separators=(",", ":")),
            ex=ttl,
        )

    async def get_session(self, session_id: str) -> WebSession | None:
        raw = await self._client.get(f"internal-web:session:{session_id}")
        if raw is None:
            return None
        payload = json.loads(raw)
        return WebSession(
            credential=str(payload["credential"]), csrf_token=str(payload["csrf_token"])
        )

    async def delete_session(self, session_id: str) -> None:
        await self._client.delete(f"internal-web:session:{session_id}")

    async def put_transaction(
        self, transaction_id: str, value: OidcTransaction, ttl: int
    ) -> None:
        await self._client.set(
            f"internal-web:auth:{transaction_id}",
            json.dumps(asdict(value), separators=(",", ":")),
            ex=ttl,
        )

    async def pop_transaction(self, transaction_id: str) -> OidcTransaction | None:
        key = f"internal-web:auth:{transaction_id}"
        async with self._client.pipeline(transaction=True) as pipeline:
            pipeline.get(key)
            pipeline.delete(key)
            raw, _ = await pipeline.execute()
        if raw is None:
            return None
        payload = json.loads(raw)
        return OidcTransaction(
            state=str(payload["state"]),
            verifier=str(payload["verifier"]),
            redirect_uri=str(payload["redirect_uri"]),
        )


@lru_cache
def _redis_store(redis_url: str) -> RedisWebSessionStore:
    return RedisWebSessionStore(Redis.from_url(redis_url, decode_responses=True))


def get_web_session_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebSessionStore:
    return _redis_store(settings.redis_url)


async def create_web_session(
    response: Response,
    credential: str,
    store: WebSessionStore,
    settings: Settings,
) -> str:
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    await store.put_session(
        session_id,
        WebSession(credential=credential, csrf_token=csrf_token),
        settings.internal_web_session_ttl_seconds,
    )
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=settings.internal_web_session_ttl_seconds,
        httponly=True,
        secure=settings.app_env.casefold() in {"prod", "production"},
        samesite="lax",
        path="/",
    )
    return csrf_token


async def get_web_session(
    request: Request,
    store: Annotated[WebSessionStore, Depends(get_web_session_store)],
) -> WebSession:
    session_id = request.cookies.get(SESSION_COOKIE, "")
    session = await store.get_session(session_id) if session_id else None
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTHENTICATION_REQUIRED", "message": "请使用企业身份登录"},
        )
    request.state.web_session = session
    return session


async def get_web_actor(
    request: Request,
    session: Annotated[WebSession, Depends(get_web_session)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
) -> ActorContext:
    try:
        return await access.authenticate(session.credential, trace_id=request_trace_id(request))
    except IdentityDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


async def require_csrf(
    request: Request,
    session: Annotated[WebSession, Depends(get_web_session)],
) -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    if not secrets.compare_digest(supplied, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "CSRF_VALIDATION_FAILED", "message": "请求验证失败，请刷新页面"},
        )


def oidc_authorization_url(settings: Settings, transaction: OidcTransaction) -> str:
    if not settings.oidc_authorization_url or not settings.oidc_client_id:
        raise ValueError("OIDC 浏览器登录配置不完整")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": transaction.redirect_uri,
            "scope": settings.oidc_scopes,
            "state": transaction.state,
            "code_challenge": _pkce_challenge(transaction.verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{settings.oidc_authorization_url}?{query}"


async def exchange_oidc_code(
    settings: Settings, *, code: str, transaction: OidcTransaction
) -> str:
    if not settings.oidc_token_url or not settings.oidc_client_id:
        raise ValueError("OIDC 浏览器登录配置不完整")
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": transaction.redirect_uri,
        "client_id": settings.oidc_client_id,
        "code_verifier": transaction.verifier,
    }
    client_secret = settings.oidc_client_secret.get_secret_value()
    if client_secret:
        payload["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(settings.oidc_token_url, data=payload)
        response.raise_for_status()
    body = cast(dict[str, object], response.json())
    credential = body.get("access_token")
    if not isinstance(credential, str) or not credential:
        raise ValueError("身份提供方未返回访问令牌")
    return credential


def new_oidc_transaction(settings: Settings) -> tuple[str, OidcTransaction]:
    transaction_id = secrets.token_urlsafe(24)
    redirect_uri = f"{settings.internal_web_base_url.rstrip('/')}/internal/auth/callback"
    transaction = OidcTransaction(
        state=secrets.token_urlsafe(32),
        verifier=secrets.token_urlsafe(64),
        redirect_uri=redirect_uri,
    )
    return transaction_id, transaction


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
