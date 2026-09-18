from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest

from erp_assistant.api.knowledge_upload import classify_document
from erp_assistant.core.config import Settings
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.orchestration import provider_factory
from erp_assistant.orchestration.provider_factory import (
    get_chat_provider_for,
    validate_restricted_provider_url,
)
from erp_assistant.rag.errors import RagError
from erp_assistant.rag.runtime_composition import build_retrieval_runtime_registry
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import MilvusVectorStore


def test_production_classification_uses_trusted_content_hash_not_client_key() -> None:
    content = b"restricted compensation policy"
    digest = sha256(content).hexdigest()
    settings = Settings(
        app_env="production",
        restricted_knowledge_sha256=digest,
    )

    assert (
        classify_document(content, "client-selected-ordinary-key", settings)
        is DataClassification.RESTRICTED
    )
    with pytest.raises(RagError, match="生产文档必须先在服务端受信分类清单中登记"):
        classify_document(b"unknown content", "ordinary-looking-key", settings)


def test_restricted_provider_endpoint_must_be_in_independent_host_registry() -> None:
    with pytest.raises(ValueError, match="trusted host registry"):
        validate_restricted_provider_url(
            "https://public-provider.example/v1",
            {"internal-model.corp"},
        )

    assert (
        validate_restricted_provider_url(
            "https://internal-model.corp/v1",
            {"internal-model.corp"},
        )
        == "https://internal-model.corp/v1"
    )


def test_invalid_http_profile_is_rejected_before_allocating_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        embedding_provider="http",
        ordinary_embedding_base_url="https://model.example/v1",
    )
    monkeypatch.setattr(provider_factory, "get_settings", lambda: settings)
    provider_factory.get_embedding_provider_for.cache_clear()
    before = len(provider_factory._http_clients)

    with pytest.raises(ValueError, match="profile identity"):
        provider_factory.get_embedding_provider_for(DataClassification.ORDINARY)

    assert len(provider_factory._http_clients) == before


def test_restricted_chat_route_is_validated_before_allocating_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        chat_provider="http",
        restricted_chat_base_url="https://public-provider.example/v1",
        restricted_chat_deployment="restricted-chat-v1",
        restricted_chat_model="grounded-chat",
        restricted_chat_version="v1",
        restricted_provider_allowed_hosts="internal-model.corp",
    )
    monkeypatch.setattr(provider_factory, "get_settings", lambda: settings)
    get_chat_provider_for.cache_clear()
    before = len(provider_factory._http_clients)

    with pytest.raises(ValueError, match="trusted host registry"):
        get_chat_provider_for(DataClassification.RESTRICTED)

    assert len(provider_factory._http_clients) == before


def test_production_rejects_fake_chat_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(app_env="production", chat_provider="fake")
    monkeypatch.setattr(provider_factory, "get_settings", lambda: settings)
    get_chat_provider_for.cache_clear()

    with pytest.raises(ValueError, match="production-ready"):
        get_chat_provider_for(DataClassification.ORDINARY)


def test_runtime_registry_prevalidates_every_route_before_allocating_clients() -> None:
    settings = Settings(
        restricted_provider_allowed_hosts="internal-model.corp",
        retrieval_runtime_deployments=[
            {
                "data_classification": "ORDINARY",
                "provider_boundary": "CLOUD",
                "parser_version": "parser-v1",
                "chunk_version": "chunk-v1",
                "embedding": {
                    "base_url": "https://cloud-model.example/v1",
                    "deployment": "cloud-embed-v1",
                    "model": "semantic-embed",
                    "version": "v1",
                    "dimension": 3,
                },
                "reranker": {
                    "base_url": "https://cloud-model.example/v1",
                    "deployment": "cloud-rerank-v1",
                    "model": "semantic-rerank",
                    "version": "v1",
                },
                "vector_index": {
                    "uri": "http://milvus.internal:19530",
                    "collection": "ordinary-staging-v1",
                },
            },
            {
                "data_classification": "RESTRICTED",
                "provider_boundary": "INTERNAL",
                "parser_version": "parser-v1",
                "chunk_version": "chunk-v1",
                "embedding": {
                    "base_url": "https://public-provider.example/v1",
                    "deployment": "restricted-embed-v1",
                    "model": "semantic-embed",
                    "version": "v1",
                    "dimension": 3,
                },
                "reranker": {
                    "base_url": "https://internal-model.corp/v1",
                    "deployment": "restricted-rerank-v1",
                    "model": "semantic-rerank",
                    "version": "v1",
                },
                "vector_index": {
                    "uri": "http://milvus.internal:19530",
                    "collection": "restricted-staging-v1",
                },
            },
        ],
    )
    before = len(provider_factory._http_clients)

    with pytest.raises(ValueError, match="trusted host registry"):
        build_retrieval_runtime_registry(settings)

    assert len(provider_factory._http_clients) == before


