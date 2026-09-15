import logging
import re
import unicodedata
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.api.dependencies import get_knowledge_retriever
from erp_assistant.api.knowledge_schemas import (
    KnowledgeDocumentResponse,
    KnowledgeRetryRequest,
    KnowledgeSearchPipeline,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
)
from erp_assistant.core.config import Settings, get_settings
from erp_assistant.infrastructure.database import get_session
from erp_assistant.infrastructure.object_storage import ObjectStorage, get_object_storage
from erp_assistant.models.knowledge import KnowledgeDocument, KnowledgeIndexJob
from erp_assistant.rag.document_parsers import (
    validate_media_type,
)
from erp_assistant.rag.errors import (
    DocumentValidationError,
    PdfValidationError,
    RagDependencyError,
    RagError,
)
from erp_assistant.rag.knowledge_eligibility import is_retrieval_eligible
from erp_assistant.rag.knowledge_queries import latest_job
from erp_assistant.rag.pdf_parser import PypdfParser
from erp_assistant.rag.retriever import ActorContext, KnowledgeRetriever, RetrievalRequest
from erp_assistant.rag.service import (
    accessible_document,
    create_document,
    retry_document,
)
from erp_assistant.rag.types import (
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


def _document_key(value: str | None, filename: str) -> str:
    source = value or Path(filename).stem
    normalized = unicodedata.normalize("NFKC", source).strip().lower()
    normalized = re.sub(r"[^\w\-.\u3400-\u9fff]+", "-", normalized, flags=re.UNICODE)
    normalized = normalized.strip("-._")
    if not normalized:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "INVALID_DOCUMENT_KEY", "文档编号无效")
    return normalized[:128]


async def _read_document(
    file: UploadFile, settings: Settings
) -> tuple[bytes, int | None, str, str]:
    filename = Path(file.filename or "").name
    content = await file.read(settings.knowledge_max_file_size + 1)
    if len(content) > settings.knowledge_max_file_size:
        raise _error(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "FILE_TOO_LARGE",
            "文档超过允许的大小限制",
        )
    try:
        declared = validate_media_type(file.content_type or "", content)
    except DocumentValidationError as exc:
        status_code = (
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            if exc.code == "UNSUPPORTED_MEDIA_TYPE"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise _error(status_code, exc.code, exc.message) from exc
    page_count: int | None = None
    if declared == "application/pdf":
        try:
            page_count = PypdfParser().inspect(content, settings.knowledge_max_pages)
        except PdfValidationError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message) from exc
    return content, page_count, filename, declared


def _response(
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
    response: Response,
    file: Annotated[UploadFile, File()],
    tenant_id: Annotated[str, Form(alias="tenantId", min_length=1, max_length=64)],
    operator_id: Annotated[str, Form(alias="operatorId", min_length=1, max_length=64)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    dispatch: Annotated[DocumentDispatcher, Depends(get_document_dispatcher)],
    scope: Annotated[KnowledgeScope, Form()] = KnowledgeScope.TENANT,
    document_key: Annotated[str | None, Form(alias="documentKey", max_length=128)] = None,
    title: Annotated[str | None, Form(max_length=255)] = None,
) -> KnowledgeDocumentResponse:
    _require_rag(settings)
    if scope is KnowledgeScope.GLOBAL and (
        not settings.allow_global_knowledge_upload
        or operator_id not in settings.global_knowledge_operators
    ):
        raise _error(status.HTTP_403_FORBIDDEN, "GLOBAL_UPLOAD_FORBIDDEN", "无权上传全局知识")
    content, page_count, filename, media_type = await _read_document(file, settings)
    key = _document_key(document_key, filename)
    try:
        result = await create_document(
            session,
            storage,
            dispatch,
            content=content,
            page_count=page_count,
            tenant_id=tenant_id,
            operator_id=operator_id,
            scope=scope,
            document_key=key,
            title=(title or Path(filename).stem)[:255],
            original_filename=filename,
            media_type=media_type,
        )
    except RagDependencyError as exc:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message) from exc
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return _response(result.document, result.job, duplicate=result.duplicate)


@router.get("/knowledge/documents/{document_id}", response_model=KnowledgeDocumentResponse)
async def get_document_status(
    document_id: UUID,
    tenant_id: Annotated[str, Query(alias="tenantId", min_length=1, max_length=64)],
    operator_id: Annotated[str, Query(alias="operatorId", min_length=1, max_length=64)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> KnowledgeDocumentResponse:
    del operator_id
    document = await accessible_document(session, document_id, tenant_id)
    if document is None:
        raise _error(status.HTTP_404_NOT_FOUND, "DOCUMENT_NOT_FOUND", "文档不存在")
    return _response(document, await latest_job(session, document.id))


@router.post(
    "/knowledge/documents/{document_id}/retry",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_failed_document(
    document_id: UUID,
    payload: KnowledgeRetryRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    dispatch: Annotated[DocumentDispatcher, Depends(get_document_dispatcher)],
) -> KnowledgeDocumentResponse:
    _require_rag(settings)
    document = await accessible_document(session, document_id, payload.tenant_id)
    if document is None:
        raise _error(status.HTTP_404_NOT_FOUND, "DOCUMENT_NOT_FOUND", "文档不存在")
    if document.status == DocumentStatus.OCR_REQUIRED.value:
        raise _error(status.HTTP_409_CONFLICT, "OCR_NOT_AVAILABLE", "当前阶段尚未支持 OCR")
    try:
        job = await retry_document(session, dispatch, document)
    except RagDependencyError as exc:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message) from exc
    except RagError as exc:
        raise _error(status.HTTP_409_CONFLICT, exc.code, exc.message) from exc
    return _response(document, job)


@router.post("/knowledge/search", response_model=KnowledgeSearchResponse)
async def search_documents(
    payload: KnowledgeSearchRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    retriever: Annotated[KnowledgeRetriever, Depends(get_knowledge_retriever)],
) -> KnowledgeSearchResponse:
    _require_rag(settings)
    try:
        outcome = await retriever.retrieve(
            RetrievalRequest(
                query=payload.query,
                actor=ActorContext(
                    subject_id=payload.operator_id,
                    tenant_id=payload.tenant_id,
                ),
                evidence_limit=payload.top_k,
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
