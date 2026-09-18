from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_assistant.rag.knowledge_corpus_contracts import LifecycleAction


class AnswerWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)


class FeedbackWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal["USEFUL", "NOT_USEFUL", "CITATION_ISSUE"]
    reason: str | None = Field(default=None, max_length=500)


class LifecycleWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action: LifecycleAction
    reason: str = Field(min_length=1, max_length=500)
    expected_revision: int = Field(alias="expectedRevision", ge=0)


class PilotAccessWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool
    pilot_scope: str = Field(alias="pilotScope", min_length=1, max_length=255)
    capacity: str = Field(min_length=1, max_length=255)
    monitoring_owner: str = Field(alias="monitoringOwner", min_length=1, max_length=128)
    rollback_owner: str = Field(alias="rollbackOwner", min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


class WebFeedbackReceipt(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    feedback_id: UUID = Field(alias="feedbackId")
    trace_id: UUID = Field(alias="traceId")
    rating: str
    requires_review: bool = Field(alias="requiresReview")


class ReleaseCandidateWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    release_version: str = Field(alias="releaseVersion", min_length=1, max_length=128)
    profile_id: UUID = Field(alias="profileId")


class ReleaseBootstrapWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    profile_id: UUID = Field(alias="profileId")


class ReleaseApprovalWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    pilot_scope: str = Field(alias="pilotScope", min_length=1, max_length=255)
    capacity: str = Field(min_length=1, max_length=255)
    monitoring_owner: str = Field(alias="monitoringOwner", min_length=1, max_length=128)
    rollback_owner: str = Field(alias="rollbackOwner", min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


class ReleaseReasonWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class ReleaseRolloutWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    state: Literal["DISABLED", "CANARY", "ENABLED", "PAUSED"]
    subject_ids: list[str] = Field(alias="subjectIds", default_factory=list, max_length=500)
    reason: str = Field(min_length=1, max_length=500)


class InitializationStartWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    data_classification: Literal["ORDINARY", "RESTRICTED"] = Field(
        default="ORDINARY", alias="dataClassification"
    )
    runtime_key: str = Field(alias="runtimeKey", min_length=1, max_length=128)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=128)


class InitializationMutationWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    expected_revision: int = Field(alias="expectedRevision", ge=0)


class InitializationMigrateWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    data_classification: Literal["ORDINARY", "RESTRICTED"] = Field(
        default="ORDINARY", alias="dataClassification"
    )
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=128)


class InitializationContinueWebRequest(InitializationMutationWebRequest):
    release_version: str = Field(alias="releaseVersion", min_length=1, max_length=128)
