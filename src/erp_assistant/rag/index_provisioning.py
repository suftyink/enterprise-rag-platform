from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Protocol, cast

from pymilvus import DataType, Function, FunctionType, MilvusClient


class IndexProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class IndexSpecification:
    uri: str
    physical_locator: str
    dimension: int
    embedding_binding_fingerprint: str
    schema_fingerprint: str
    index_identity: str

    @classmethod
    def create(
        cls,
        *,
        uri: str,
        physical_locator: str,
        dimension: int,
        embedding_binding_fingerprint: str,
        index_identity: str,
    ) -> IndexSpecification:
        payload = {
            "analyzer": {"filter": ["cnalphanumonly"], "tokenizer": "jieba"},
            "denseMetric": "COSINE",
            "dimension": dimension,
            "fields": [
                "id",
                "chunk_id",
                "dense_vector",
                "text",
                "sparse_vector",
                "scope",
                "scope_id",
                "document_id",
                "version",
                "embedding_binding",
            ],
            "sparseMetric": "BM25",
            "version": "milvus-hybrid-v3",
        }
        fingerprint = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return cls(
            uri=uri,
            physical_locator=physical_locator,
            dimension=dimension,
            embedding_binding_fingerprint=embedding_binding_fingerprint,
            schema_fingerprint=fingerprint,
            index_identity=index_identity,
        )


@dataclass(frozen=True, slots=True)
class ProvisionedIndex:
    physical_locator: str
    schema_fingerprint: str
    embedding_binding_fingerprint: str
    index_identity: str


class IndexProvisioner(Protocol):
    def provision(self, specification: IndexSpecification) -> ProvisionedIndex: ...

    def reconcile(self, specification: IndexSpecification) -> ProvisionedIndex: ...

    def reconcile_legacy(
        self, specification: IndexSpecification
    ) -> ProvisionedIndex: ...


class InMemoryIndexProvisioner:
    def __init__(self) -> None:
        self.indexes: dict[str, IndexSpecification] = {}

    def provision(self, specification: IndexSpecification) -> ProvisionedIndex:
        existing = self.indexes.setdefault(specification.physical_locator, specification)
        if existing != specification:
            raise IndexProvisioningError(
                "INDEX_IDENTITY_COLLISION",
                "物理索引已存在，但其 schema 或模型绑定与候选不一致",
                retryable=False,
            )
        return _provisioned(specification)

    def reconcile(self, specification: IndexSpecification) -> ProvisionedIndex:
        existing = self.indexes.get(specification.physical_locator)
        if existing is None:
            raise IndexProvisioningError(
                "INDEX_NOT_FOUND",
                "物理索引不存在",
                retryable=False,
            )
        if existing != specification:
            raise IndexProvisioningError(
                "INDEX_IDENTITY_COLLISION",
                "物理索引已存在，但其 schema 或模型绑定与候选不一致",
                retryable=False,
            )
        return _provisioned(specification)

    def reconcile_legacy(self, specification: IndexSpecification) -> ProvisionedIndex:
        return self.reconcile(specification)


