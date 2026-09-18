from __future__ import annotations

from typing import Any

import pytest

import erp_assistant.rag.index_provisioning as provisioning_module
from erp_assistant.rag.index_provisioning import (
    IndexProvisioningError,
    IndexSpecification,
    MilvusIndexProvisioner,
)


class ExistingMilvusClient:
    def __init__(
        self,
        *,
        dimension: int = 256,
        analyzer: str = '{"tokenizer":"jieba","filter":["cnalphanumonly"]}',
        function_type: int = 1,
        schema_fingerprint: str | None = None,
    ) -> None:
        self.dimension = dimension
        self.analyzer = analyzer
        self.function_type = function_type
        self.schema_fingerprint = schema_fingerprint or _specification().schema_fingerprint
        self.closed = False

    def has_collection(self, collection: str) -> bool:
        assert collection == "knowledge_v3_ordinary_d256_eabc_sdef_p123"
        return True

    def describe_collection(self, collection: str) -> dict[str, object]:
        assert collection == "knowledge_v3_ordinary_d256_eabc_sdef_p123"
        return {
            "description": (
                "erp-assistant-index:"
                f"schema={self.schema_fingerprint};embedding={'a' * 64};"
                f"identity={_specification().index_identity}"
            ),
            "auto_id": False,
            "enable_dynamic_field": False,
            "fields": [
                {
                    "name": "id",
                    "type": 21,
                    "is_primary": True,
                    "params": {"max_length": 36},
                },
                {"name": "chunk_id", "type": 21, "params": {"max_length": 36}},
                {
                    "name": "dense_vector",
                    "type": 101,
                    "params": {"dim": self.dimension},
                },
                {
                    "name": "text",
                    "type": 21,
                    "params": {
                        "max_length": 8192,
                        "enable_analyzer": True,
                        "analyzer_params": self.analyzer,
                    },
                },
                {"name": "sparse_vector", "type": 104, "is_function_output": True},
                {"name": "scope", "type": 21, "params": {"max_length": 16}},
                {"name": "scope_id", "type": 21, "params": {"max_length": 64}},
                {"name": "document_id", "type": 21, "params": {"max_length": 36}},
                {"name": "version", "type": 5},
                {
                    "name": "embedding_binding",
                    "type": 21,
                    "params": {"max_length": 255},
                },
            ],
            "functions": [
                {
                    "name": "text_bm25",
                    "type": self.function_type,
                    "input_field_names": ["text"],
                    "output_field_names": ["sparse_vector"],
                }
            ],
        }

    def list_indexes(self, *, collection_name: str) -> list[str]:
        assert collection_name == "knowledge_v3_ordinary_d256_eabc_sdef_p123"
        return ["dense", "sparse"]

    def describe_index(
        self, *, collection_name: str, index_name: str
    ) -> dict[str, object]:
        assert collection_name == "knowledge_v3_ordinary_d256_eabc_sdef_p123"
        return (
            {
                "field_name": "dense_vector",
                "metric_type": "COSINE",
                "index_type": "AUTOINDEX",
            }
            if index_name == "dense"
            else {
                "field_name": "sparse_vector",
                "metric_type": "BM25",
                "index_type": "AUTOINDEX",
            }
        )

    def close(self) -> None:
        self.closed = True


class CreatingMilvusClient(ExistingMilvusClient):
    def __init__(self, *, concurrent_create: bool) -> None:
        super().__init__()
        self.concurrent_create = concurrent_create
        self.exists = False
        self.create_calls = 0
        self.describe_calls = 0

    def has_collection(self, collection: str) -> bool:
        assert collection == "knowledge_v3_ordinary_d256_eabc_sdef_p123"
        return self.exists

    def create_collection(
        self,
        collection: str,
        *,
        schema: object,
        index_params: object,
    ) -> None:
        del schema, index_params
        assert collection == "knowledge_v3_ordinary_d256_eabc_sdef_p123"
        self.create_calls += 1
        self.exists = True
        if self.concurrent_create:
            raise RuntimeError("already exists")

    def describe_collection(self, collection: str) -> dict[str, object]:
        self.describe_calls += 1
        return super().describe_collection(collection)


class LegacyMilvusClient(ExistingMilvusClient):
    def __init__(self, embedding_binding: str) -> None:
        super().__init__()
        self.embedding_binding = embedding_binding

    def describe_collection(self, collection: str) -> dict[str, object]:
        observed = super().describe_collection(collection)
        observed["description"] = ""
        return observed

    def query_iterator(self, **kwargs: object) -> object:
        assert kwargs["collection_name"] == _specification().physical_locator
        rows = [{"embedding_binding": self.embedding_binding}]

        class Iterator:
            def next(self) -> list[dict[str, str]]:
                nonlocal rows
                result, rows = rows, []
                return result

            def close(self) -> None:
                return None

        return Iterator()


