import logging
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
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.api.dependencies import (
    get_current_actor,
    get_identity_access,
    get_knowledge_retriever,
    request_trace_id,
)
from erp_assistant.api.knowledge_access import authorize_or_not_found, knowledge_resource
from erp_assistant.api.knowledge_schemas import (
    KnowledgeDocumentResponse,
    KnowledgeRetryRequest,
    KnowledgeSearchPipeline,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
)
from erp_assistant.api.knowledge_upload import (
    KnowledgeUploadCommand,
    accept_document_upload,
)
from erp_assistant.core.config import Settings, get_settings
from erp_assistant.identity import (
    ActorContext,
    IdentityAccess,
    IdentityAction,
    IdentityResource,
)
from erp_assistant.infrastructure.database import get_session
from erp_assistant.infrastructure.object_storage import ObjectStorage, get_object_storage
from erp_assistant.models.knowledge import KnowledgeDocument, KnowledgeIndexJob
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.knowledge_eligibility import is_retrieval_eligible
from erp_assistant.rag.knowledge_queries import latest_job
from erp_assistant.rag.profile_bound_retriever import ProfileBoundRetriever
from erp_assistant.rag.retriever import RetrievalRequest
from erp_assistant.rag.service import retry_document
from erp_assistant.rag.types import (
    DataClassification,
    DocumentStatus,
    IndexJobStatus,
    KnowledgeScope,
    PublicationStatus,
    RetentionStatus,
)
from erp_assistant.workers.dispatcher import DocumentDispatcher, get_document_dispatcher

router = APIRouter()
logger = logging.getLogger(__name__)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _require_rag(settings: Settings) -> None:
    if not settings.rag_enabled:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "RAG_DISABLED", "RAG 功能未启用")


def document_response(
    document: KnowledgeDocument,
    job: KnowledgeIndexJob | None,
    *,
    duplicate: bool = False,
) -> KnowledgeDocumentResponse:
    return KnowledgeDocumentResponse(
        documentId=document.id,
        documentKey=document.document_key,
        version=document.version,
        scope=KnowledgeScope(document.scope),
        status=DocumentStatus(document.status),
        publicationStatus=PublicationStatus(document.publication_status),
        retentionStatus=RetentionStatus(document.retention_status),
        governanceRevision=document.governance_revision,
        retrievalEligible=is_retrieval_eligible(document),
        duplicate=duplicate,
        title=document.title,
        mediaType=document.media_type,
        dataClassification=DataClassification(document.data_classification),
        pageCount=document.page_count,
        blockCount=document.block_count,
        phase=job.phase if job else None,
        jobStatus=IndexJobStatus(job.status) if job else None,
        attempt=job.attempt if job else None,
        chunkCount=job.chunk_count if job else None,
        warnings=job.warnings if job else document.parse_warnings,
        pipelineRevision=job.pipeline_revision if job else None,
        parserVersion=job.parser_version if job else None,
        chunkingVersion=job.chunking_version if job else None,
        qualitySignals=job.quality_signals if job else {},
        cleanupRequired=job.cleanup_required if job else False,
        errorCode=(job.error_code if job and job.error_code else document.error_code),
        errorDetail=(job.error_detail if job and job.error_detail else document.error_detail),
    )


