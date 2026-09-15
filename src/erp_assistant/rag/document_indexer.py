import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.core.config import Settings
from erp_assistant.infrastructure.object_storage import ObjectStorage
from erp_assistant.models.knowledge import KnowledgeChunk, KnowledgeDocument, KnowledgeIndexJob
from erp_assistant.orchestration.providers import EmbeddingProvider, ProviderBoundary
from erp_assistant.rag.chunker import (
    CHUNK_STRATEGY_VERSION,
    ChunkDraft,
    chunk_document,
)
from erp_assistant.rag.document_parsers import DocumentParserRouter
from erp_assistant.rag.errors import DocumentValidationError, OcrRequiredError, RagDependencyError
from erp_assistant.rag.knowledge_queries import latest_job
from erp_assistant.rag.parsed_document import ParserLimits
from erp_assistant.rag.pdf_parser import PdfParser
from erp_assistant.rag.types import (
    DataClassification,
    DocumentStatus,
    IndexJobStatus,
    PublicationStatus,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class VectorWriter(Protocol):
    identity: str

    def upsert_document(
        self,
        document_id: UUID,
        scope: str,
        scope_id: str,
        version: int,
        chunks: list[ChunkDraft],
        vectors: list[list[float]],
    ) -> None: ...

    def delete_document(self, document_id: UUID) -> None: ...


class IndexDecision(StrEnum):
    INDEXED_FOR_CURRENT_POLICY = "INDEXED_FOR_CURRENT_POLICY"
    OCR_REQUIRED = "OCR_REQUIRED"
    SKIPPED = "SKIPPED"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


@dataclass(frozen=True)
class IndexPipelineIdentity:
    parser: str
    chunking: str
    embedding: str
    vector_index: str
    revision: str


@dataclass(frozen=True)
class DocumentIndexOutcome:
    document_id: UUID
    decision: IndexDecision
    status: DocumentStatus
    chunk_count: int
    warnings: tuple[str, ...]
    attempt: int
    pipeline: IndexPipelineIdentity
    error_code: str | None = None
    cleanup_required: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "documentId": str(self.document_id),
            "decision": self.decision.value,
            "status": self.status.value,
            "chunkCount": self.chunk_count,
            "warnings": list(self.warnings),
            "attempt": self.attempt,
            "pipelineRevision": self.pipeline.revision,
            "errorCode": self.error_code,
            "cleanupRequired": self.cleanup_required,
        }


class DocumentIndexRetryableError(RagDependencyError):
    def __init__(self, code: str, message: str, attempt: int) -> None:
        super().__init__(code, message)
        self.attempt = attempt


class _IndexLeaseLost(Exception):
    pass


