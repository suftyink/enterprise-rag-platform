from functools import lru_cache

from erp_assistant.core.config import get_settings
from erp_assistant.orchestration.fake_providers import (
    FakeEmbeddingProvider,
    FakeRerankProvider,
)
from erp_assistant.orchestration.providers import EmbeddingProvider, RerankProvider


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    provider = get_settings().embedding_provider
    if provider == "fake":
        return FakeEmbeddingProvider()
    raise ValueError(f"Unsupported embedding provider: {provider}")


@lru_cache
def get_rerank_provider() -> RerankProvider:
    provider = get_settings().rerank_provider
    if provider == "fake":
        return FakeRerankProvider()
    raise ValueError(f"Unsupported rerank provider: {provider}")
