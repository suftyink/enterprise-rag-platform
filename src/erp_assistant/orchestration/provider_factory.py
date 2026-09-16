from functools import lru_cache
from urllib.parse import urlsplit

import httpx

from erp_assistant.core.config import get_settings
from erp_assistant.orchestration.fake_providers import (
    FakeEmbeddingProvider,
    FakeGroundedChatProvider,
    FakeRerankProvider,
)
from erp_assistant.orchestration.http_chat_provider import HttpChatProvider
from erp_assistant.orchestration.http_retrieval_providers import (
    HttpEmbeddingProvider,
    HttpRerankProvider,
)
from erp_assistant.orchestration.providers import (
    ChatProvider,
    EmbeddingProvider,
    ProviderBoundary,
    RerankProvider,
)
from erp_assistant.rag.types import DataClassification

_http_clients: list[httpx.AsyncClient] = []


def _http_client(base_url: str, timeout: float) -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
    _http_clients.append(client)
    return client


def create_retrieval_http_client(base_url: str, timeout: float) -> httpx.AsyncClient:
    return _http_client(base_url, timeout)


async def close_retrieval_provider_clients() -> None:
    clients = list(_http_clients)
    _http_clients.clear()
    for client in clients:
        await client.aclose()
    get_embedding_provider.cache_clear()
    get_embedding_provider_for.cache_clear()
    get_rerank_provider.cache_clear()
    get_rerank_provider_for.cache_clear()
    get_chat_provider_for.cache_clear()
    from erp_assistant.rag.runtime_composition import get_retrieval_runtime_registry

    get_retrieval_runtime_registry.cache_clear()


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    return get_embedding_provider_for(DataClassification.ORDINARY)


@lru_cache
def get_embedding_provider_for(classification: DataClassification) -> EmbeddingProvider:
    if classification is DataClassification.UNCLASSIFIED:
        raise ValueError("Unclassified data has no retrieval provider")
    settings = get_settings()
    provider = settings.embedding_provider
    if provider == "fake":
        return FakeEmbeddingProvider()
    if provider == "http":
        prefix = "restricted" if classification is DataClassification.RESTRICTED else "ordinary"
        base_url = str(getattr(settings, f"{prefix}_embedding_base_url"))
        deployment = str(getattr(settings, f"{prefix}_embedding_deployment"))
        model = str(getattr(settings, f"{prefix}_embedding_model"))
        version = str(getattr(settings, f"{prefix}_embedding_version"))
        dimension = int(getattr(settings, f"{prefix}_embedding_dimension"))
        if not all(value.strip() for value in (base_url, deployment, model, version)):
            raise ValueError("Embedding profile identity is invalid")
        if dimension < 1:
            raise ValueError("Embedding profile identity is invalid")
        if classification is DataClassification.RESTRICTED:
            base_url = validate_restricted_provider_url(
                base_url, settings.restricted_provider_hosts
            )
        return HttpEmbeddingProvider(
            _http_client(base_url, settings.retrieval_provider_timeout_seconds),
            deployment=deployment,
            model=model,
            version=version,
            dimension=dimension,
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
    if classification is DataClassification.UNCLASSIFIED:
        raise ValueError("Unclassified data has no retrieval provider")
    settings = get_settings()
    provider = settings.rerank_provider
    if provider == "fake":
        return FakeRerankProvider()
    if provider == "http":
        prefix = "restricted" if classification is DataClassification.RESTRICTED else "ordinary"
        base_url = str(getattr(settings, f"{prefix}_rerank_base_url"))
        deployment = str(getattr(settings, f"{prefix}_rerank_deployment"))
        model = str(getattr(settings, f"{prefix}_rerank_model"))
        version = str(getattr(settings, f"{prefix}_rerank_version"))
        if not all(value.strip() for value in (base_url, deployment, model, version)):
            raise ValueError("Rerank profile identity is invalid")
        if classification is DataClassification.RESTRICTED:
            base_url = validate_restricted_provider_url(
                base_url, settings.restricted_provider_hosts
            )
        return HttpRerankProvider(
            _http_client(base_url, settings.retrieval_provider_timeout_seconds),
            deployment=deployment,
            model=model,
            version=version,
            boundary=(
                ProviderBoundary.INTERNAL
                if classification is DataClassification.RESTRICTED
                else ProviderBoundary.CLOUD
            ),
            api_key=str(getattr(settings, f"{prefix}_rerank_api_key")),
        )
    raise ValueError(f"Unsupported rerank provider: {provider}")


@lru_cache
def get_chat_provider_for(classification: DataClassification) -> ChatProvider:
    if classification is DataClassification.UNCLASSIFIED:
        raise ValueError("Unclassified data has no chat provider")
    settings = get_settings()
    provider = settings.chat_provider
    if provider == "fake":
        return FakeGroundedChatProvider()
    if provider == "http":
        prefix = "restricted" if classification is DataClassification.RESTRICTED else "ordinary"
        base_url = str(getattr(settings, f"{prefix}_chat_base_url"))
        deployment = str(getattr(settings, f"{prefix}_chat_deployment"))
        model = str(getattr(settings, f"{prefix}_chat_model"))
        version = str(getattr(settings, f"{prefix}_chat_version"))
        if not all(value.strip() for value in (base_url, deployment, model, version)):
            raise ValueError("Chat profile identity is invalid")
        if classification is DataClassification.RESTRICTED:
            base_url = validate_restricted_provider_url(
                base_url, settings.restricted_provider_hosts
            )
        return HttpChatProvider(
            _http_client(base_url, settings.chat_provider_timeout_seconds),
            deployment=deployment,
            model=model,
            version=version,
            boundary=(
                ProviderBoundary.INTERNAL
                if classification is DataClassification.RESTRICTED
                else ProviderBoundary.CLOUD
            ),
            api_key=str(getattr(settings, f"{prefix}_chat_api_key")),
        )
    raise ValueError(f"Unsupported chat provider: {provider}")


def validate_restricted_provider_url(base_url: str, allowed_hosts: set[str]) -> str:
    host = urlsplit(base_url).hostname
    if host is None or host.casefold() not in allowed_hosts:
        raise ValueError("Restricted provider endpoint is not in the trusted host registry")
    return base_url