def test_existing_matching_milvus_index_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ExistingMilvusClient()
    monkeypatch.setattr(
        provisioning_module,
        "MilvusClient",
        lambda **kwargs: _return_client(client, kwargs),
    )
    specification = _specification()

    observed = MilvusIndexProvisioner().provision(specification)

    assert observed.physical_locator == specification.physical_locator
    assert observed.schema_fingerprint == specification.schema_fingerprint
    assert observed.embedding_binding_fingerprint == "a" * 64
    assert client.closed is True


@pytest.mark.parametrize(
    ("binding", "succeeds"),
    [("a" * 64, True), ("b" * 64, False)],
)
def test_legacy_reconcile_observes_persisted_embedding_binding(
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
    succeeds: bool,
) -> None:
    client = LegacyMilvusClient(binding)
    monkeypatch.setattr(
        provisioning_module,
        "MilvusClient",
        lambda **kwargs: _return_client(client, kwargs),
    )

    if succeeds:
        observed = MilvusIndexProvisioner().reconcile_legacy(_specification())
        assert observed.embedding_binding_fingerprint == "a" * 64
    else:
        with pytest.raises(IndexProvisioningError) as raised:
            MilvusIndexProvisioner().reconcile_legacy(_specification())
        assert raised.value.code == "INDEX_IDENTITY_COLLISION"


@pytest.mark.parametrize("concurrent_create", [False, True])
def test_milvus_creation_is_reconciled_before_success(
    monkeypatch: pytest.MonkeyPatch,
    concurrent_create: bool,
) -> None:
    client = CreatingMilvusClient(concurrent_create=concurrent_create)
    real_client = provisioning_module.MilvusClient

    class ClientFactory:
        create_schema = staticmethod(real_client.create_schema)
        prepare_index_params = staticmethod(real_client.prepare_index_params)

        def __new__(cls, **kwargs: object) -> CreatingMilvusClient:
            del cls
            return _return_client(client, kwargs)

    monkeypatch.setattr(provisioning_module, "MilvusClient", ClientFactory)

    observed = MilvusIndexProvisioner().provision(_specification())

    assert observed.physical_locator == _specification().physical_locator
    assert client.create_calls == 1
    assert client.describe_calls == 1
    assert client.closed is True


def test_existing_milvus_index_with_different_dimension_is_rejected_without_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ExistingMilvusClient(dimension=1024)
    monkeypatch.setattr(
        provisioning_module,
        "MilvusClient",
        lambda **kwargs: _return_client(client, kwargs),
    )

    with pytest.raises(IndexProvisioningError) as raised:
        MilvusIndexProvisioner().provision(_specification())

    assert raised.value.code == "INDEX_IDENTITY_COLLISION"
    assert raised.value.retryable is False
    assert not hasattr(client, "drop_collection")
    assert client.closed is True


@pytest.mark.parametrize(
    "client_options",
    [
        {"analyzer": '{"tokenizer":"standard"}'},
        {"function_type": 2},
        {"schema_fingerprint": "f" * 64},
    ],
    ids=["analyzer", "bm25-function", "stored-fingerprint"],
)
def test_existing_milvus_index_with_incompatible_identity_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    client_options: dict[str, object],
) -> None:
    client = ExistingMilvusClient(**client_options)  # type: ignore[arg-type]
    monkeypatch.setattr(
        provisioning_module,
        "MilvusClient",
        lambda **kwargs: _return_client(client, kwargs),
    )

    with pytest.raises(IndexProvisioningError) as raised:
        MilvusIndexProvisioner().provision(_specification())

    assert raised.value.code == "INDEX_IDENTITY_COLLISION"
    assert client.closed is True


def test_milvus_dependency_failure_is_normalized_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableClient:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def has_collection(self, collection: str) -> bool:
            del collection
            raise ConnectionError("socket detail must not escape")

        def close(self) -> None:
            pass

    monkeypatch.setattr(provisioning_module, "MilvusClient", UnavailableClient)

    with pytest.raises(IndexProvisioningError) as raised:
        MilvusIndexProvisioner().provision(_specification())

    assert raised.value.code == "INDEX_DEPENDENCY_UNAVAILABLE"
    assert raised.value.retryable is True
    assert "socket detail" not in raised.value.message


def test_reconcile_rejects_missing_physical_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CreatingMilvusClient(concurrent_create=False)
    monkeypatch.setattr(
        provisioning_module,
        "MilvusClient",
        lambda **kwargs: _return_client(client, kwargs),
    )

    with pytest.raises(IndexProvisioningError) as raised:
        MilvusIndexProvisioner().reconcile(_specification())

    assert raised.value.code == "INDEX_NOT_FOUND"
    assert raised.value.retryable is False
    assert client.create_calls == 0


def _specification() -> IndexSpecification:
    return IndexSpecification.create(
        uri="http://milvus.internal:19530",
        physical_locator="knowledge_v3_ordinary_d256_eabc_sdef_p123",
        dimension=256,
        embedding_binding_fingerprint="a" * 64,
        index_identity="milvus-v3:test:knowledge:dense-256:bm25:abc",
    )


def _return_client(client: ExistingMilvusClient, kwargs: dict[str, Any]) -> ExistingMilvusClient:
    assert kwargs == {"uri": "http://milvus.internal:19530"}
    return client