class MilvusIndexProvisioner:
    def provision(self, specification: IndexSpecification) -> ProvisionedIndex:
        client: MilvusClient | None = None
        try:
            client = MilvusClient(uri=specification.uri)
            if client.has_collection(specification.physical_locator):
                self._reconcile(client, specification)
                return _provisioned(specification)
            schema = MilvusClient.create_schema(
                auto_id=False,
                enable_dynamic_field=False,
                description=_schema_description(specification),
            )
            schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=36)
            schema.add_field("chunk_id", DataType.VARCHAR, max_length=36)
            schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=specification.dimension)
            schema.add_field(
                "text",
                DataType.VARCHAR,
                max_length=8192,
                enable_analyzer=True,
                analyzer_params={"tokenizer": "jieba", "filter": ["cnalphanumonly"]},
            )
            schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
            schema.add_field("scope", DataType.VARCHAR, max_length=16)
            schema.add_field("scope_id", DataType.VARCHAR, max_length=64)
            schema.add_field("document_id", DataType.VARCHAR, max_length=36)
            schema.add_field("version", DataType.INT64)
            schema.add_field("embedding_binding", DataType.VARCHAR, max_length=255)
            schema.add_function(
                Function(
                    name="text_bm25",
                    input_field_names=["text"],
                    output_field_names=["sparse_vector"],
                    function_type=FunctionType.BM25,
                )
            )
            indexes = MilvusClient.prepare_index_params()
            indexes.add_index("dense_vector", index_type="AUTOINDEX", metric_type="COSINE")
            indexes.add_index("sparse_vector", index_type="AUTOINDEX", metric_type="BM25")
            try:
                client.create_collection(
                    specification.physical_locator,
                    schema=schema,
                    index_params=indexes,
                )
            except Exception as exc:
                if not client.has_collection(specification.physical_locator):
                    raise IndexProvisioningError(
                        "INDEX_PROVISION_FAILED",
                        "无法创建物理索引",
                        retryable=True,
                    ) from exc
            self._reconcile(client, specification)
            return _provisioned(specification)
        except IndexProvisioningError:
            raise
        except Exception as exc:
            raise IndexProvisioningError(
                "INDEX_DEPENDENCY_UNAVAILABLE",
                "物理索引依赖暂时不可用",
                retryable=True,
            ) from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def reconcile(self, specification: IndexSpecification) -> ProvisionedIndex:
        return self._reconcile_existing(specification, allow_legacy_description=False)

    def reconcile_legacy(self, specification: IndexSpecification) -> ProvisionedIndex:
        return self._reconcile_existing(specification, allow_legacy_description=True)

    def _reconcile_existing(
        self,
        specification: IndexSpecification,
        *,
        allow_legacy_description: bool,
    ) -> ProvisionedIndex:
        client: MilvusClient | None = None
        try:
            client = MilvusClient(uri=specification.uri)
            if not client.has_collection(specification.physical_locator):
                raise IndexProvisioningError(
                    "INDEX_NOT_FOUND",
                    "物理索引不存在",
                    retryable=False,
                )
            self._reconcile(
                client,
                specification,
                allow_legacy_description=allow_legacy_description,
            )
            return _provisioned(specification)
        except IndexProvisioningError:
            raise
        except Exception as exc:
            raise IndexProvisioningError(
                "INDEX_DEPENDENCY_UNAVAILABLE",
                "物理索引依赖暂时不可用",
                retryable=True,
            ) from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    @staticmethod
    def _reconcile(
        client: MilvusClient,
        specification: IndexSpecification,
        *,
        allow_legacy_description: bool = False,
    ) -> None:
        try:
            description = cast(
                dict[str, object], client.describe_collection(specification.physical_locator)
            )
            fields = cast(list[dict[str, object]], description.get("fields", []))
            by_name = {str(field.get("name")): field for field in fields}
            required = _required_fields(specification.dimension)
            dense = by_name["dense_vector"]
            params = cast(dict[str, object], dense.get("params") or dense.get("type_params") or {})
            dimension = int(str(params.get("dim", 0)))
            functions = cast(list[dict[str, object]], description.get("functions", []))
            index_names = tuple(
                map(
                    str,
                    client.list_indexes(
                        collection_name=specification.physical_locator
                    ),
                )
            )
            indexes = {
                str(item.get("field_name") or item.get("fieldName")): (
                    str(item.get("index_type") or item.get("indexType")),
                    str(item.get("metric_type") or item.get("metricType")),
                )
                for item in (
                    cast(
                        dict[str, object],
                        client.describe_index(
                            collection_name=specification.physical_locator,
                            index_name=name,
                        ),
                    )
                    for name in index_names
                )
            }
            legacy_binding_matches = True
            if allow_legacy_description and description.get("description") in {
                None,
                "",
            }:
                legacy_binding_matches = _legacy_embedding_binding_matches(
                    client,
                    specification,
                )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise IndexProvisioningError(
                "INDEX_IDENTITY_COLLISION",
                "无法验证已存在物理索引的完整身份",
                retryable=False,
            ) from exc
        if (
            not _fields_match(by_name, required)
            or dimension != specification.dimension
            or description.get("auto_id") is not False
            or description.get("enable_dynamic_field") is not False
            or not _description_matches(
                description.get("description"),
                specification,
                allow_legacy=allow_legacy_description,
            )
            or not _bm25_function_matches(functions)
            or indexes.get("dense_vector") != ("AUTOINDEX", "COSINE")
            or indexes.get("sparse_vector") != ("AUTOINDEX", "BM25")
            or not legacy_binding_matches
        ):
            raise IndexProvisioningError(
                "INDEX_IDENTITY_COLLISION",
                "物理索引已存在，但其 schema 或索引配置不匹配",
                retryable=False,
            )


