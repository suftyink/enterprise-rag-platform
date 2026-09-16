from dataclasses import dataclass
from functools import lru_cache
from typing import cast

from erp_assistant.core.config import (
    RetrievalRuntimeDeploymentSettings,
    Settings,
    get_settings,
)
from erp_assistant.orchestration.http_retrieval_providers import (
    HttpEmbeddingProvider,
    HttpRerankProvider,
)
from erp_assistant.orchestration.provider_factory import (
    create_retrieval_http_client,
    validate_restricted_provider_url,
)
from erp_assistant.orchestration.providers import (
    EmbeddingProvider,
    ProviderBoundary,
    RerankProvider,
    embedding_binding_fingerprint,
)
from erp_assistant.rag.retriever import (
    retrieval_configuration,
    retrieval_configuration_fingerprint,
)
from erp_assistant.rag.runtime_registry import (
    RetrievalRuntimeBinding,
    RetrievalRuntimeRegistry,
)
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import MilvusVectorStore


def build_retrieval_runtime_registry(settings: Settings) -> RetrievalRuntimeRegistry:
    _validate_runtime_deployments(settings)
    bindings: list[RetrievalRuntimeBinding] = []
    for deployment in settings.retrieval_runtime_deployments:
        classification = DataClassification(deployment.data_classification)
        boundary = ProviderBoundary(deployment.provider_boundary)
        embedding_url, rerank_url = _provider_urls(settings, deployment)
        embedding = HttpEmbeddingProvider(
            create_retrieval_http_client(
                embedding_url, settings.retrieval_provider_timeout_seconds
            ),
            deployment=deployment.embedding.deployment,
            model=deployment.embedding.model,
            version=deployment.embedding.version,
            dimension=deployment.embedding.dimension,
            boundary=boundary,
            api_key=deployment.embedding.api_key.get_secret_value(),
        )
        reranker = HttpRerankProvider(
            create_retrieval_http_client(
                rerank_url, settings.retrieval_provider_timeout_seconds
            ),
            deployment=deployment.reranker.deployment,
            model=deployment.reranker.model,
            version=deployment.reranker.version,
            boundary=boundary,
            api_key=deployment.reranker.api_key.get_secret_value(),
        )
        vector_store = MilvusVectorStore(
            uri=deployment.vector_index.uri,
            collection=deployment.vector_index.collection,
            dimension=embedding.dimension,
            embedding_binding=embedding_binding_fingerprint(embedding),
        )
        runtime_settings = _runtime_settings(settings, deployment)
        bindings.append(
            RetrievalRuntimeBinding(
                data_classification=classification,
                vector_store=vector_store,
                embedding=embedding,
                reranker=reranker,
                settings=runtime_settings,
                parser_version=deployment.parser_version,
                chunk_version=deployment.chunk_version,
            )
        )
    return RetrievalRuntimeRegistry(bindings)


def _validate_runtime_deployments(settings: Settings) -> None:
    registry_keys: set[tuple[str, str, str, str, str]] = set()
    index_snapshots: dict[tuple[str, str], tuple[object, ...]] = {}
    for deployment in settings.retrieval_runtime_deployments:
        classification = DataClassification(deployment.data_classification)
        boundary = ProviderBoundary(deployment.provider_boundary)
        if (
            classification is DataClassification.RESTRICTED
            and boundary is not ProviderBoundary.INTERNAL
        ):
            raise ValueError("Restricted runtime deployment must use the internal boundary")
        if classification is DataClassification.RESTRICTED:
            _provider_urls(settings, deployment)
        runtime_settings = _runtime_settings(settings, deployment)
        if (
            runtime_settings.knowledge_chunk_max < runtime_settings.knowledge_chunk_target
            or runtime_settings.knowledge_chunk_overlap
            >= runtime_settings.knowledge_chunk_target
        ):
            raise ValueError("Runtime deployment chunk configuration is invalid")
        registry_key = _deployment_registry_key(settings, deployment)
        if registry_key in registry_keys:
            raise ValueError("Duplicate retrieval runtime binding")
        registry_keys.add(registry_key)
        index_key = (
            deployment.vector_index.uri,
            deployment.vector_index.collection,
        )
        snapshot_identity = (
            boundary,
            deployment.parser_version,
            deployment.chunk_version,
            deployment.embedding.deployment,
            deployment.embedding.model,
            deployment.embedding.version,
            deployment.embedding.dimension,
        )
        existing_snapshot = index_snapshots.setdefault(index_key, snapshot_identity)
        if existing_snapshot != snapshot_identity:
            raise ValueError("Indexing changes require an independent vector collection")


