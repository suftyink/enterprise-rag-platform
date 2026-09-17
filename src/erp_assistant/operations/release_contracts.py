from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from erp_assistant.rag.types import DataClassification


class ReleaseStatus(StrEnum):
    BUILDING = "BUILDING"
    EVALUATING = "EVALUATING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    ROLLBACK = "ROLLBACK"
    FAILED = "FAILED"
    DEGRADED = "DEGRADED"


class RolloutState(StrEnum):
    DISABLED = "DISABLED"
    CANARY = "CANARY"
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"


class ReleaseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ReleaseApproval:
    approved_by: str
    pilot_scope: str
    capacity: str
    monitoring_owner: str
    rollback_owner: str
    reason: str


@dataclass(frozen=True)
class CorpusReleaseView:
    id: UUID
    release_version: str
    status: ReleaseStatus
    data_classification: DataClassification
    corpus_generation: int
    corpus_hash: str
    document_count: int
    profile_id: UUID
    index_identity: str
    index_build_hash: str
    release_fingerprint: str | None
    readiness_report_id: UUID | None
    previous_release_id: UUID | None
    rollout_state: RolloutState
    rollout_subject_ids: tuple[str, ...]
    degraded_reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "releaseId": str(self.id),
            "releaseVersion": self.release_version,
            "status": self.status.value,
            "dataClassification": self.data_classification.value,
            "corpusGeneration": self.corpus_generation,
            "corpusHash": self.corpus_hash,
            "documentCount": self.document_count,
            "profileId": str(self.profile_id),
            "indexIdentity": self.index_identity,
            "indexBuildHash": self.index_build_hash or None,
            "releaseFingerprint": self.release_fingerprint,
            "readinessReportId": (
                str(self.readiness_report_id) if self.readiness_report_id else None
            ),
            "previousReleaseId": (
                str(self.previous_release_id) if self.previous_release_id else None
            ),
            "rolloutState": self.rollout_state.value,
            "rolloutSubjectIds": list(self.rollout_subject_ids),
            "degradedReason": self.degraded_reason,
        }
