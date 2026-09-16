from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from erp_assistant.identity import (
    ActorContext,
    IdentityAccess,
    IdentityAction,
    IdentityDenied,
    IdentityMembership,
    IdentityResource,
    IdentityRole,
)
from erp_assistant.rag.document_indexer import DocumentIndexOutcome
from erp_assistant.rag.types import (
    CleanupKind,
    CleanupStatus,
    DocumentStatus,
    KnowledgeScope,
    PublicationStatus,
    RetentionStatus,
)

CorpusActor = ActorContext


class CorpusProcessor(Protocol):
    async def index(
        self, document_id: UUID, *, trace_id: UUID | None = None
    ) -> DocumentIndexOutcome: ...


class CorpusAuthorizer(Protocol):
    async def require(
        self,
        actor: CorpusActor,
        action: IdentityAction,
        resource: IdentityResource,
        trace_id: UUID,
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


class IdentityCorpusAuthorizer:
    def __init__(self, access: IdentityAccess) -> None:
        self._access = access

    async def require(
        self,
        actor: CorpusActor,
        action: IdentityAction,
        resource: IdentityResource,
        trace_id: UUID,
    ) -> None:
        if action is IdentityAction.PUBLISH and resource.scope is KnowledgeScope.GLOBAL:
            action = IdentityAction.PUBLISH_GLOBAL
        try:
            await self._access.authorize(
                actor,
                action,
                resource,
                trace_id=trace_id,
            )
        except IdentityDenied as exc:
            raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在") from exc


@dataclass(frozen=True)
class CorpusAuditEvent:
    action: str
    actor_id: str
    reason: str
    document_version: int
    revision: int
    result: str
    created_at: datetime


@dataclass(frozen=True)
class CorpusCleanup:
    id: UUID
    idempotency_key: str
    kind: CleanupKind
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
    parser_version: str | None
    chunking_version: str | None
    quality_signals: dict[str, int | float | str]
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
            "parserVersion": self.parser_version,
            "chunkingVersion": self.chunking_version,
            "qualitySignals": self.quality_signals,
        }


SYSTEM_CORPUS_ACTOR = ActorContext._from_membership(
    "knowledge-index-worker",
    IdentityMembership(
        tenant_id="*",
        roles=frozenset({IdentityRole.SYSTEM_ADMIN}),
        knowledge_scopes=frozenset(),
        internal_only=True,
    ),
)
