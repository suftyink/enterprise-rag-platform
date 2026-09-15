from functools import lru_cache

import httpx

from erp_assistant.core.config import get_settings
from erp_assistant.orchestration.fake_providers import (
    FakeEmbeddingProvider,
    FakeRerankProvider,
)
from erp_assistant.orchestration.http_retrieval_providers import (
    HttpEmbeddingProvider,
    HttpRerankProvider,
)
from erp_assistant.orchestration.providers import (
    EmbeddingProvider,
    ProviderBoundary,
    RerankProvider,
)
from erp_assistant.rag.types import DataClassification


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    return get_embedding_provider_for(DataClassification.ORDINARY)


@lru_cache
def get_embedding_provider_for(classification: DataClassification) -> EmbeddingProvider:
    settings = get_settings()
    provider = settings.embedding_provider
    if provider == "fake":
        return FakeEmbeddingProvider()
    if provider == "http":
        prefix = "restricted" if classification is DataClassification.RESTRICTED else "ordinary"
        base_url = str(getattr(settings, f"{prefix}_embedding_base_url"))
        return HttpEmbeddingProvider(
            httpx.AsyncClient(
                base_url=base_url,
                timeout=settings.retrieval_provider_timeout_seconds,
            ),
            deployment=str(getattr(settings, f"{prefix}_embedding_deployment")),
            model=str(getattr(settings, f"{prefix}_embedding_model")),
            version=str(getattr(settings, f"{prefix}_embedding_version")),
            dimension=int(getattr(settings, f"{prefix}_embedding_dimension")),
            boundary=(
                ProviderBoundary.INTERNAL
                if classification is DataClassification.RESTRICTED
                else ProviderBoundary.CLOUD
            ),
            api_key=str(getattr(settings, f"{prefix}_embedding_api_key")),
        )
    raise ValueError(f"Unsupported embedding provider: {provider}")


@lru_cache
def get_rerank_provider() -> RerankProvider:
    return get_rerank_provider_for(DataClassification.ORDINARY)


@lru_cache
def get_rerank_provider_for(classification: DataClassification) -> RerankProvider:
    settings = get_settings()
    provider = settings.rerank_provider
    if provider == "fake":
        return FakeRerankProvider()
    if provider == "http":
        prefix = "restricted" if classification is DataClassification.RESTRICTED else "ordinary"
        base_url = str(getattr(settings, f"{prefix}_rerank_base_url"))
        return HttpRerankProvider(
            httpx.AsyncClient(
                base_url=base_url,
                timeout=settings.retrieval_provider_timeout_seconds,
            ),
            deployment=str(getattr(settings, f"{prefix}_rerank_deployment")),
            model=str(getattr(settings, f"{prefix}_rerank_model")),
            version=str(getattr(settings, f"{prefix}_rerank_version")),
            boundary=(
                ProviderBoundary.INTERNAL
                if classification is DataClassification.RESTRICTED
                else ProviderBoundary.CLOUD
            ),
            api_key=str(getattr(settings, f"{prefix}_rerank_api_key")),
        )
    raise ValueError(f"Unsupported rerank provider: {provider}")
