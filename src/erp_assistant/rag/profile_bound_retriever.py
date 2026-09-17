from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings
from erp_assistant.identity import IdentityAccess
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.models.operations import CorpusReleaseRecord
from erp_assistant.operations.contracts import NullTelemetry, Telemetry
from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.retriever import KnowledgeRetriever, RetrievalOutcome, RetrievalRequest
from erp_assistant.rag.runtime_registry import RetrievalRuntimeRegistry


class ProfileBoundRetriever:
    """Fail closed when production queries do not match the atomically active profile."""

    def __init__(
        self,
        session: AsyncSession,
        retriever: KnowledgeRetriever,
        settings: Settings,
        *,
        identity_access: IdentityAccess,
        telemetry: Telemetry | None = None,
        runtime_registry: RetrievalRuntimeRegistry | None = None,
    ) -> None:
        self._session = session
        self._retriever = retriever
        self._runtime_registry = runtime_registry
        self._identity = identity_access
        self._telemetry = telemetry or NullTelemetry()
        self._enforce_binding = settings.app_env.casefold() == "production"

    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        if not self._enforce_binding and request.expected_release_id is None:
            return await self._retriever.retrieve(request)
        active_release = await self._session.scalar(
            select(CorpusReleaseRecord).where(
                CorpusReleaseRecord.data_classification == request.data_classification.value,
                CorpusReleaseRecord.status == "ACTIVE",
            )
        )
        if request.expected_release_id is not None and (
            active_release is None
            or active_release.id != request.expected_release_id
            or (
                request.expected_release_version is not None
                and active_release.release_version != request.expected_release_version
            )
        ):
            raise RagDependencyError(
                "ACTIVE_RELEASE_CHANGED",
                "问答授权的 Release 已不再活动，请重试",
            )
        if active_release is not None:
            active = await self._session.get(
                RetrievalProfileRecord, active_release.profile_id
            )
        else:
            active = await self._session.scalar(
                select(RetrievalProfileRecord).where(
                    RetrievalProfileRecord.data_classification
                    == request.data_classification.value,
                    RetrievalProfileRecord.status == "ACTIVE",
                )
            )
        if active is None:
            raise RagDependencyError("ACTIVE_PROFILE_UNAVAILABLE", "没有已激活的检索 Profile")
        retriever = self._retriever
        if self._runtime_registry is not None and self._enforce_binding:
            retriever = self._runtime_registry.resolve(
                active,
                self._session,
                self._identity,
                self._telemetry,
            )
        elif self._enforce_binding and (
            not active.production_ready
            or active.index_identity != self._retriever.index_identity
            or active.configuration_fingerprint != self._retriever.configuration_fingerprint()
        ):
            raise RagDependencyError(
                "ACTIVE_PROFILE_BINDING_MISMATCH", "运行时检索绑定与活动 Profile 不一致"
            )
        outcome = await retriever.retrieve(request)
        if active_release is None:
            return outcome
        confirmed_release_id = await self._session.scalar(
            select(CorpusReleaseRecord.id).where(
                CorpusReleaseRecord.id == active_release.id,
                CorpusReleaseRecord.status == "ACTIVE",
            )
        )
        if confirmed_release_id is None:
            raise RagDependencyError(
                "ACTIVE_RELEASE_CHANGED",
                "检索期间活动 Release 已变化，请重试",
            )
        return replace(
            outcome,
            release_id=active_release.id,
            release_version=active_release.release_version,
        )
