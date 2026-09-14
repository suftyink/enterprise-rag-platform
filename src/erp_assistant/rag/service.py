import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings, get_settings
from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.infrastructure.object_storage import ObjectStorage, get_object_storage
from erp_assistant.models.knowledge import KnowledgeChunk, KnowledgeDocument, KnowledgeIndexJob
from erp_assistant.orchestration.provider_factory import get_embedding_provider
from erp_assistant.orchestration.providers import EmbeddingProvider
from erp_assistant.rag.chunker import ChunkDraft, chunk_pages
from erp_assistant.rag.errors import OcrRequiredError, RagDependencyError, RagError
from erp_assistant.rag.pdf_parser import PypdfParser
from erp_assistant.rag.types import DocumentStatus, IndexJobStatus, KnowledgeScope
from erp_assistant.rag.vector_store import VectorStore, get_vector_store
from erp_assistant.workers.dispatcher import DocumentDispatcher


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DocumentCreateResult:
    document: KnowledgeDocument
    job: KnowledgeIndexJob | None
    duplicate: bool


@dataclass(frozen=True)
class ReindexResult:
    document_count: int
    chunk_count: int


def scope_id_for(scope: KnowledgeScope, tenant_id: str) -> str:
    return "GLOBAL" if scope is KnowledgeScope.GLOBAL else tenant_id


async def latest_job(
    session: AsyncSession, document_id: UUID
) -> KnowledgeIndexJob | None:
    return cast(
        KnowledgeIndexJob | None,
        await session.scalar(
            select(KnowledgeIndexJob)
            .where(KnowledgeIndexJob.document_id == document_id)
            .order_by(KnowledgeIndexJob.created_at.desc())
            .limit(1)
        ),
    )


async def create_document(
    session: AsyncSession,
    storage: ObjectStorage,
    dispatch: DocumentDispatcher,
    *,
    content: bytes,
    page_count: int,
    tenant_id: str,
    operator_id: str,
    scope: KnowledgeScope,
    document_key: str,
    title: str,
    original_filename: str,
    media_type: str,
) -> DocumentCreateResult:
    owner = scope_id_for(scope, tenant_id)
    digest = sha256(content).hexdigest()
    duplicate = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.scope == scope.value,
            KnowledgeDocument.scope_id == owner,
            KnowledgeDocument.sha256 == digest,
        )
    )
    if duplicate is not None:
        return DocumentCreateResult(
            document=duplicate,
            job=await latest_job(session, duplicate.id),
            duplicate=True,
        )

    latest_version = await session.scalar(
        select(func.max(KnowledgeDocument.version)).where(
            KnowledgeDocument.scope == scope.value,
            KnowledgeDocument.scope_id == owner,
            KnowledgeDocument.document_key == document_key,
        )
    )
    version = int(latest_version or 0) + 1
    document_id = uuid4()
    object_key = (
        f"knowledge/{scope.value.lower()}/{owner}/{document_key}/"
        f"v{version}/{document_id}/original.pdf"
    )
    document = KnowledgeDocument(
        id=document_id,
        scope=scope.value,
        scope_id=owner,
        tenant_id=tenant_id,
        document_key=document_key,
        version=version,
        title=title,
        original_filename=original_filename,
        media_type=media_type,
        size_bytes=len(content),
        sha256=digest,
        object_key=object_key,
        page_count=page_count,
        status=DocumentStatus.UPLOADING.value,
        active=False,
        created_by=operator_id,
    )
    job = KnowledgeIndexJob(
        document_id=document_id,
        status=IndexJobStatus.QUEUED.value,
        phase=DocumentStatus.QUEUED.value,
    )
    session.add_all([document, job])
    await session.commit()

    try:
        await asyncio.to_thread(storage.put_bytes, object_key, content, "application/pdf")
        document.status = DocumentStatus.QUEUED.value
        await session.commit()
        job.celery_task_id = dispatch(document_id)
        await session.commit()
    except Exception as exc:
        document.status = DocumentStatus.FAILED.value
        document.error_code = "UPLOAD_OR_DISPATCH_FAILED"
        document.error_detail = str(exc)[:2_000]
        job.status = IndexJobStatus.FAILED.value
        job.error_code = document.error_code
        job.error_detail = document.error_detail
        job.finished_at = utc_now()
        await session.commit()
        raise RagDependencyError(document.error_code, "文件保存或任务入队失败") from exc
    return DocumentCreateResult(document=document, job=job, duplicate=False)


