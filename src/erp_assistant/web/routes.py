from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.api.dependencies import (
    get_identity_access,
    get_knowledge_assistant,
    request_trace_id,
)
from erp_assistant.api.knowledge_schemas import KnowledgeDocumentResponse
from erp_assistant.api.routes.knowledge import document_response, upload_document
from erp_assistant.core.config import Settings, get_settings
from erp_assistant.identity import (
    ActorContext,
    IdentityAccess,
    IdentityAction,
    IdentityDenied,
    IdentityResource,
    IdentityRole,
)
from erp_assistant.infrastructure.database import get_session
from erp_assistant.infrastructure.object_storage import ObjectStorage, get_object_storage
from erp_assistant.models.knowledge import KnowledgeDocument
from erp_assistant.rag.composition import get_knowledge_corpus
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.knowledge_assistant import (
    AnswerRequest,
    FeedbackRating,
    FeedbackRequest,
    KnowledgeAssistant,
)
from erp_assistant.rag.knowledge_corpus import KnowledgeCorpus
from erp_assistant.rag.knowledge_corpus_contracts import CorpusError
from erp_assistant.rag.knowledge_queries import latest_job
from erp_assistant.rag.types import KnowledgeScope
from erp_assistant.web.pilot import PilotAccessGate, get_pilot_access_gate
from erp_assistant.web.schemas import (
    AnswerWebRequest,
    FeedbackWebRequest,
    LifecycleWebRequest,
    PilotAccessWebRequest,
    WebFeedbackReceipt,
)
from erp_assistant.web.sessions import get_web_actor, require_csrf
from erp_assistant.workers.dispatcher import DocumentDispatcher, get_document_dispatcher

router = APIRouter()


def get_web_knowledge_corpus() -> KnowledgeCorpus:
    return get_knowledge_corpus()


async def get_pilot_actor(
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    gate: Annotated[PilotAccessGate, Depends(get_pilot_access_gate)],
) -> ActorContext:
    await gate.require_enabled()
    return actor


