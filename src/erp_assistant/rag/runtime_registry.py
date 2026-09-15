from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.orchestration.providers import (
    EmbeddingProvider,
    ProviderBoundary,
    RerankProvider,
)
from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.retriever import (
    KnowledgeRetriever,
    retrieval_configuration,
    retrieval_configuration_fingerprint,
)
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import VectorStore


@dataclass(frozen=True)
class RetrievalRuntimeBinding:
    """A process-owned, pre-approved model/index combination."""

    data_classification: DataClassification
    vector_store: VectorStore
    embedding: EmbeddingProvider
    reranker: RerankProvider
    settings: Settings

    def __post_init__(self) -> None:
        if self.data_classification is DataClassification.UNCLASSIFIED:
            raise ValueError("Unclassified data has no runtime binding")
        vector_dimension = getattr(self.vector_store, "dimension", self.embedding.dimension)
        if vector_dimension != self.embedding.dimension:
            raise ValueError("Runtime binding vector dimension does not match embedding")
        if (
            self.data_classification is DataClassification.RESTRICTED
            and self.provider_boundary is not ProviderBoundary.INTERNAL
        ):
            raise ValueError("Restricted runtime binding must use the internal boundary")
        if not self.production_ready:
            raise ValueError("Production runtime registry cannot contain fake providers")

    @property
    def provider_boundary(self) -> ProviderBoundary:
        embedding_boundary = ProviderBoundary(
            getattr(self.embedding, "boundary", ProviderBoundary.INTERNAL)
        )
        rerank_boundary = ProviderBoundary(getattr(self.reranker, "boundary", embedding_boundary))
        if embedding_boundary is not rerank_boundary:
            raise ValueError("Runtime binding provider boundaries do not match")
        return embedding_boundary

    @property
    def production_ready(self) -> bool:
        return bool(
            getattr(self.embedding, "production_ready", True)
            and getattr(self.reranker, "production_ready", True)
        )

    @property
    def retrieval_config(self) -> dict[str, int | float | bool | str]:
        return retrieval_configuration(
            settings=self.settings,
            vector_store=self.vector_store,
            embedding=self.embedding,
            reranker=self.reranker,
        )

    @property
    def configuration_fingerprint(self) -> str:
        return retrieval_configuration_fingerprint(self.retrieval_config)

    @property
    def registry_key(self) -> tuple[str, str, str]:
        return (
            self.data_classification.value,
            self.vector_store.identity,
            self.configuration_fingerprint,
        )

    def matches(self, profile: RetrievalProfileRecord) -> bool:
        return (
            profile.data_classification == self.data_classification.value
            and profile.provider_boundary == self.provider_boundary.value
            and profile.embedding_deployment
            == str(getattr(self.embedding, "deployment", "legacy"))
            and profile.embedding_model == self.embedding.model
            and profile.embedding_dimension == self.embedding.dimension
            and profile.rerank_deployment
            == str(getattr(self.reranker, "deployment", "legacy"))
            and profile.rerank_model == self.reranker.model
            and profile.index_identity == self.vector_store.identity
            and profile.retrieval_config == self.retrieval_config
            and profile.configuration_fingerprint == self.configuration_fingerprint
            and profile.production_ready
            and self.production_ready
        )

    def build(self, session: AsyncSession) -> KnowledgeRetriever:
        return KnowledgeRetriever(
            session=session,
            vector_store=self.vector_store,
            embedding=self.embedding,
            reranker=self.reranker,
            settings=self.settings,
        )


class RetrievalRuntimeRegistry:
    """Resolves only bindings approved during process composition."""

    def __init__(self, bindings: Iterable[RetrievalRuntimeBinding]) -> None:
        self._bindings: dict[tuple[str, str, str], RetrievalRuntimeBinding] = {}
        for binding in bindings:
            if binding.registry_key in self._bindings:
                raise ValueError("Duplicate retrieval runtime binding")
            self._bindings[binding.registry_key] = binding

    def can_resolve(self, profile: RetrievalProfileRecord) -> bool:
        binding = self._bindings.get(self._profile_key(profile))
        return binding is not None and binding.matches(profile)

    def resolve(
        self,
        profile: RetrievalProfileRecord,
        session: AsyncSession,
    ) -> KnowledgeRetriever:
        binding = self._bindings.get(self._profile_key(profile))
        if binding is None or not binding.matches(profile):
            raise RagDependencyError(
                "ACTIVE_PROFILE_BINDING_UNAVAILABLE",
                "活动 Profile 未注册为可解析的运行时绑定",
            )
        return binding.build(session)

    @staticmethod
    def _profile_key(profile: RetrievalProfileRecord) -> tuple[str, str, str]:
        return (
            profile.data_classification,
            profile.index_identity,
            profile.configuration_fingerprint,
        )
