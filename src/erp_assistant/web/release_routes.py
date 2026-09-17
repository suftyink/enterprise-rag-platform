from collections.abc import Awaitable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from erp_assistant.api.dependencies import get_identity_access, request_trace_id
from erp_assistant.identity import (
    ActorContext,
    IdentityAccess,
    IdentityAction,
    IdentityDenied,
    IdentityResource,
)
from erp_assistant.operations.release_contracts import (
    CorpusReleaseView,
    ReleaseApproval,
    ReleaseError,
    RolloutState,
)
from erp_assistant.operations.releases import (
    CorpusReleaseWorkflow,
    get_corpus_release_workflow,
)
from erp_assistant.rag.composition import get_knowledge_corpus
from erp_assistant.rag.knowledge_corpus import KnowledgeCorpus
from erp_assistant.rag.knowledge_corpus_contracts import CorpusError, LifecycleAction
from erp_assistant.rag.types import KnowledgeScope
from erp_assistant.web.schemas import (
    ReleaseApprovalWebRequest,
    ReleaseBootstrapWebRequest,
    ReleaseCandidateWebRequest,
    ReleaseReasonWebRequest,
    ReleaseRolloutWebRequest,
)
from erp_assistant.web.sessions import get_web_actor, require_csrf

router = APIRouter()


def get_release_knowledge_corpus() -> KnowledgeCorpus:
    return get_knowledge_corpus()


@router.get("/internal/api/releases")
async def list_releases(
    request: Request,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    workflow: Annotated[CorpusReleaseWorkflow, Depends(get_corpus_release_workflow)],
) -> list[dict[str, object]]:
    await _authorize(actor, access, request)
    return [item.to_dict() for item in await workflow.list()]


@router.post("/internal/api/releases/bootstrap")
async def bootstrap_release(
    request: Request,
    payload: ReleaseBootstrapWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    workflow: Annotated[CorpusReleaseWorkflow, Depends(get_corpus_release_workflow)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    return await _run(
        workflow.bootstrap(
            payload.profile_id,
            actor_id=actor.subject_id,
            trace_id=request_trace_id(request),
        )
    )


@router.post("/internal/api/releases")
async def create_release(
    request: Request,
    payload: ReleaseCandidateWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    workflow: Annotated[CorpusReleaseWorkflow, Depends(get_corpus_release_workflow)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    return await _run(
        workflow.create_candidate(
            payload.release_version,
            payload.profile_id,
            actor_id=actor.subject_id,
            trace_id=request_trace_id(request),
        )
    )


@router.post("/internal/api/releases/{release_id}/sync-build")
async def sync_release_build(
    request: Request,
    release_id: UUID,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    workflow: Annotated[CorpusReleaseWorkflow, Depends(get_corpus_release_workflow)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    return await _run(
        workflow.sync_build(
            release_id,
            actor_id=actor.subject_id,
            trace_id=request_trace_id(request),
        )
    )


@router.post("/internal/api/releases/{release_id}/readiness")
async def bind_release_readiness(
    request: Request,
    release_id: UUID,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    workflow: Annotated[CorpusReleaseWorkflow, Depends(get_corpus_release_workflow)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    return await _run(
        workflow.bind_readiness(
            release_id,
            actor_id=actor.subject_id,
            trace_id=request_trace_id(request),
        )
    )


@router.post("/internal/api/releases/{release_id}/activate")
async def activate_release(
    request: Request,
    release_id: UUID,
    payload: ReleaseApprovalWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    workflow: Annotated[CorpusReleaseWorkflow, Depends(get_corpus_release_workflow)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    return await _run(
        workflow.activate(
            release_id,
            ReleaseApproval(
                approved_by=actor.subject_id,
                pilot_scope=payload.pilot_scope,
                capacity=payload.capacity,
                monitoring_owner=payload.monitoring_owner,
                rollback_owner=payload.rollback_owner,
                reason=payload.reason,
            ),
            trace_id=request_trace_id(request),
        )
    )


@router.post("/internal/api/releases/{release_id}/rollback")
async def rollback_release(
    request: Request,
    release_id: UUID,
    payload: ReleaseReasonWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    workflow: Annotated[CorpusReleaseWorkflow, Depends(get_corpus_release_workflow)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    return await _run(
        workflow.rollback(
            release_id,
            actor_id=actor.subject_id,
            reason=payload.reason,
            trace_id=request_trace_id(request),
        )
    )


@router.post("/internal/api/releases/{release_id}/rollout")
async def manage_release_rollout(
    request: Request,
    release_id: UUID,
    payload: ReleaseRolloutWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    workflow: Annotated[CorpusReleaseWorkflow, Depends(get_corpus_release_workflow)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    return await _run(
        workflow.set_rollout(
            release_id,
            RolloutState(payload.state),
            subject_ids=tuple(payload.subject_ids),
            actor_id=actor.subject_id,
            reason=payload.reason,
            trace_id=request_trace_id(request),
        )
    )


@router.post("/internal/api/releases/{release_id}/documents/{document_id}/emergency-withdraw")
async def emergency_withdraw(
    request: Request,
    release_id: UUID,
    document_id: UUID,
    payload: ReleaseReasonWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    workflow: Annotated[CorpusReleaseWorkflow, Depends(get_corpus_release_workflow)],
    corpus: Annotated[KnowledgeCorpus, Depends(get_release_knowledge_corpus)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    await _authorize(actor, access, request)
    await _run_void(workflow.require_active_member(release_id, document_id))
    try:
        view = await corpus.inspect(document_id, actor, trace_id=request_trace_id(request))
        await corpus.transition(
            document_id,
            LifecycleAction.WITHDRAW,
            actor,
            reason=payload.reason,
            expected_revision=view.revision,
            trace_id=request_trace_id(request),
        )
    except CorpusError as exc:
        raise _error(409, exc.code, exc.message) from exc
    return await _run(
        workflow.mark_degraded(
            release_id,
            document_id=document_id,
            actor_id=actor.subject_id,
            reason=payload.reason,
            reason_code="EMERGENCY_WITHDRAWAL",
            trace_id=request_trace_id(request),
        )
    )


async def _authorize(
    actor: ActorContext,
    access: IdentityAccess,
    request: Request,
) -> None:
    try:
        await access.authorize(
            actor,
            IdentityAction.MANAGE_PILOT,
            IdentityResource("corpus_release", "production", KnowledgeScope.GLOBAL, "GLOBAL"),
            trace_id=request_trace_id(request),
        )
    except IdentityDenied as exc:
        raise _error(403, exc.code, "仅系统管理员可管理知识发布") from exc


async def _run(operation: Awaitable[CorpusReleaseView]) -> dict[str, object]:
    try:
        result = await operation
    except ReleaseError as exc:
        status_code = 404 if exc.code.endswith("NOT_FOUND") else 409
        raise _error(status_code, exc.code, exc.message) from exc
    return result.to_dict()


async def _run_void(operation: Awaitable[None]) -> None:
    try:
        await operation
    except ReleaseError as exc:
        status_code = 404 if exc.code.endswith("NOT_FOUND") else 409
        raise _error(status_code, exc.code, exc.message) from exc


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