@router.post("/internal/api/answers")
async def web_answer(
    request: Request,
    payload: AnswerWebRequest,
    actor: Annotated[ActorContext, Depends(get_pilot_actor)],
    assistant: Annotated[KnowledgeAssistant, Depends(get_knowledge_assistant)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    trace_id = request_trace_id(request)
    try:
        outcome = await assistant.ask(
            AnswerRequest(question=payload.question.strip(), actor=actor, trace_id=trace_id)
        )
    except RagDependencyError as exc:
        raise _error(503, exc.code, exc.message) from exc
    except RagError as exc:
        status_code = 404 if exc.code == "RESOURCE_NOT_FOUND" else 422
        raise _error(status_code, exc.code, exc.message) from exc
    except ValueError as exc:
        code = getattr(exc, "code", "INVALID_QUESTION")
        message = getattr(exc, "message", "问题无效")
        raise _error(422, code, message) from exc
    return {
        "traceId": str(outcome.trace_id),
        "decision": outcome.decision.value,
        "reason": outcome.reason,
        "units": [
            {"text": unit.text, "evidenceIds": list(unit.evidence_ids)}
            for unit in outcome.units
        ],
        "citations": [
            {
                "evidenceId": item.evidence_id,
                "documentId": str(item.document_id),
                "documentKey": item.document_key,
                "documentVersion": item.document_version,
                "title": item.title,
                "pageStart": item.page_start,
                "pageEnd": item.page_end,
                "sectionTitle": item.section_title,
                "headingPath": list(item.heading_path),
                "anchor": item.anchor,
                "sourceLocator": item.source_locator,
                "excerpt": item.excerpt,
            }
            for item in outcome.citations
        ],
    }


@router.post(
    "/internal/api/answers/{trace_id}/feedback", response_model=WebFeedbackReceipt
)
async def web_feedback(
    trace_id: UUID,
    payload: FeedbackWebRequest,
    actor: Annotated[ActorContext, Depends(get_pilot_actor)],
    assistant: Annotated[KnowledgeAssistant, Depends(get_knowledge_assistant)],
    _: Annotated[None, Depends(require_csrf)],
) -> WebFeedbackReceipt:
    try:
        receipt = await assistant.submit_feedback(
            FeedbackRequest(
                trace_id=trace_id,
                actor=actor,
                rating=FeedbackRating(payload.rating),
                reason=payload.reason,
            )
        )
    except (RagError, ValueError) as exc:
        raise _error(
            422,
            getattr(exc, "code", "INVALID_FEEDBACK"),
            getattr(exc, "message", "反馈无效"),
        ) from exc
    return WebFeedbackReceipt(
        feedbackId=receipt.feedback_id,
        traceId=receipt.trace_id,
        rating=receipt.rating.value,
        requiresReview=receipt.requires_review,
    )


@router.get("/internal/api/documents", response_model=list[KnowledgeDocumentResponse])
async def web_documents(
    actor: Annotated[ActorContext, Depends(get_pilot_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[KnowledgeDocumentResponse]:
    _require_admin(actor)
    statement = select(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc()).limit(100)
    if IdentityRole.SYSTEM_ADMIN not in actor.roles:
        permitted = [(KnowledgeScope.TENANT.value, actor.tenant_id)] + [
            (scope.scope.value, scope.scope_id) for scope in actor.knowledge_scopes
        ]
        statement = statement.where(
            or_(
                *(
                    (KnowledgeDocument.scope == scope)
                    & (KnowledgeDocument.scope_id == scope_id)
                    for scope, scope_id in permitted
                )
            )
        )
    documents = (await session.scalars(statement)).all()
    return [document_response(item, await latest_job(session, item.id)) for item in documents]


@router.post(
    "/internal/api/documents",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def web_upload_document(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File()],
    actor: Annotated[ActorContext, Depends(get_pilot_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    dispatch: Annotated[DocumentDispatcher, Depends(get_document_dispatcher)],
    _: Annotated[None, Depends(require_csrf)],
    scope: Annotated[KnowledgeScope, Form()] = KnowledgeScope.TENANT,
    scope_id: Annotated[str | None, Form(alias="scopeId", max_length=64)] = None,
    document_key: Annotated[str | None, Form(alias="documentKey", max_length=128)] = None,
    title: Annotated[str | None, Form(max_length=255)] = None,
) -> KnowledgeDocumentResponse:
    return await upload_document(
        request,
        response,
        file,
        actor,
        access,
        session,
        settings,
        storage,
        dispatch,
        scope,
        scope_id,
        document_key,
        title,
    )


@router.post("/internal/api/documents/{document_id}/lifecycle")
async def web_lifecycle(
    request: Request,
    document_id: UUID,
    payload: LifecycleWebRequest,
    actor: Annotated[ActorContext, Depends(get_pilot_actor)],
    corpus: Annotated[KnowledgeCorpus, Depends(get_web_knowledge_corpus)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    _require_admin(actor)
    try:
        view = await corpus.transition(
            document_id,
            payload.action,
            actor,
            reason=payload.reason,
            expected_revision=payload.expected_revision,
            trace_id=request_trace_id(request),
        )
    except CorpusError as exc:
        status_code = 404 if exc.code == "DOCUMENT_NOT_FOUND" else 409
        raise _error(status_code, exc.code, exc.message) from exc
    return view.to_dict()


@router.get("/internal/api/pilot")
async def web_pilot_status(
    _: Annotated[ActorContext, Depends(get_web_actor)],
    gate: Annotated[PilotAccessGate, Depends(get_pilot_access_gate)],
) -> dict[str, object]:
    return (await gate.state()).to_dict()


@router.post("/internal/api/pilot")
async def web_manage_pilot(
    request: Request,
    payload: PilotAccessWebRequest,
    actor: Annotated[ActorContext, Depends(get_web_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    gate: Annotated[PilotAccessGate, Depends(get_pilot_access_gate)],
    _: Annotated[None, Depends(require_csrf)],
) -> dict[str, object]:
    try:
        await access.authorize(
            actor,
            IdentityAction.MANAGE_PILOT,
            IdentityResource("pilot", "internal-web", KnowledgeScope.GLOBAL, "GLOBAL"),
            trace_id=request_trace_id(request),
        )
    except IdentityDenied as exc:
        raise _error(403, exc.code, "仅系统管理员可管理试点") from exc
    state = await gate.record(
        enabled=payload.enabled,
        approved_by=actor.subject_id,
        pilot_scope=payload.pilot_scope,
        capacity=payload.capacity,
        monitoring_owner=payload.monitoring_owner,
        rollback_owner=payload.rollback_owner,
        reason=payload.reason,
    )
    return state.to_dict()


def _require_admin(actor: ActorContext) -> None:
    if not actor.roles & {IdentityRole.KNOWLEDGE_ADMIN, IdentityRole.SYSTEM_ADMIN}:
        raise _error(403, "KNOWLEDGE_ADMIN_REQUIRED", "当前身份没有知识治理权限")


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
