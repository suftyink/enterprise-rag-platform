from copy import deepcopy
from hashlib import sha256

import pytest

from erp_assistant.api.routes.knowledge import _classify_document
from erp_assistant.core.config import Settings
from erp_assistant.orchestration import provider_factory
from erp_assistant.orchestration.provider_factory import (
    get_chat_provider_for,
    validate_restricted_provider_url,
)
from erp_assistant.rag.errors import RagError
from erp_assistant.rag.runtime_composition import build_retrieval_runtime_registry
from erp_assistant.rag.types import DataClassification


def test_production_classification_uses_trusted_content_hash_not_client_key() -> None:
    content = b"restricted compensation policy"
    digest = sha256(content).hexdigest()
    settings = Settings(
        app_env="production",
        restricted_knowledge_sha256=digest,
    )

    assert (
        _classify_document(content, "client-selected-ordinary-key", settings)
        is DataClassification.RESTRICTED
    )
    with pytest.raises(RagError, match="生产文档必须先在服务端受信分类清单中登记"):
        _classify_document(b"unknown content", "ordinary-looking-key", settings)


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