def test_runtime_registry_rejects_exact_key_collision_before_allocating_clients() -> None:
    deployment = {
        "data_classification": "ORDINARY",
        "provider_boundary": "CLOUD",
        "parser_version": "parser-v1",
        "chunk_version": "chunk-v1",
        "embedding": {
            "base_url": "https://cloud-model.example/v1",
            "deployment": "cloud-embed-v1",
            "model": "semantic-embed",
            "version": "v1",
            "dimension": 3,
        },
        "reranker": {
            "base_url": "https://cloud-model.example/v1",
            "deployment": "cloud-rerank-v1",
            "model": "semantic-rerank",
            "version": "v1",
        },
        "vector_index": {
            "uri": "http://milvus.internal:19530",
            "collection": "ordinary-staging-v1",
        },
    }
    duplicate = deepcopy(deployment)
    duplicate["embedding"]["base_url"] = "https://other-cloud.example/v1"  # type: ignore[index]
    settings = Settings(retrieval_runtime_deployments=[deployment, duplicate])
    before = len(provider_factory._http_clients)

    with pytest.raises(ValueError, match="Duplicate retrieval runtime binding"):
        build_retrieval_runtime_registry(settings)

    assert len(provider_factory._http_clients) == before


def test_runtime_registry_exposes_profile_bound_physical_store_for_cleanup() -> None:
    registry = build_retrieval_runtime_registry(Settings(app_env="development"))
    base = next(
        binding
        for binding in registry.bindings
        if binding.data_classification is DataClassification.ORDINARY
    )
    assert isinstance(base.vector_store, MilvusVectorStore)
    physical = MilvusVectorStore(
        uri=base.vector_store.uri,
        collection=base.vector_store.base_collection,
        dimension=base.vector_store.dimension,
        embedding_binding=base.vector_store.embedding_binding,
        physical_collection="knowledge_v3_ordinary_profile_bound",
    )
    rebound = replace(base, vector_store=physical)
    profile = RetrievalProfileRecord(
        id=uuid4(),
        profile_key="ordinary-profile-bound",
        version=1,
        data_classification="ORDINARY",
        provider_boundary=rebound.provider_boundary.value,
        embedding_deployment=rebound.embedding.deployment,
        embedding_model=rebound.embedding.model,
        embedding_dimension=rebound.embedding.dimension,
        rerank_deployment=rebound.reranker.deployment,
        rerank_model=rebound.reranker.model,
        index_identity=physical.identity,
        physical_index_locator=physical.collection,
        index_schema_fingerprint="a" * 64,
        embedding_binding_fingerprint=physical.embedding_binding,
        parser_version=rebound.parser_version,
        chunk_version=rebound.chunk_version,
        retrieval_config=rebound.retrieval_config,
        configuration_fingerprint=rebound.configuration_fingerprint,
        production_ready=False,
        status="RETIRED",
    )

    stores = registry.vector_stores_for(DataClassification.ORDINARY, (profile,))

    assert physical.identity in {store.identity for store in stores}
    assert next(store for store in stores if store.identity == physical.identity).collection == (
        profile.physical_index_locator
    )
