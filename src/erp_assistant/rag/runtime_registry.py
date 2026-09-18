from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings
from erp_assistant.identity import IdentityAccess
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.operations.contracts import Telemetry
from erp_assistant.orchestration.providers import (
    EmbeddingProvider,
    ProviderBoundary,
    RerankProvider,
    embedding_binding_fingerprint,
)
from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.retriever import (
    KnowledgeRetriever,
    retrieval_configuration,
    retrieval_configuration_fingerprint,
)
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import MilvusVectorStore, VectorStore


@dataclass(frozen=True)
class RetrievalRuntimeBinding:
    """A process-owned, pre-approved model/index combination."""

    data_classification: DataClassification
    vector_store: VectorStore
    embedding: EmbeddingProvider
    reranker: RerankProvider
    settings: Settings
    parser_version: str
    chunk_version: str
    allow_non_production: bool = False

    def __post_init__(self) -> None:
        if not self.parser_version.strip() or not self.chunk_version.strip():
            raise ValueError("Runtime binding parser and chunk versions are required")
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
        if not self.production_ready and not self.allow_non_production:
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
    def registry_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.data_classification.value,
            self.vector_store.identity,
            self.configuration_fingerprint,
            self.parser_version,
            self.chunk_version,
        )

    def matches(self, profile: RetrievalProfileRecord) -> bool:
        return (
            profile.data_classification == self.data_classification.value
            and profile.provider_boundary == self.provider_boundary.value
            and profile.embedding_deployment == str(getattr(self.embedding, "deployment", "legacy"))
            and profile.embedding_model == self.embedding.model
            and profile.embedding_dimension == self.embedding.dimension
            and profile.rerank_deployment == str(getattr(self.reranker, "deployment", "legacy"))
            and profile.rerank_model == self.reranker.model
            and profile.index_identity == self.vector_store.identity
            and profile.parser_version == self.parser_version
            and profile.chunk_version == self.chunk_version
            and profile.retrieval_config == self.retrieval_config
            and profile.configuration_fingerprint == self.configuration_fingerprint
            and profile.production_ready == self.production_ready
        )

    def for_profile(self, profile: RetrievalProfileRecord) -> RetrievalRuntimeBinding:
        locator = profile.physical_index_locator
        if (
            locator is None
            or not isinstance(self.vector_store, MilvusVectorStore)
            or locator == self.vector_store.collection
        ):
            return self
        store = MilvusVectorStore(
            uri=self.vector_store.uri,
            collection=self.vector_store.base_collection,
            dimension=self.vector_store.dimension,
            embedding_binding=embedding_binding_fingerprint(self.embedding),
            physical_collection=locator,
        )
        return replace(self, vector_store=store)

    def build(
        self,
        session: AsyncSession,
        identity_access: IdentityAccess,
        telemetry: Telemetry | None = None,
    ) -> KnowledgeRetriever:
        return KnowledgeRetriever(
            session=session,
            vector_store=self.vector_store,
            embedding=self.embedding,
            reranker=self.reranker,
            settings=self.settings,
            identity_access=identity_access,
            telemetry=telemetry,
        )


class RetrievalRuntimeRegistry:
    """Resolves only bindings approved during process composition."""

    def __init__(self, bindings: Iterable[RetrievalRuntimeBinding]) -> None:
        self._bindings: dict[tuple[str, str, str, str, str], RetrievalRuntimeBinding] = {}
        for binding in bindings:
            if binding.registry_key in self._bindings:
                raise ValueError("Duplicate retrieval runtime binding")
            self._bindings[binding.registry_key] = binding

    def can_resolve(self, profile: RetrievalProfileRecord) -> bool:
        try:
            self.binding_for(profile)
        except RagDependencyError:
            return False
        return True

    def resolve(
        self,
        profile: RetrievalProfileRecord,
        session: AsyncSession,
        identity_access: IdentityAccess,
        telemetry: Telemetry | None = None,
    ) -> KnowledgeRetriever:
        return self.binding_for(profile).build(session, identity_access, telemetry)

    def binding_for(self, profile: RetrievalProfileRecord) -> RetrievalRuntimeBinding:
        binding = self._bindings.get(self._profile_key(profile))
        if binding is not None and binding.matches(profile):
            return binding
        for candidate in self._bindings.values():
            rebound = candidate.for_profile(profile)
            if rebound.matches(profile):
                return rebound
        raise RagDependencyError(
            "ACTIVE_PROFILE_BINDING_UNAVAILABLE",
            "活动 Profile 未注册为可解析的运行时绑定",
        )

    def vector_stores_for(
        self,
        classification: DataClassification,
        profiles: Iterable[RetrievalProfileRecord] = (),
    ) -> tuple[VectorStore, ...]:
        stores: dict[str, VectorStore] = {}
        for binding in self._bindings.values():
            if binding.data_classification is classification:
                stores.setdefault(binding.vector_store.identity, binding.vector_store)
        for profile in profiles:
            if profile.data_classification != classification.value:
                continue
            try:
                store = self.binding_for(profile).vector_store
            except RagDependencyError:
                continue
            stores.setdefault(store.identity, store)
        return tuple(stores.values())

    @property
    def bindings(self) -> tuple[RetrievalRuntimeBinding, ...]:
        return tuple(
            sorted(
                self._bindings.values(),
                key=lambda binding: binding.data_classification.value,
            )
        )

    @staticmethod
    def _profile_key(profile: RetrievalProfileRecord) -> tuple[str, str, str, str, str]:
        return (
            profile.data_classification,
            profile.index_identity,
            profile.configuration_fingerprint,
            profile.parser_version,
            profile.chunk_version,
        )