@router.post(
    "/knowledge/documents",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File()],
    actor: Annotated[ActorContext, Depends(get_current_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    dispatch: Annotated[DocumentDispatcher, Depends(get_document_dispatcher)],
    scope: Annotated[KnowledgeScope, Form()] = KnowledgeScope.TENANT,
    scope_id: Annotated[str | None, Form(alias="scopeId", max_length=64)] = None,
    document_key: Annotated[str | None, Form(alias="documentKey", max_length=128)] = None,
    title: Annotated[str | None, Form(max_length=255)] = None,
) -> KnowledgeDocumentResponse:
    result = await accept_document_upload(
        request=request,
        file=file,
        actor=actor,
        access=access,
        session=session,
        settings=settings,
        storage=storage,
        dispatch=dispatch,
        command=KnowledgeUploadCommand(scope, scope_id, document_key, title),
    )
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return document_response(result.document, result.job, duplicate=result.duplicate)


@router.get("/knowledge/documents/{document_id}", response_model=KnowledgeDocumentResponse)
async def get_document_status(
    request: Request,
    document_id: UUID,
    actor: Annotated[ActorContext, Depends(get_current_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> KnowledgeDocumentResponse:
    document = await session.get(KnowledgeDocument, document_id)
    if document is None:
        raise _error(status.HTTP_404_NOT_FOUND, "RESOURCE_NOT_FOUND", "资源不存在")
    await authorize_or_not_found(
        access,
        actor,
        IdentityAction.VIEW_DOCUMENT,
        knowledge_resource(document),
        request,
    )
    return document_response(document, await latest_job(session, document.id))


@router.post(
    "/knowledge/documents/{document_id}/retry",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_failed_document(
    request: Request,
    document_id: UUID,
    payload: KnowledgeRetryRequest,
    actor: Annotated[ActorContext, Depends(get_current_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    dispatch: Annotated[DocumentDispatcher, Depends(get_document_dispatcher)],
) -> KnowledgeDocumentResponse:
    _require_rag(settings)
    del payload
    document = await session.get(KnowledgeDocument, document_id)
    if document is None:
        raise _error(status.HTTP_404_NOT_FOUND, "RESOURCE_NOT_FOUND", "资源不存在")
    await authorize_or_not_found(
        access,
        actor,
        IdentityAction.RETRY,
        knowledge_resource(document),
        request,
    )
    if document.status == DocumentStatus.OCR_REQUIRED.value:
        raise _error(status.HTTP_409_CONFLICT, "OCR_NOT_AVAILABLE", "当前阶段尚未支持 OCR")
    try:
        job = await retry_document(
            session,
            dispatch,
            document,
            trace_id=request_trace_id(request),
        )
    except RagDependencyError as exc:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message) from exc
    except RagError as exc:
        raise _error(status.HTTP_409_CONFLICT, exc.code, exc.message) from exc
    return document_response(document, job)


@router.post("/knowledge/search", response_model=KnowledgeSearchResponse)
async def search_documents(
    request: Request,
    payload: KnowledgeSearchRequest,
    actor: Annotated[ActorContext, Depends(get_current_actor)],
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    settings: Annotated[Settings, Depends(get_settings)],
    retriever: Annotated[ProfileBoundRetriever, Depends(get_knowledge_retriever)],
) -> KnowledgeSearchResponse:
    _require_rag(settings)
    await authorize_or_not_found(
        access,
        actor,
        IdentityAction.SEARCH,
        IdentityResource(
            "knowledge_scope", actor.tenant_id, KnowledgeScope.TENANT, actor.tenant_id
        ),
        request,
    )
    try:
        outcome = await retriever.retrieve(
            RetrievalRequest(
                query=payload.query,
                actor=actor,
                evidence_limit=payload.top_k,
                trace_id=request_trace_id(request),
            )
        )
    except RagDependencyError as exc:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message) from exc
    except RagError as exc:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message) from exc
    logger.info(
        "knowledge_search trace_id=%s embedding=%s reranker=%s results=%d elapsed_ms=%.2f",
        outcome.run.trace_id,
        outcome.run.embedding_model,
        outcome.run.rerank_model,
        len(outcome.evidence),
        outcome.run.elapsed_ms,
    )
    return KnowledgeSearchResponse(
        traceId=outcome.run.trace_id,
        query=payload.query,
        decision=outcome.decision,
        elapsedMs=outcome.run.elapsed_ms,
        pipeline=KnowledgeSearchPipeline(
            retrieval=outcome.run.retrieval,
            embeddingModel=outcome.run.embedding_model,
            rerankModel=outcome.run.rerank_model,
            threshold=outcome.run.threshold,
            configFingerprint=outcome.run.config_fingerprint,
            embeddingDeployment=outcome.run.embedding_deployment,
            embeddingDimension=outcome.run.embedding_dimension,
            rerankDeployment=outcome.run.rerank_deployment,
            providerBoundary=outcome.run.provider_boundary,
            productionReady=outcome.run.production_ready,
            requestCount=outcome.run.request_count,
            inputTokens=outcome.run.input_tokens,
            retrievalConfig=outcome.run.retrieval_config,
        ),
        results=[
            KnowledgeSearchResult(
                chunkId=evidence.chunk_id,
                documentId=evidence.document_id,
                documentKey=evidence.document_key,
                title=evidence.title,
                scope=evidence.scope,
                version=evidence.document_version,
                pageStart=evidence.page_start,
                pageEnd=evidence.page_end,
                sectionTitle=evidence.section_title,
                headingPath=list(evidence.heading_path),
                anchor=evidence.anchor,
                blockKind=evidence.block_kind,
                sourceLocator=evidence.source_locator,
                content=evidence.content,
                score=evidence.score,
                retrievalRank=evidence.retrieval_rank,
            )
            for evidence in outcome.evidence
        ],
    )
