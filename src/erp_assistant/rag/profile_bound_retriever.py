from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings
from erp_assistant.models.knowledge import RetrievalProfileRecord
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
        runtime_registry: RetrievalRuntimeRegistry | None = None,
    ) -> None:
        self._session = session
        self._retriever = retriever
        self._runtime_registry = runtime_registry
        self._enforce_binding = settings.app_env.casefold() == "production"

    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        if not self._enforce_binding:
            return await self._retriever.retrieve(request)
        active = await self._session.scalar(
            select(RetrievalProfileRecord).where(
                RetrievalProfileRecord.data_classification == request.data_classification.value,
                RetrievalProfileRecord.status == "ACTIVE",
            )
        )
        if active is None:
            raise RagDependencyError("ACTIVE_PROFILE_UNAVAILABLE", "没有已激活的检索 Profile")
        if self._runtime_registry is not None:
            retriever = self._runtime_registry.resolve(active, self._session)
            return await retriever.retrieve(request)
        if (
            not active.production_ready
            or active.index_identity != self._retriever.index_identity
            or active.configuration_fingerprint != self._retriever.configuration_fingerprint()
        ):
            raise RagDependencyError(
                "ACTIVE_PROFILE_BINDING_MISMATCH", "运行时检索绑定与活动 Profile 不一致"
            )
        return await self._retriever.retrieve(request)