class DocumentIndexer:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        parser: PdfParser | DocumentParserRouter,
        embedding: EmbeddingProvider,
        vector_writer: VectorWriter,
        settings: Settings,
    ) -> None:
        self._sessions = session_factory
        self._storage = storage
        self._parser = (
            parser if isinstance(parser, DocumentParserRouter) else DocumentParserRouter(parser)
        )
        self._embedding = embedding
        self._vectors = vector_writer
        self._settings = settings
        self.pipeline = self._pipeline_identity()

    async def index(self, document_id: UUID) -> DocumentIndexOutcome:
        async with self._sessions() as session:
            try:
                document = await session.get(KnowledgeDocument, document_id)
            except SQLAlchemyError as exc:
                raise DocumentIndexRetryableError(
                    "DATABASE_READ_FAILED", "读取索引状态失败，可安全重试", 0
                ) from exc
            if document is None:
                raise LookupError(f"Knowledge document {document_id} does not exist")
            try:
                job = await latest_job(session, document_id)
            except SQLAlchemyError as exc:
                raise DocumentIndexRetryableError(
                    "DATABASE_READ_FAILED", "读取索引任务失败，可安全重试", 0
                ) from exc
            if job is None:
                raise LookupError(f"Knowledge document {document_id} has no index job")

            if job.cleanup_required:
                await self._cleanup_pending(job)
            if self._is_current(document, job):
                return self._outcome(
                    document,
                    job,
                    IndexDecision.SKIPPED,
                    DocumentStatus(document.status),
                )
            was_active = document.active
            source_hash = document.sha256
            job_id = job.id
            previous_attempt = job.attempt
            attempt = previous_attempt + 1
            claim_token = str(uuid4())
            claimed_at = utc_now()
            lease_until = claimed_at + timedelta(
                seconds=self._settings.knowledge_index_lease_seconds
            )
            try:
                claim = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(KnowledgeIndexJob)
                        .where(
                            KnowledgeIndexJob.id == job_id,
                            KnowledgeIndexJob.attempt == previous_attempt,
                            or_(
                                KnowledgeIndexJob.status != IndexJobStatus.RUNNING.value,
                                KnowledgeIndexJob.lease_expires_at.is_(None),
                                KnowledgeIndexJob.lease_expires_at < claimed_at,
                            ),
                        )
                        .values(
                            status=IndexJobStatus.RUNNING.value,
                            phase=DocumentStatus.PARSING.value,
                            attempt=attempt,
                            started_at=claimed_at,
                            finished_at=None,
                            pipeline_revision=self.pipeline.revision,
                            parser_version=self._parser.parser_version(document.media_type),
                            chunking_version=CHUNK_STRATEGY_VERSION,
                            quality_signals={},
                            embedding_deployment=str(
                                getattr(self._embedding, "deployment", "legacy")
                            ),
                            embedding_dimension=self._embedding.dimension,
                            retrieval_profile=self.pipeline.revision,
                            warnings=[],
                            chunk_count=0,
                            source_hash=source_hash,
                            claim_token=claim_token,
                            lease_expires_at=lease_until,
                            error_code=None,
                            error_detail=None,
                        )
                        .execution_options(synchronize_session=False)
                    ),
                )
            except SQLAlchemyError as exc:
                raise DocumentIndexRetryableError(
                    "DATABASE_WRITE_FAILED", "认领索引任务失败，可安全重试", previous_attempt
                ) from exc
            if claim.rowcount != 1:
                await session.rollback()
                try:
                    await session.refresh(document)
                    await session.refresh(job)
                except SQLAlchemyError as exc:
                    raise DocumentIndexRetryableError(
                        "DATABASE_READ_FAILED", "读取任务认领结果失败，可安全重试", 0
                    ) from exc
                return self._outcome(
                    document,
                    job,
                    IndexDecision.SKIPPED,
                    DocumentStatus(document.status),
                )
            try:
                await session.refresh(job)
            except SQLAlchemyError as exc:
                raise DocumentIndexRetryableError(
                    "DATABASE_READ_FAILED", "读取已认领任务失败，可安全重试", previous_attempt
                ) from exc
            if not was_active:
                document.status = DocumentStatus.PARSING.value
            phase = DocumentStatus.PARSING
            failure_code = "DATABASE_WRITE_FAILED"
            stage_document_id = uuid5(document_id, claim_token)
            vector_dirty = False
            try:
                await session.commit()

                if (
                    DataClassification(document.data_classification)
                    is DataClassification.RESTRICTED
                    and ProviderBoundary(
                        getattr(self._embedding, "boundary", ProviderBoundary.INTERNAL)
                    )
                    is not ProviderBoundary.INTERNAL
                ):
                    raise DocumentValidationError(
                        "RESTRICTED_ROUTE_FORBIDDEN",
                        "受限文档只能发送到批准的内网或本地 Embedding",
                    )

                failure_code = "BLOB_READ_FAILED"
                content = await asyncio.to_thread(self._storage.get_bytes, document.object_key)
                failure_code = "PARSER_FAILED"
                parsed = await asyncio.to_thread(
                    self._parser.parse,
                    document.media_type,
                    content,
                    self._parser_limits(),
                )
                parsed_key = document.object_key.rsplit("/", 1)[0] + "/parsed/document.json"
                parsed_content = json.dumps(
                    {
                        "documentId": str(document.id),
                        "version": document.version,
                        "mediaType": document.media_type,
                        "parserVersion": parsed.parser_version,
                        "chunkingVersion": CHUNK_STRATEGY_VERSION,
                        "warnings": list(parsed.warnings),
                        "qualitySignals": parsed.quality_signals,
                        "blocks": [block.to_dict() for block in parsed.blocks],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                failure_code = "PARSED_BLOB_WRITE_FAILED"
                await asyncio.to_thread(
                    self._storage.put_bytes,
                    parsed_key,
                    parsed_content,
                    "application/json; charset=utf-8",
                )
                document.parsed_object_key = parsed_key
                document.page_count = parsed.page_count
                document.block_count = len(parsed.blocks)
                document.parse_warnings = list(parsed.warnings)
                if not was_active:
                    document.status = DocumentStatus.CHUNKING.value
                job.phase = DocumentStatus.CHUNKING.value
                phase = DocumentStatus.CHUNKING
                failure_code = "DATABASE_WRITE_FAILED"
                await self._commit_claimed(session, job_id, attempt, claim_token)

                drafts = chunk_document(
                    document.id,
                    parsed,
                    self._settings.knowledge_chunk_target,
                    self._settings.knowledge_chunk_max,
                    self._settings.knowledge_chunk_overlap,
                )
                if not drafts:
                    raise DocumentValidationError("EMPTY_DOCUMENT", "文档未生成有效文本 Chunk")
                if not was_active:
                    await session.execute(
                        delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
                    )
                    session.add_all([self._chunk_model(document, draft) for draft in drafts])
                if not was_active:
                    document.status = DocumentStatus.INDEXING.value
                job.phase = DocumentStatus.INDEXING.value
                job.chunk_count = len(drafts)
                job.warnings = list(parsed.warnings)
                job.parser_version = parsed.parser_version
                job.chunking_version = CHUNK_STRATEGY_VERSION
                job.quality_signals = parsed.quality_signals
                phase = DocumentStatus.INDEXING
                await self._commit_claimed(session, job_id, attempt, claim_token)

                failure_code = "EMBEDDING_FAILED"
                vectors: list[list[float]] = []
                batch_size = max(1, self._settings.knowledge_embedding_batch_size)
                for start in range(0, len(drafts), batch_size):
                    batch = drafts[start : start + batch_size]
                    vectors.extend(await self._embedding.embed([draft.content for draft in batch]))
                await self._renew_claim(session, job_id, attempt, claim_token)
                failure_code = "VECTOR_WRITE_FAILED"
                vector_document_id = stage_document_id
                vector_dirty = True
                await asyncio.to_thread(
                    self._vectors.upsert_document,
                    vector_document_id,
                    document.scope,
                    document.scope_id,
                    document.version,
                    drafts,
                    vectors,
                )
                failure_code = "DATABASE_WRITE_FAILED"
                if was_active:
                    await session.execute(
                        delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
                    )
                    session.add_all([self._chunk_model(document, draft) for draft in drafts])
                cleanup_ids: list[UUID] = []
                if was_active:
                    cleanup_ids.append(document.vector_document_id or document.id)
                cleanup_ids = [
                    cleanup_id for cleanup_id in cleanup_ids if cleanup_id != vector_document_id
                ]
                document.active = was_active
                document.status = DocumentStatus.READY.value
                if not was_active:
                    document.publication_status = PublicationStatus.REVIEW_PENDING.value
                document.vector_document_id = vector_document_id
                document.index_artifacts_present = True
                document.indexed_at = utc_now()
                document.error_code = None
                document.error_detail = None
                job.status = IndexJobStatus.SUCCEEDED.value
                job.phase = DocumentStatus.READY.value
                job.finished_at = utc_now()
                job.cleanup_required = bool(cleanup_ids)
                job.cleanup_artifacts = [
                    {"kind": "vector_document", "document_id": str(cleanup_id)}
                    for cleanup_id in cleanup_ids
                ]
                await self._commit_claimed(session, job_id, attempt, claim_token, release=True)

                await self._cleanup_pending(job)
                return self._outcome(
                    document,
                    job,
                    IndexDecision.INDEXED_FOR_CURRENT_POLICY,
                    DocumentStatus.READY,
                )
            except OcrRequiredError as exc:
                await session.rollback()
                try:
                    persisted = await self._persist_terminal(
                        document_id,
                        job_id,
                        was_active,
                        attempt,
                        source_hash,
                        claim_token,
                        phase,
                        DocumentStatus.OCR_REQUIRED,
                        exc.code,
                        exc.message,
                    )
                except SQLAlchemyError as persistence_error:
                    raise DocumentIndexRetryableError(
                        "DATABASE_WRITE_FAILED", "保存 OCR 状态失败，可安全重试", attempt
                    ) from persistence_error
                if not persisted:
                    return await self._outcome_from_ids(
                        document_id, job_id, IndexDecision.SKIPPED, None
                    )
                return await self._outcome_from_ids(
                    document_id, job_id, IndexDecision.OCR_REQUIRED, exc.code
                )
            except DocumentValidationError as exc:
                await session.rollback()
                try:
                    persisted = await self._persist_terminal(
                        document_id,
                        job_id,
                        was_active,
                        attempt,
                        source_hash,
                        claim_token,
                        phase,
                        DocumentStatus.FAILED,
                        exc.code,
                        exc.message,
                    )
                except SQLAlchemyError as persistence_error:
                    raise DocumentIndexRetryableError(
                        "DATABASE_WRITE_FAILED", "保存解析失败状态失败，可安全重试", attempt
                    ) from persistence_error
                if not persisted:
                    return await self._outcome_from_ids(
                        document_id, job_id, IndexDecision.SKIPPED, None
                    )
                return await self._outcome_from_ids(
                    document_id, job_id, IndexDecision.PERMANENT_FAILURE, exc.code
                )
            except _IndexLeaseLost:
                await session.rollback()
                if vector_dirty:
                    try:
                        await asyncio.to_thread(self._vectors.delete_document, stage_document_id)
                    except Exception:
                        pass
                return await self._outcome_from_ids(
                    document_id, job_id, IndexDecision.SKIPPED, None
                )
            except DocumentIndexRetryableError:
                raise
            except Exception as exc:
                await session.rollback()
                artifacts = (
                    [
                        {
                            "kind": "vector_document",
                            "document_id": str(stage_document_id),
                        }
                    ]
                    if vector_dirty
                    else []
                )
                try:
                    persisted = await self._persist_retryable(
                        document_id,
                        job_id,
                        was_active,
                        attempt,
                        source_hash,
                        claim_token,
                        phase,
                        failure_code,
                        exc.__class__.__name__,
                        artifacts,
                    )
                except SQLAlchemyError as persistence_error:
                    raise DocumentIndexRetryableError(
                        "DATABASE_WRITE_FAILED",
                        "保存索引失败状态失败，可安全重试",
                        attempt,
                    ) from persistence_error
                if not persisted:
                    return await self._outcome_from_ids(
                        document_id, job_id, IndexDecision.SKIPPED, None
                    )
                raise DocumentIndexRetryableError(
                    failure_code,
                    "文档索引遇到临时故障，可安全重试",
                    attempt,
                ) from exc

    def _pipeline_identity(self) -> IndexPipelineIdentity:
        chunking = (
            f"target={self._settings.knowledge_chunk_target};"
            f"max={self._settings.knowledge_chunk_max};"
            f"overlap={self._settings.knowledge_chunk_overlap}"
        )
        values = {
            "parser": self._parser.identity,
            "chunking": f"{CHUNK_STRATEGY_VERSION};{chunking}",
            "embedding": f"{self._embedding.model}:{self._embedding.dimension}",
            "vector_index": self._vectors.identity,
        }
        revision = sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return IndexPipelineIdentity(revision=revision, **values)

    async def _renew_claim(
        self,
        session: AsyncSession,
        job_id: UUID,
        attempt: int,
        claim_token: str,
    ) -> None:
        await self._commit_claimed(session, job_id, attempt, claim_token)

    async def _commit_claimed(
        self,
        session: AsyncSession,
        job_id: UUID,
        attempt: int,
        claim_token: str,
        *,
        release: bool = False,
    ) -> None:
        lease_until = None
        if not release:
            lease_until = utc_now() + timedelta(
                seconds=self._settings.knowledge_index_lease_seconds
            )
        with session.no_autoflush:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(KnowledgeIndexJob)
                    .where(
                        KnowledgeIndexJob.id == job_id,
                        KnowledgeIndexJob.attempt == attempt,
                        KnowledgeIndexJob.claim_token == claim_token,
                    )
                    .values(
                        lease_expires_at=lease_until,
                        claim_token=None if release else claim_token,
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
        if result.rowcount != 1:
            await session.rollback()
            raise _IndexLeaseLost
        await session.commit()

    def _is_current(self, document: KnowledgeDocument, job: KnowledgeIndexJob) -> bool:
        return (
            job.status == IndexJobStatus.SUCCEEDED.value
            and job.source_hash == document.sha256
            and job.pipeline_revision == self.pipeline.revision
            and document.status in {DocumentStatus.READY.value, DocumentStatus.SUPERSEDED.value}
        )

    def _outcome(
        self,
        document: KnowledgeDocument,
        job: KnowledgeIndexJob,
        decision: IndexDecision,
        status: DocumentStatus,
        error_code: str | None = None,
    ) -> DocumentIndexOutcome:
        return DocumentIndexOutcome(
            document_id=document.id,
            decision=decision,
            status=status,
            chunk_count=job.chunk_count,
            warnings=tuple(job.warnings),
            attempt=job.attempt,
            pipeline=self.pipeline,
            error_code=error_code,
            cleanup_required=job.cleanup_required,
        )

    async def _outcome_from_ids(
        self,
        document_id: UUID,
        job_id: UUID,
        decision: IndexDecision,
        error_code: str | None,
    ) -> DocumentIndexOutcome:
        try:
            async with self._sessions() as session:
                document = await session.get_one(KnowledgeDocument, document_id)
                job = await session.get_one(KnowledgeIndexJob, job_id)
                return self._outcome(
                    document,
                    job,
                    decision,
                    DocumentStatus(document.status),
                    error_code,
                )
        except SQLAlchemyError as exc:
            raise DocumentIndexRetryableError(
                "DATABASE_READ_FAILED", "读取索引结果失败，可安全重试", 0
            ) from exc

    async def _persist_terminal(
        self,
        document_id: UUID,
        job_id: UUID,
        was_active: bool,
        attempt: int,
        source_hash: str,
        claim_token: str,
        phase: DocumentStatus,
        status: DocumentStatus,
        code: str,
        detail: str,
    ) -> bool:
        async with self._sessions() as session:
            document = await session.get_one(KnowledgeDocument, document_id)
            job = await session.scalar(
                select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id).with_for_update()
            )
            if job is None:
                return False
            if job.claim_token != claim_token and not (
                job.claim_token is None and job.attempt < attempt
            ):
                return False
            if not was_active:
                document.active = False
                document.status = status.value
                document.error_code = code[:64]
                document.error_detail = detail[:2_000]
            job.status = IndexJobStatus.FAILED.value
            job.phase = phase.value
            job.attempt = attempt
            job.pipeline_revision = self.pipeline.revision
            job.source_hash = source_hash
            job.claim_token = None
            job.lease_expires_at = None
            job.error_code = code[:64]
            job.error_detail = detail[:2_000]
            job.finished_at = utc_now()
            await session.commit()
            return True

    async def _persist_retryable(
        self,
        document_id: UUID,
        job_id: UUID,
        was_active: bool,
        attempt: int,
        source_hash: str,
        claim_token: str,
        phase: DocumentStatus,
        code: str,
        detail: str,
        artifacts: list[dict[str, str]],
    ) -> bool:
        async with self._sessions() as session:
            document = await session.get_one(KnowledgeDocument, document_id)
            job = await session.scalar(
                select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id).with_for_update()
            )
            if job is None:
                return False
            if job.claim_token != claim_token and not (
                job.claim_token is None and job.attempt < attempt
            ):
                return False
            if not was_active:
                document.active = False
                document.status = DocumentStatus.FAILED.value
                document.error_code = code[:64]
                document.error_detail = detail[:2_000]
            job.status = IndexJobStatus.FAILED.value
            job.phase = phase.value
            job.attempt = attempt
            job.pipeline_revision = self.pipeline.revision
            job.source_hash = source_hash
            job.claim_token = None
            job.lease_expires_at = None
            job.error_code = code[:64]
            job.error_detail = detail[:2_000]
            job.cleanup_required = bool(artifacts)
            job.cleanup_artifacts = artifacts
            job.finished_at = utc_now()
            await session.commit()
            return True

    async def _cleanup_pending(self, job: KnowledgeIndexJob) -> None:
        if not job.cleanup_required:
            return
        pending = list(job.cleanup_artifacts)
        failures: list[dict[str, str]] = []
        for artifact in pending:
            try:
                await asyncio.to_thread(
                    self._vectors.delete_document, UUID(artifact["document_id"])
                )
            except Exception:
                failures.append(artifact)
        job.cleanup_required = bool(failures)
        job.cleanup_artifacts = failures
        if failures and "VECTOR_CLEANUP_PENDING" not in job.warnings:
            job.warnings = [*job.warnings, "VECTOR_CLEANUP_PENDING"]
        if not failures:
            job.warnings = [
                warning for warning in job.warnings if warning != "VECTOR_CLEANUP_PENDING"
            ]
        try:
            async with self._sessions() as session:
                stored = await session.get_one(KnowledgeIndexJob, job.id)
                if stored.cleanup_artifacts == pending:
                    stored.cleanup_required = job.cleanup_required
                    stored.cleanup_artifacts = job.cleanup_artifacts
                    stored.warnings = job.warnings
                    await session.commit()
                else:
                    job.cleanup_required = stored.cleanup_required
                    job.cleanup_artifacts = stored.cleanup_artifacts
                    job.warnings = stored.warnings
        except SQLAlchemyError as exc:
            raise DocumentIndexRetryableError(
                "DATABASE_WRITE_FAILED", "保存清理状态失败，可安全重试", job.attempt
            ) from exc
        if failures:
            raise DocumentIndexRetryableError(
                "VECTOR_CLEANUP_FAILED", "向量清理暂时失败，可安全重试", job.attempt
            )

    def _chunk_model(self, document: KnowledgeDocument, draft: ChunkDraft) -> KnowledgeChunk:
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
            embedding_model=self._embedding.model,
            embedding_dimension=self._embedding.dimension,
            retrieval_profile=self.pipeline.revision,
            heading_path=list(draft.heading_path),
            anchor=draft.anchor,
            block_kind=draft.block_kind.value,
            source_locator=draft.source_locator,
        )

    def _parser_limits(self) -> ParserLimits:
        return ParserLimits(
            max_file_size=self._settings.knowledge_max_file_size,
            max_pages=self._settings.knowledge_max_pages,
            max_blocks=self._settings.knowledge_max_blocks,
            max_uncompressed_size=self._settings.knowledge_max_uncompressed_size,
            max_compression_ratio=self._settings.knowledge_max_compression_ratio,
            minimum_text_characters=self._settings.knowledge_min_text_characters,
            minimum_text_per_page=self._settings.knowledge_min_text_per_page,
        )