async def accessible_document(
    session: AsyncSession, document_id: UUID, tenant_id: str
) -> KnowledgeDocument | None:
    return cast(
        KnowledgeDocument | None,
        await session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.id == document_id,
                or_(
                    KnowledgeDocument.scope == KnowledgeScope.GLOBAL.value,
                    (
                        (KnowledgeDocument.scope == KnowledgeScope.TENANT.value)
                        & (KnowledgeDocument.scope_id == tenant_id)
                    ),
                ),
            )
        ),
    )


async def retry_document(
    session: AsyncSession,
    dispatch: DocumentDispatcher,
    document: KnowledgeDocument,
) -> KnowledgeIndexJob:
    if document.status != DocumentStatus.FAILED.value:
        raise RagError("DOCUMENT_NOT_RETRYABLE", "只有 FAILED 文档可以重试")
    document.status = DocumentStatus.QUEUED.value
    document.error_code = None
    document.error_detail = None
    job = KnowledgeIndexJob(
        document_id=document.id,
        status=IndexJobStatus.QUEUED.value,
        phase=DocumentStatus.QUEUED.value,
    )
    session.add(job)
    await session.commit()
    try:
        job.celery_task_id = dispatch(document.id)
        await session.commit()
    except Exception as exc:
        document.status = DocumentStatus.FAILED.value
        document.error_code = "QUEUE_DISPATCH_FAILED"
        document.error_detail = str(exc)[:2_000]
        job.status = IndexJobStatus.FAILED.value
        job.error_code = document.error_code
        job.error_detail = document.error_detail
        job.finished_at = utc_now()
        await session.commit()
        raise RagDependencyError(document.error_code, "任务重新入队失败") from exc
    return job


