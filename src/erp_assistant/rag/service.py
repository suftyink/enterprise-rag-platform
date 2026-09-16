import asyncio
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.infrastructure.object_storage import ObjectStorage
from erp_assistant.models.knowledge import (
    KnowledgeCorpusHead,
    KnowledgeDocument,
    KnowledgeIndexJob,
)
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.knowledge_queries import latest_job
from erp_assistant.rag.types import (
    DataClassification,
    DocumentStatus,
    IndexJobStatus,
    KnowledgeScope,
)
from erp_assistant.workers.dispatcher import DocumentDispatcher


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DocumentCreateResult:
    document: KnowledgeDocument
    job: KnowledgeIndexJob | None
    duplicate: bool


def scope_id_for(
    scope: KnowledgeScope,
    tenant_id: str,
    requested_scope_id: str | None = None,
) -> str:
    if scope is KnowledgeScope.GLOBAL:
        return "GLOBAL"
    if scope is KnowledgeScope.SHARED:
        value = unicodedata.normalize("NFKC", requested_scope_id or "").strip()
        if not re.fullmatch(r"[\w.-]{1,64}", value, flags=re.UNICODE):
            raise RagError("INVALID_SCOPE_ID", "共享知识范围编号无效")
        return value
    return tenant_id


async def create_document(
    session: AsyncSession,
    storage: ObjectStorage,
    dispatch: DocumentDispatcher,
    *,
    content: bytes,
    page_count: int | None,
    tenant_id: str,
    operator_id: str,
    scope: KnowledgeScope,
    scope_id: str | None = None,
    document_key: str,
    title: str,
    original_filename: str,
    media_type: str,
    data_classification: DataClassification,
    trace_id: UUID,
) -> DocumentCreateResult:
    if data_classification is DataClassification.UNCLASSIFIED:
        raise RagError("DOCUMENT_CLASSIFICATION_REQUIRED", "必须先完成可信文档分类")
    owner = scope_id_for(scope, tenant_id, scope_id)
    digest = sha256(content).hexdigest()
    duplicate = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.scope == scope.value,
            KnowledgeDocument.scope_id == owner,
            KnowledgeDocument.document_key == document_key,
            KnowledgeDocument.sha256 == digest,
        )
    )
    if duplicate is not None:
        if duplicate.data_classification != data_classification.value:
            raise RagError(
                "DOCUMENT_CLASSIFICATION_CHANGED",
                "文档分类策略已变化，必须先完成受审计的重新分类和索引重建",
            )
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
    extension = {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "text/markdown": ".md",
        "text/html": ".html",
    }[media_type]
    object_key = (
        f"knowledge/{scope.value.lower()}/{owner}/{document_key}/"
        f"v{version}/{document_id}/original{extension}"
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
        data_classification=data_classification.value,
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
    corpus_head = await session.scalar(
        select(KnowledgeCorpusHead).where(
            KnowledgeCorpusHead.scope == scope.value,
            KnowledgeCorpusHead.scope_id == owner,
            KnowledgeCorpusHead.document_key == document_key,
        )
    )
    if corpus_head is None:
        session.add(
            KnowledgeCorpusHead(
                scope=scope.value,
                scope_id=owner,
                document_key=document_key,
            )
        )
    await session.commit()

    try:
        await asyncio.to_thread(storage.put_bytes, object_key, content, media_type)
        document.status = DocumentStatus.QUEUED.value
        await session.commit()
        job.celery_task_id = dispatch(document_id, trace_id)
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


async def retry_document(
    session: AsyncSession,
    dispatch: DocumentDispatcher,
    document: KnowledgeDocument,
    *,
    trace_id: UUID,
) -> KnowledgeIndexJob:
    if document.status != DocumentStatus.FAILED.value:
        raise RagError("DOCUMENT_NOT_RETRYABLE", "只有 FAILED 文档可以重试")
    previous_job = await latest_job(session, document.id)
    document.status = DocumentStatus.QUEUED.value
    document.error_code = None
    document.error_detail = None
    job = KnowledgeIndexJob(
        document_id=document.id,
        status=IndexJobStatus.QUEUED.value,
        phase=DocumentStatus.QUEUED.value,
        cleanup_required=bool(previous_job and previous_job.cleanup_required),
        cleanup_artifacts=(
            list(previous_job.cleanup_artifacts) if previous_job is not None else []
        ),
        warnings=list(previous_job.warnings) if previous_job is not None else [],
    )
    session.add(job)
    await session.commit()
    try:
        job.celery_task_id = dispatch(document.id, trace_id)
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