@dataclass(frozen=True)
class _EmbeddingIdentity:
    deployment: str
    model: str
    version: str
    dimension: int
    boundary: ProviderBoundary


@dataclass(frozen=True)
class _RerankIdentity:
    deployment: str
    model: str
    version: str
    boundary: ProviderBoundary


def _deployment_registry_key(
    settings: Settings,
    deployment: RetrievalRuntimeDeploymentSettings,
) -> tuple[str, str, str, str, str]:
    boundary = ProviderBoundary(deployment.provider_boundary)
    embedding = _EmbeddingIdentity(
        deployment=deployment.embedding.deployment,
        model=f"{deployment.embedding.model}:{deployment.embedding.version}",
        version=deployment.embedding.version,
        dimension=deployment.embedding.dimension,
        boundary=boundary,
    )
    reranker = _RerankIdentity(
        deployment=deployment.reranker.deployment,
        model=f"{deployment.reranker.model}:{deployment.reranker.version}",
        version=deployment.reranker.version,
        boundary=boundary,
    )
    embedding_provider = cast(EmbeddingProvider, embedding)
    rerank_provider = cast(RerankProvider, reranker)
    vector_store = MilvusVectorStore(
        uri=deployment.vector_index.uri,
        collection=deployment.vector_index.collection,
        dimension=embedding.dimension,
        embedding_binding=embedding_binding_fingerprint(embedding_provider),
    )
    config = retrieval_configuration(
        settings=_runtime_settings(settings, deployment),
        vector_store=vector_store,
        embedding=embedding_provider,
        reranker=rerank_provider,
    )
    return (
        deployment.data_classification,
        vector_store.identity,
        retrieval_configuration_fingerprint(config),
        deployment.parser_version,
        deployment.chunk_version,
    )


def _provider_urls(
    settings: Settings,
    deployment: RetrievalRuntimeDeploymentSettings,
) -> tuple[str, str]:
    embedding_url = str(deployment.embedding.base_url)
    rerank_url = str(deployment.reranker.base_url)
    if deployment.data_classification == DataClassification.RESTRICTED.value:
        embedding_url = validate_restricted_provider_url(
            embedding_url, settings.restricted_provider_hosts
        )
        rerank_url = validate_restricted_provider_url(
            rerank_url, settings.restricted_provider_hosts
        )
    return embedding_url, rerank_url


def _runtime_settings(
    settings: Settings,
    deployment: RetrievalRuntimeDeploymentSettings,
) -> Settings:
    return settings.model_copy(
        update={
            "rag_rrf_k": deployment.rrf_k,
            "rag_candidate_multiplier": deployment.candidate_multiplier,
            "rag_max_candidates": deployment.max_candidates,
            "rag_rerank_threshold": deployment.rerank_threshold,
            "knowledge_chunk_target": (
                deployment.chunk_target
                if deployment.chunk_target is not None
                else settings.knowledge_chunk_target
            ),
            "knowledge_chunk_max": (
                deployment.chunk_max
                if deployment.chunk_max is not None
                else settings.knowledge_chunk_max
            ),
            "knowledge_chunk_overlap": (
                deployment.chunk_overlap
                if deployment.chunk_overlap is not None
                else settings.knowledge_chunk_overlap
            ),
        }
    )


@lru_cache
def get_retrieval_runtime_registry() -> RetrievalRuntimeRegistry:
    return build_retrieval_runtime_registry(get_settings())