async def process_document(
    document_id: UUID,
    *,
    settings: Settings | None = None,
    storage: ObjectStorage | None = None,
    vector_store: VectorStore | None = None,
    embedding: EmbeddingProvider | None = None,
) -> None:
    settings = settings or get_settings()
    storage = storage or get_object_storage()
    vector_store = vector_store or get_vector_store()
    embedding = embedding or get_embedding_provider()
    parser = PypdfParser()

    async with SessionFactory() as session:
        document = await session.get(KnowledgeDocument, document_id)
        if document is None or document.status != DocumentStatus.QUEUED.value:
            return
        job = await latest_job(session, document_id)
        if job is None:
            return
        job.status = IndexJobStatus.RUNNING.value
        job.phase = DocumentStatus.PARSING.value
        job.attempt += 1
        job.started_at = utc_now()
        document.status = DocumentStatus.PARSING.value
        await session.commit()

        try:
            content = await asyncio.to_thread(storage.get_bytes, document.object_key)
            parsed = await asyncio.to_thread(
                parser.parse, content, settings.knowledge_min_text_per_page
            )
            document.page_count = len(parsed.pages)
            document.parse_warnings = parsed.warnings
            parsed_key = document.object_key.removesuffix("original.pdf") + "parsed/pages.json"
            parsed_content = json.dumps(
                {
                    "documentId": str(document.id),
                    "version": document.version,
                    "warnings": parsed.warnings,
                    "pages": [
                        {"pageNumber": page.page_number, "text": page.text}
                        for page in parsed.pages
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            await asyncio.to_thread(
                storage.put_bytes,
                parsed_key,
                parsed_content,
                "application/json; charset=utf-8",
            )
            document.parsed_object_key = parsed_key
            document.status = DocumentStatus.CHUNKING.value
            job.phase = DocumentStatus.CHUNKING.value
            await session.commit()

            drafts = chunk_pages(
                document.id,
                parsed.pages,
                settings.knowledge_chunk_target,
                settings.knowledge_chunk_max,
                settings.knowledge_chunk_overlap,
            )
            if not drafts:
                raise OcrRequiredError("OCR_REQUIRED", "PDF 未生成有效文本 Chunk")
            await session.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
            )
            session.add_all(
                [
                    _chunk_model(document, draft, embedding.model)
                    for draft in drafts
                ]
            )
            document.status = DocumentStatus.INDEXING.value
            job.phase = DocumentStatus.INDEXING.value
            await session.commit()

            vectors = await embedding.embed([draft.content for draft in drafts])
            await asyncio.to_thread(
                vector_store.upsert_document,
                document.id,
                document.scope,
                document.scope_id,
                document.version,
                drafts,
                vectors,
            )
            previous_documents = list(
                (
                    await session.scalars(
                        select(KnowledgeDocument).where(
                            KnowledgeDocument.scope == document.scope,
                            KnowledgeDocument.scope_id == document.scope_id,
                            KnowledgeDocument.document_key == document.document_key,
                            KnowledgeDocument.active.is_(True),
                            KnowledgeDocument.id != document.id,
                        )
                    )
                ).all()
            )
            for previous in previous_documents:
                previous.active = False
                previous.status = DocumentStatus.SUPERSEDED.value
            document.active = True
            document.status = DocumentStatus.READY.value
            document.indexed_at = utc_now()
            document.error_code = None
            document.error_detail = None
            job.status = IndexJobStatus.SUCCEEDED.value
            job.phase = DocumentStatus.READY.value
            job.finished_at = utc_now()
            await session.commit()

            for previous in previous_documents:
                try:
                    await asyncio.to_thread(vector_store.delete_document, previous.id)
                except Exception:
                    pass
        except OcrRequiredError as exc:
            await session.rollback()
            await _mark_processing_failure(
                session,
                document_id,
                job.id,
                DocumentStatus.OCR_REQUIRED,
                exc.code,
                exc.message,
            )
        except Exception as exc:
            await session.rollback()
            code = exc.code if isinstance(exc, RagError) else exc.__class__.__name__
            message = exc.message if isinstance(exc, RagError) else str(exc)
            await _mark_processing_failure(
                session,
                document_id,
                job.id,
                DocumentStatus.FAILED,
                code,
                message,
            )


def _chunk_model(
    document: KnowledgeDocument, draft: ChunkDraft, embedding_model: str
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=draft.id,
        document_id=document.id,
        scope=document.scope,
        scope_id=document.scope_id,
        version=document.version,
        chunk_index=draft.index,
        page_start=draft.page_start,
        page_end=draft.page_end,
        section_title=draft.section_title,
        content=draft.content,
        content_hash=draft.content_hash,
        char_count=len(draft.content),
        embedding_model=embedding_model,
    )


async def rebuild_active_index(
    session: AsyncSession,
    vector_store: VectorStore,
    embedding: EmbeddingProvider,
) -> ReindexResult:
    rows = (
        await session.execute(
            select(KnowledgeDocument, KnowledgeChunk)
            .join(KnowledgeChunk, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.active.is_(True),
                KnowledgeDocument.status == DocumentStatus.READY.value,
            )
            .order_by(KnowledgeDocument.id, KnowledgeChunk.chunk_index)
        )
    ).all()
    grouped: dict[UUID, tuple[KnowledgeDocument, list[KnowledgeChunk]]] = {}
    for document, chunk in rows:
        if document.id not in grouped:
            grouped[document.id] = (document, [])
        grouped[document.id][1].append(chunk)

    chunk_count = 0
    for document, chunks in grouped.values():
        drafts = [
            ChunkDraft(
                id=chunk.id,
                index=chunk.chunk_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section_title=chunk.section_title,
                content=chunk.content,
                content_hash=chunk.content_hash,
            )
            for chunk in chunks
        ]
        vectors = await embedding.embed([draft.content for draft in drafts])
        await asyncio.to_thread(
            vector_store.upsert_document,
            document.id,
            document.scope,
            document.scope_id,
            document.version,
            drafts,
            vectors,
        )
        chunk_count += len(drafts)
    return ReindexResult(document_count=len(grouped), chunk_count=chunk_count)


async def _mark_processing_failure(
    session: AsyncSession,
    document_id: UUID,
    job_id: UUID,
    status: DocumentStatus,
    code: str,
    detail: str,
) -> None:
    document = await session.get(KnowledgeDocument, document_id)
    job = await session.get(KnowledgeIndexJob, job_id)
    if document is not None:
        document.status = status.value
        document.active = False
        document.error_code = code[:64]
        document.error_detail = detail[:2_000]
    if job is not None:
        job.status = IndexJobStatus.FAILED.value
        job.error_code = code[:64]
        job.error_detail = detail[:2_000]
        job.finished_at = utc_now()
    await session.commit()

