import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse

from erp_assistant.api.dependencies import get_identity_access, request_trace_id
from erp_assistant.core.config import Settings, get_settings
from erp_assistant.identity import ActorContext, IdentityAccess, IdentityDenied
from erp_assistant.web.sessions import (
    AUTH_COOKIE,
    SESSION_COOKIE,
    WebSessionStore,
    create_web_session,
    exchange_oidc_code,
    get_web_actor,
    get_web_session_store,
    new_oidc_transaction,
    oidc_authorization_url,
    require_csrf,
)

router = APIRouter()
_STATIC = Path(__file__).with_name("static")
_ASSETS = {
    "app.css": "text/css",
    "app.js": "text/javascript",
    "answer.js": "text/javascript",
    "core.js": "text/javascript",
    "governance.js": "text/javascript",
}


@router.get("/internal", include_in_schema=False)
async def internal_web() -> FileResponse:
    return FileResponse(_STATIC / "index.html", media_type="text/html")


@router.get("/internal/assets/{asset_name}", include_in_schema=False)
async def internal_asset(asset_name: str) -> FileResponse:
    media_type = _ASSETS.get(asset_name)
    if media_type is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(_STATIC / asset_name, media_type=media_type)


@router.get("/internal/auth/login", include_in_schema=False)
async def web_login(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[WebSessionStore, Depends(get_web_session_store)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
) -> Response:
    if settings.identity_provider == "test":
        subject = settings.internal_web_test_subject.strip()
        if not subject or settings.app_env.casefold() in {"prod", "production"}:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "WEB_SSO_NOT_CONFIGURED", "message": "浏览器登录未配置"},
            )
        await _authenticate_web_credential(access, subject, request)
        response = RedirectResponse("/internal", status_code=status.HTTP_303_SEE_OTHER)
        await create_web_session(response, subject, store, settings)
        return response
    transaction_id, transaction = new_oidc_transaction(settings)
    await store.put_transaction(
        transaction_id, transaction, settings.internal_web_auth_state_ttl_seconds
    )
    response = RedirectResponse(oidc_authorization_url(settings, transaction))
    response.set_cookie(
        AUTH_COOKIE,
        transaction_id,
        max_age=settings.internal_web_auth_state_ttl_seconds,
        httponly=True,
        secure=settings.app_env.casefold() in {"prod", "production"},
        samesite="lax",
        path="/internal/auth",
    )
    return response


@router.get("/internal/auth/callback", include_in_schema=False)
async def web_callback(
    request: Request,
    code: str,
    state: str,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[WebSessionStore, Depends(get_web_session_store)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
) -> Response:
    transaction_id = request.cookies.get(AUTH_COOKIE, "")
    transaction = await store.pop_transaction(transaction_id) if transaction_id else None
    if transaction is None or not secrets.compare_digest(transaction.state, state):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "OIDC_STATE_INVALID", "message": "登录请求已失效，请重试"},
        )
    try:
        credential = await exchange_oidc_code(settings, code=code, transaction=transaction)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "IDENTITY_PROVIDER_UNAVAILABLE", "message": "企业登录暂时不可用"},
        ) from exc
    await _authenticate_web_credential(access, credential, request)
    response = RedirectResponse("/internal", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(AUTH_COOKIE, path="/internal/auth")
    await create_web_session(response, credential, store, settings)
    return response


@router.get("/internal/api/session")
async def web_session(
    request: Request,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    session = request.state.web_session
    return {
        "subjectId": actor.subject_id,
        "tenantId": actor.tenant_id,
        "roles": sorted(role.value for role in actor.roles),
        "knowledgeScopes": [
            {"scope": scope.scope.value, "scopeId": scope.scope_id}
            for scope in sorted(
                actor.knowledge_scopes, key=lambda item: (item.scope.value, item.scope_id)
            )
        ],
        "upload": {
            "maxFileSize": settings.knowledge_max_file_size,
            "formats": ["PDF", "DOCX", "Markdown", "HTML"],
        },
        "csrfToken": session.csrf_token,
        "traceId": str(request_trace_id(request)),
    }


@router.post("/internal/api/logout", dependencies=[Depends(require_csrf)])
async def web_logout(
    request: Request,
    store: Annotated[WebSessionStore, Depends(get_web_session_store)],
) -> Response:
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if session_id:
        await store.delete_session(session_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


async def _authenticate_web_credential(
    access: IdentityAccess, credential: str, request: Request
) -> ActorContext:
    try:
        return await access.authenticate(credential, trace_id=request_trace_id(request))
    except IdentityDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