def _schema_description(specification: IndexSpecification) -> str:
    return (
        "erp-assistant-index:"
        f"schema={specification.schema_fingerprint};"
        f"embedding={specification.embedding_binding_fingerprint};"
        f"identity={specification.index_identity}"
    )


def _description_matches(
    actual: object,
    specification: IndexSpecification,
    *,
    allow_legacy: bool,
) -> bool:
    if actual == _schema_description(specification):
        return True
    return allow_legacy and actual in {None, ""}


def _legacy_embedding_binding_matches(
    client: MilvusClient,
    specification: IndexSpecification,
) -> bool:
    iterator = client.query_iterator(
        collection_name=specification.physical_locator,
        filter="",
        output_fields=["embedding_binding"],
        batch_size=1000,
    )
    observed: set[str] = set()
    try:
        while True:
            rows = cast(list[dict[str, object]], iterator.next())
            if not rows:
                break
            observed.update(str(row.get("embedding_binding", "")) for row in rows)
            if observed != {specification.embedding_binding_fingerprint}:
                return False
    finally:
        iterator.close()
    return observed == {specification.embedding_binding_fingerprint}


def _required_fields(dimension: int) -> dict[str, tuple[int, dict[str, object]]]:
    return {
        "id": (DataType.VARCHAR.value, {"max_length": 36, "is_primary": True}),
        "chunk_id": (DataType.VARCHAR.value, {"max_length": 36}),
        "dense_vector": (DataType.FLOAT_VECTOR.value, {"dim": dimension}),
        "text": (
            DataType.VARCHAR.value,
            {
                "max_length": 8192,
                "enable_analyzer": True,
                "analyzer_params": {
                    "tokenizer": "jieba",
                    "filter": ["cnalphanumonly"],
                },
            },
        ),
        "sparse_vector": (
            DataType.SPARSE_FLOAT_VECTOR.value,
            {"is_function_output": True},
        ),
        "scope": (DataType.VARCHAR.value, {"max_length": 16}),
        "scope_id": (DataType.VARCHAR.value, {"max_length": 64}),
        "document_id": (DataType.VARCHAR.value, {"max_length": 36}),
        "version": (DataType.INT64.value, {}),
        "embedding_binding": (DataType.VARCHAR.value, {"max_length": 255}),
    }


def _fields_match(
    actual: dict[str, dict[str, object]],
    expected: dict[str, tuple[int, dict[str, object]]],
) -> bool:
    if set(actual) != set(expected):
        return False
    for name, (expected_type, expected_params) in expected.items():
        field = actual[name]
        if int(str(field.get("type", -1))) != expected_type:
            return False
        params = cast(
            dict[str, object], field.get("params") or field.get("type_params") or {}
        )
        observed = dict(params)
        if "is_primary" in expected_params:
            observed["is_primary"] = field.get("is_primary")
        if "is_function_output" in expected_params:
            observed["is_function_output"] = field.get("is_function_output")
        analyzer = observed.get("analyzer_params")
        if isinstance(analyzer, str):
            observed["analyzer_params"] = json.loads(analyzer)
        if any(observed.get(key) != value for key, value in expected_params.items()):
            return False
    return True


def _bm25_function_matches(functions: list[dict[str, object]]) -> bool:
    if len(functions) != 1:
        return False
    function = functions[0]
    return (
        function.get("name") == "text_bm25"
        and int(str(function.get("type", function.get("function_type", -1))))
        == FunctionType.BM25.value
        and function.get("input_field_names") == ["text"]
        and function.get("output_field_names") == ["sparse_vector"]
    )


def _provisioned(specification: IndexSpecification) -> ProvisionedIndex:
    values = asdict(specification)
    return ProvisionedIndex(
        physical_locator=str(values["physical_locator"]),
        schema_fingerprint=str(values["schema_fingerprint"]),
        embedding_binding_fingerprint=str(values["embedding_binding_fingerprint"]),
        index_identity=str(values["index_identity"]),
    )
