from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.evaluation.models import EvaluationReport, GateDecision
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.orchestration.providers import ProviderBoundary
from erp_assistant.rag.errors import RagError
from erp_assistant.rag.types import DataClassification


class ProfileStatus(StrEnum):
    STAGING = "STAGING"
    ACTIVE = "ACTIVE"
    ROLLBACK = "ROLLBACK"


@dataclass(frozen=True)
class RetrievalProfile:
    id: UUID
    profile_key: str
    version: int
    data_classification: DataClassification
    provider_boundary: ProviderBoundary
    embedding_deployment: str
    embedding_model: str
    embedding_dimension: int
    rerank_deployment: str
    rerank_model: str
    index_identity: str
    parser_version: str
    chunk_version: str
    retrieval_config: dict[str, int | float | bool | str]
    configuration_fingerprint: str
    production_ready: bool = True

    def validate(self) -> None:
        identities = (
            self.profile_key,
            self.embedding_deployment,
            self.embedding_model,
            self.rerank_deployment,
            self.rerank_model,
            self.index_identity,
            self.parser_version,
            self.chunk_version,
            self.configuration_fingerprint,
        )
        if (
            self.version < 1
            or self.embedding_dimension < 1
            or not all(value.strip() for value in identities)
            or self.data_classification is DataClassification.UNCLASSIFIED
        ):
            raise RagError("INVALID_RETRIEVAL_PROFILE", "检索 Profile 身份不完整")
        if (
            self.data_classification is DataClassification.RESTRICTED
            and self.provider_boundary is not ProviderBoundary.INTERNAL
        ):
            raise RagError("RESTRICTED_PROFILE_FORBIDDEN", "受限 Profile 必须位于内网边界")


@dataclass(frozen=True)
class ProfileActivation:
    profile_id: UUID
    previous_profile_id: UUID | None
    status: ProfileStatus


class ProfileActivator(Protocol):
    async def activate(self, profile_id: UUID, report: EvaluationReport) -> ProfileActivation: ...


class SqlRetrievalProfileStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        maximum_evidence_age: timedelta = timedelta(hours=24),
    ) -> None:
        self._sessions = session_factory
        self._maximum_evidence_age = maximum_evidence_age

    async def stage(self, profile: RetrievalProfile) -> None:
        profile.validate()
        async with self._sessions() as session:
            session.add(
                RetrievalProfileRecord(
                    id=profile.id,
                    profile_key=profile.profile_key,
                    version=profile.version,
                    data_classification=profile.data_classification.value,
                    provider_boundary=profile.provider_boundary.value,
                    embedding_deployment=profile.embedding_deployment,
                    embedding_model=profile.embedding_model,
                    embedding_dimension=profile.embedding_dimension,
                    rerank_deployment=profile.rerank_deployment,
                    rerank_model=profile.rerank_model,
                    index_identity=profile.index_identity,
                    parser_version=profile.parser_version,
                    chunk_version=profile.chunk_version,
                    retrieval_config=dict(profile.retrieval_config),
                    configuration_fingerprint=profile.configuration_fingerprint,
                    production_ready=profile.production_ready,
                    status=ProfileStatus.STAGING.value,
                )
            )
            await session.commit()

    async def activate(self, profile_id: UUID, report: EvaluationReport) -> ProfileActivation:
        if report.gate.decision is not GateDecision.PASS:
            raise RagError("PROFILE_GATE_NOT_PASSED", "只有通过完整质量门禁的 Profile 才能激活")
        executed_at = datetime.fromisoformat(report.metadata.executed_at)
        if datetime.now(UTC) - executed_at > self._maximum_evidence_age:
            raise RagError("PROFILE_EVIDENCE_EXPIRED", "Profile 评测证据已过期")
        async with self._sessions() as session:
            candidate = await session.scalar(
                select(RetrievalProfileRecord)
                .where(RetrievalProfileRecord.id == profile_id)
                .with_for_update()
            )
            if candidate is None:
                raise RagError("PROFILE_NOT_FOUND", "staging Profile 不存在")
            self._validate_evidence(candidate, report)
            active = await session.scalar(
                select(RetrievalProfileRecord)
                .where(
                    RetrievalProfileRecord.data_classification == candidate.data_classification,
                    RetrievalProfileRecord.status == ProfileStatus.ACTIVE.value,
                )
                .with_for_update()
            )
            if (
                active is not None
                and active.id != candidate.id
                and active.dataset_hash != report.metadata.dataset_hash
            ):
                raise RagError(
                    "PROFILE_DATASET_MISMATCH", "候选 Profile 必须使用与活动版本相同的数据集"
                )
            if active is not None and active.id != candidate.id:
                active.status = ProfileStatus.ROLLBACK.value
                await session.flush()
            candidate.previous_profile_id = (
                active.id if active and active.id != candidate.id else None
            )
            candidate.status = ProfileStatus.ACTIVE.value
            candidate.dataset_hash = report.metadata.dataset_hash
            candidate.comparison_key = report.metadata.comparison_key
            candidate.evaluated_at = executed_at
            candidate.activated_at = datetime.now(UTC)
            await session.commit()
            return ProfileActivation(
                profile_id=candidate.id,
                previous_profile_id=candidate.previous_profile_id,
                status=ProfileStatus.ACTIVE,
            )

    @staticmethod
    def _validate_evidence(candidate: RetrievalProfileRecord, report: EvaluationReport) -> None:
        route = next(
            (
                item
                for item in report.metadata.routes
                if item.data_classification.value == candidate.data_classification
            ),
            None,
        )
        matches = (
            report.metadata.index_identity == candidate.index_identity
            and report.metadata.parser_version == candidate.parser_version
            and report.metadata.chunk_version == candidate.chunk_version
            and route is not None
            and route.embedding_model == candidate.embedding_model
            and route.embedding_deployment == candidate.embedding_deployment
            and route.embedding_dimension == candidate.embedding_dimension
            and route.rerank_model == candidate.rerank_model
            and route.rerank_deployment == candidate.rerank_deployment
            and route.provider_boundary.value == candidate.provider_boundary
            and route.production_ready
            and candidate.production_ready
            and route.configuration_fingerprint == candidate.configuration_fingerprint
            and route.retrieval_config == candidate.retrieval_config
        )
        if not matches:
            raise RagError("PROFILE_EVIDENCE_MISMATCH", "评测证据与 staging Profile 不匹配")
