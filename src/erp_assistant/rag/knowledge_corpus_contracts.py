from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from erp_assistant.rag.document_indexer import DocumentIndexOutcome
from erp_assistant.rag.types import (
    CleanupStatus,
    DocumentStatus,
    PublicationStatus,
    RetentionStatus,
)


@dataclass(frozen=True)
class CorpusActor:
    subject_id: str
    tenant_id: str
    trusted: bool = False


class CorpusProcessor(Protocol):
    async def index(self, document_id: UUID) -> DocumentIndexOutcome: ...


class CorpusAuthorizer(Protocol):
    def require(
        self,
        actor: CorpusActor,
        operation: str,
        scope: str,
        scope_id: str,
    ) -> None: ...


class CorpusError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LifecycleAction(StrEnum):
    PUBLISH = "PUBLISH"
    REJECT = "REJECT"
    WITHDRAW = "WITHDRAW"
    RESUBMIT = "RESUBMIT"
    ROLLBACK = "ROLLBACK"
    REQUEST_PURGE = "REQUEST_PURGE"
    MARK_CLEANUP_FAILED = "MARK_CLEANUP_FAILED"
    RETRY_CLEANUP = "RETRY_CLEANUP"
    COMPLETE_CLEANUP = "COMPLETE_CLEANUP"


class TrustedActorAuthorizer:
    def require(
        self,
        actor: CorpusActor,
        operation: str,
        scope: str,
        scope_id: str,
    ) -> None:
        del operation
        allowed_scope = scope == "GLOBAL" or actor.tenant_id in {scope_id, "*"}
        if not actor.trusted or not actor.subject_id.strip() or not allowed_scope:
            raise CorpusError("CORPUS_FORBIDDEN", "无权执行知识治理操作")


@dataclass(frozen=True)
class CorpusAuditEvent:
    action: str
    actor_id: str
    reason: str
    document_version: int
    revision: int
    result: str


@dataclass(frozen=True)
class CorpusCleanup:
    id: UUID
    kind: str
    status: CleanupStatus
    attempt: int
    artifacts: tuple[dict[str, str], ...]
    error_detail: str | None


@dataclass(frozen=True)
class CorpusChunkSummary:
    chunk_count: int
    character_count: int
    first_page: int | None
    last_page: int | None


@dataclass(frozen=True)
class CorpusDocumentView:
    document_id: UUID
    document_key: str
    version: int
    title: str
    original_filename: str
    media_type: str
    size_bytes: int
    sha256: str
    processing_status: DocumentStatus
    publication_status: PublicationStatus
    retention_status: RetentionStatus
    revision: int
    retrieval_eligible: bool
    warnings: tuple[str, ...]
    pipeline_revision: str | None
    chunk_summary: CorpusChunkSummary
    audit_events: tuple[CorpusAuditEvent, ...]
    cleanup_requests: tuple[CorpusCleanup, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "documentId": str(self.document_id),
            "status": self.processing_status.value,
            "publicationStatus": self.publication_status.value,
            "retentionStatus": self.retention_status.value,
            "revision": self.revision,
            "retrievalEligible": self.retrieval_eligible,
            "warnings": list(self.warnings),
            "pipelineRevision": self.pipeline_revision,
        }


SYSTEM_CORPUS_ACTOR = CorpusActor(subject_id="knowledge-index-worker", tenant_id="*", trusted=True)
