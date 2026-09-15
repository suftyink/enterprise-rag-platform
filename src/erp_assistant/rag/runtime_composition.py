from functools import lru_cache

from erp_assistant.core.config import Settings, get_settings
from erp_assistant.orchestration.http_retrieval_providers import (
    HttpEmbeddingProvider,
    HttpRerankProvider,
)
from erp_assistant.orchestration.provider_factory import (
    _validated_restricted_url,
    create_retrieval_http_client,
)
from erp_assistant.orchestration.providers import ProviderBoundary, embedding_binding_fingerprint
from erp_assistant.rag.runtime_registry import (
    RetrievalRuntimeBinding,
    RetrievalRuntimeRegistry,
)
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import MilvusVectorStore


def build_retrieval_runtime_registry(settings: Settings) -> RetrievalRuntimeRegistry:
    bindings: list[RetrievalRuntimeBinding] = []
    for deployment in settings.retrieval_runtime_deployments:
        classification = DataClassification(deployment.data_classification)
        boundary = ProviderBoundary(deployment.provider_boundary)
        if (
            classification is DataClassification.RESTRICTED
            and boundary is not ProviderBoundary.INTERNAL
        ):
            raise ValueError("Restricted runtime deployment must use the internal boundary")
        embedding_url = deployment.embedding.base_url
        rerank_url = deployment.reranker.base_url
        if classification is DataClassification.RESTRICTED:
            embedding_url = _validated_restricted_url(
                embedding_url, settings.restricted_provider_hosts
            )
            rerank_url = _validated_restricted_url(rerank_url, settings.restricted_provider_hosts)
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
        runtime_settings = settings.model_copy(
            update={
                "rag_rrf_k": deployment.rrf_k,
                "rag_candidate_multiplier": deployment.candidate_multiplier,
                "rag_max_candidates": deployment.max_candidates,
                "rag_rerank_threshold": deployment.rerank_threshold,
            }
        )
        bindings.append(
            RetrievalRuntimeBinding(
                data_classification=classification,
                vector_store=vector_store,
                embedding=embedding,
                reranker=reranker,
                settings=runtime_settings,
            )
        )
    return RetrievalRuntimeRegistry(bindings)


@lru_cache
def get_retrieval_runtime_registry() -> RetrievalRuntimeRegistry:
    return build_retrieval_runtime_registry(get_settings())
