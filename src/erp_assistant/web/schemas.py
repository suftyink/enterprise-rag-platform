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
