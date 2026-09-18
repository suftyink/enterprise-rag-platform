from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.api.dependencies import get_identity_access, request_trace_id
from erp_assistant.core.config import Settings, get_settings
from erp_assistant.identity import (
    ActorContext,
    IdentityAccess,
    IdentityAction,
    IdentityDenied,
    IdentityResource,
)
from erp_assistant.infrastructure.database import get_session
from erp_assistant.operations.knowledge_initialization import (
    InitializationCommand,
    InitializationError,
    KnowledgeInitialization,
)
from erp_assistant.rag.index_provisioning import (
    IndexProvisioner,
    MilvusIndexProvisioner,
)
from erp_assistant.rag.types import DataClassification, KnowledgeScope
from erp_assistant.web.schemas import (
    InitializationContinueWebRequest,
    InitializationMigrateWebRequest,
    InitializationMutationWebRequest,
    InitializationStartWebRequest,
)
from erp_assistant.web.sessions import get_web_actor, require_csrf
from erp_assistant.workers.dispatcher import (
    InitializationDispatcher,
    get_initialization_dispatcher,
)

router = APIRouter()


def get_initialization_index_provisioner() -> IndexProvisioner:
    return MilvusIndexProvisioner()


def get_knowledge_initialization(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    provisioner: Annotated[IndexProvisioner, Depends(get_initialization_index_provisioner)],
    dispatcher: Annotated[
        InitializationDispatcher, Depends(get_initialization_dispatcher)
    ],
) -> KnowledgeInitialization:
    return KnowledgeInitialization(session, settings, provisioner, dispatcher)


@router.get("/internal/api/initialization")
async def initialization_status(
    request: Request,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    initialization: Annotated[
        KnowledgeInitialization, Depends(get_knowledge_initialization)
    ],
    classification: Annotated[
        DataClassification, Query(alias="dataClassification")
    ] = DataClassification.ORDINARY,
) -> dict[str, object]:
    await _authorize(actor, access, request)
    if classification is DataClassification.UNCLASSIFIED:
        raise _error(422, "INVALID_DATA_CLASSIFICATION", "初始化不支持未分类数据")
    return (await initialization.status(classification)).to_dict()


@router.post("/internal/api/initialization")
async def start_initialization(
    request: Request,
    payload: InitializationStartWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    initialization: Annotated[
        KnowledgeInitialization, Depends(get_knowledge_initialization)
    ],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    try:
        result = await initialization.start(
            InitializationCommand(
                classification=DataClassification(payload.data_classification),
                idempotency_key=payload.idempotency_key,
                runtime_key=payload.runtime_key,
                actor_id=actor.subject_id,
                trace_id=request_trace_id(request),
            )
        )
    except InitializationError as exc:
        raise _initialization_error(exc) from exc
    return result.to_dict()


@router.post("/internal/api/initialization/migrate")
async def migrate_initialization(
    request: Request,
    payload: InitializationMigrateWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    initialization: Annotated[
        KnowledgeInitialization, Depends(get_knowledge_initialization)
    ],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    try:
        result = await initialization.migrate_active(
            DataClassification(payload.data_classification),
            idempotency_key=payload.idempotency_key,
            actor_id=actor.subject_id,
            trace_id=request_trace_id(request),
        )
    except InitializationError as exc:
        raise _initialization_error(exc) from exc
    return result.to_dict()


@router.post("/internal/api/initialization/{run_id}/continue")
async def continue_initialization(
    run_id: UUID,
    request: Request,
    payload: InitializationContinueWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    initialization: Annotated[
        KnowledgeInitialization, Depends(get_knowledge_initialization)
    ],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    try:
        result = await initialization.continue_to_candidate(
            run_id,
            release_version=payload.release_version,
            actor_id=actor.subject_id,
            trace_id=request_trace_id(request),
            expected_revision=payload.expected_revision,
        )
    except InitializationError as exc:
        raise _initialization_error(exc) from exc
    return result.to_dict()


@router.post("/internal/api/initialization/{run_id}/retry")
async def retry_initialization(
    run_id: UUID,
    request: Request,
    payload: InitializationMutationWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    initialization: Annotated[
        KnowledgeInitialization, Depends(get_knowledge_initialization)
    ],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    try:
        result = await initialization.retry(
            run_id,
            actor_id=actor.subject_id,
            trace_id=request_trace_id(request),
            expected_revision=payload.expected_revision,
        )
    except InitializationError as exc:
        raise _initialization_error(exc) from exc
    return result.to_dict()


@router.post("/internal/api/initialization/{run_id}/cancel")
async def cancel_initialization(
    run_id: UUID,
    request: Request,
    payload: InitializationMutationWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    initialization: Annotated[
        KnowledgeInitialization, Depends(get_knowledge_initialization)
    ],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    try:
        result = await initialization.cancel(
            run_id,
            actor_id=actor.subject_id,
            trace_id=request_trace_id(request),
            expected_revision=payload.expected_revision,
        )
    except InitializationError as exc:
        raise _initialization_error(exc) from exc
    return result.to_dict()


async def _authorize(
    actor: ActorContext,
    access: IdentityAccess,
    request: Request,
) -> None:
    try:
        await access.authorize(
            actor,
            IdentityAction.MANAGE_PILOT,
            IdentityResource(
                "knowledge_initialization",
                "internal-web",
                KnowledgeScope.GLOBAL,
                "GLOBAL",
            ),
            trace_id=request_trace_id(request),
        )
    except IdentityDenied as exc:
        raise _error(403, exc.code, "仅系统管理员可初始化知识库") from exc


def _initialization_error(exc: InitializationError) -> HTTPException:
    if exc.code == "INITIALIZATION_NOT_FOUND":
        status_code = 404
    elif exc.code in {
        "RUNTIME_DEPLOYMENT_UNAVAILABLE",
        "IDEMPOTENCY_KEY_REQUIRED",
        "INVALID_DATA_CLASSIFICATION",
    }:
        status_code = 422
    else:
        status_code = 409
    detail: dict[str, object] = {"code": exc.code, "message": exc.message}
    if exc.run_id is not None:
        detail["runId"] = str(exc.run_id)
    return HTTPException(status_code=status_code, detail=detail)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
