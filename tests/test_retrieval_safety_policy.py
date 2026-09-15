from hashlib import sha256

import pytest

from erp_assistant.api.routes.knowledge import _classify_document
from erp_assistant.core.config import Settings
from erp_assistant.orchestration import provider_factory
from erp_assistant.orchestration.provider_factory import _validated_restricted_url
from erp_assistant.rag.errors import RagError
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
        _validated_restricted_url(
            "https://public-provider.example/v1",
            {"internal-model.corp"},
        )

    assert (
        _validated_restricted_url(
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
