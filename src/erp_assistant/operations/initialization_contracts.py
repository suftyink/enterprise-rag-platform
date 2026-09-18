from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from erp_assistant.core.config import Settings
from erp_assistant.models.operations import KnowledgeInitializationRunRecord
from erp_assistant.orchestration.providers import (
    EmbeddingProvider,
    ProviderBoundary,
    RerankProvider,
)
from erp_assistant.rag.types import DataClassification

type CandidateBuildDispatcher = Callable[[UUID, UUID, str], str]


class InitializationMode(StrEnum):
    FRESH = "FRESH"
    MIGRATE_ACTIVE = "MIGRATE_ACTIVE"
    RESUME = "RESUME"


class InitializationStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class InitializationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        run_id: UUID | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run_id = run_id
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RuntimeOption:
    key: str
    label: str
    classification: DataClassification
    provider_boundary: ProviderBoundary
    parser_version: str
    chunk_version: str
    embedding_deployment: str
    embedding_model: str
    embedding_dimension: int
    rerank_deployment: str
    rerank_model: str
    vector_uri: str
    collection_base: str
    production_ready: bool
    runtime_settings: Settings
    embedding: EmbeddingProvider
    reranker: RerankProvider

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "dataClassification": self.classification.value,
            "providerBoundary": self.provider_boundary.value,
            "embeddingModel": self.embedding_model,
            "embeddingDimension": self.embedding_dimension,
            "rerankModel": self.rerank_model,
            "parserVersion": self.parser_version,
            "chunkVersion": self.chunk_version,
            "productionReady": self.production_ready,
        }


@dataclass(frozen=True, slots=True)
class InitializationCommand:
    classification: DataClassification
    idempotency_key: str
    runtime_key: str
    actor_id: str
    trace_id: UUID


@dataclass(frozen=True, slots=True)
class InitializationRunView:
    id: UUID
    classification: DataClassification
    mode: InitializationMode
    stage: str
    status: InitializationStatus
    attempt: int
    runtime_key: str | None
    configuration_fingerprint: str | None
    profile_id: UUID | None
    candidate_release_id: UUID | None
    failure_code: str | None
    failure_message: str | None
    retryable: bool
    progress: int
    revision: int
    next_action: str | None
    updated_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "runId": str(self.id),
            "dataClassification": self.classification.value,
            "mode": self.mode.value,
            "stage": self.stage,
            "status": self.status.value,
            "attempt": self.attempt,
            "runtimeKey": self.runtime_key,
            "configurationFingerprint": self.configuration_fingerprint,
            "profileId": str(self.profile_id) if self.profile_id else None,
            "candidateReleaseId": (
                str(self.candidate_release_id) if self.candidate_release_id else None
            ),
            "failureCode": self.failure_code,
            "failureMessage": self.failure_message,
            "retryable": self.retryable,
            "progress": self.progress,
            "revision": self.revision,
            "nextAction": self.next_action,
            "updatedAt": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class InitializationStateView:
    action: str
    run: InitializationRunView | None
    runtime_options: tuple[RuntimeOption, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "run": self.run.to_dict() if self.run else None,
            "runtimeOptions": [option.to_dict() for option in self.runtime_options],
        }

def view(run: KnowledgeInitializationRunRecord) -> InitializationRunView:
    if run.status == InitializationStatus.WAITING.value:
        next_action = "UPLOAD_AND_APPROVE_CONTENT"
    elif run.status == InitializationStatus.FAILED.value and run.retryable:
        next_action = "RETRY"
    elif (
        run.status == InitializationStatus.FAILED.value
        and not run.retryable
    ):
        next_action = "START_FRESH_INITIALIZATION"
    elif run.status == InitializationStatus.CANCELLED.value:
        next_action = "START_NEW_INITIALIZATION"
    elif run.status == InitializationStatus.SUCCEEDED.value:
        next_action = "RUN_EVALUATION"
    else:
        next_action = None
    return InitializationRunView(
        id=run.id,
        classification=DataClassification(run.data_classification),
        mode=InitializationMode(run.mode),
        stage=run.stage,
        status=InitializationStatus(run.status),
        attempt=run.attempt,
        runtime_key=run.runtime_key,
        configuration_fingerprint=run.configuration_fingerprint,
        profile_id=run.profile_id,
        candidate_release_id=run.candidate_release_id,
        failure_code=run.failure_code,
        failure_message=run.failure_message,
        retryable=run.retryable,
        progress=run.progress,
        revision=run.revision,
        next_action=next_action,
        updated_at=run.updated_at,
    )
