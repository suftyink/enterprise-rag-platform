import json
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid5

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)

from erp_assistant.core.config import get_settings
from erp_assistant.identity.models import KnowledgeScopeRef
from erp_assistant.orchestration.provider_factory import get_embedding_provider_for
from erp_assistant.orchestration.providers import embedding_binding_fingerprint
from erp_assistant.rag.chunker import ChunkDraft
from erp_assistant.rag.types import DataClassification


@dataclass(frozen=True)
class VectorHit:
    chunk_id: UUID
    score: float


class VectorStore(Protocol):
    identity: str

    def upsert_document(
        self,
        document_id: UUID,
        scope: str,
        scope_id: str,
        version: int,
        chunks: list[ChunkDraft],
        vectors: list[list[float]],
    ) -> None: ...

    def delete_document(self, document_id: UUID) -> None: ...

    def search(
        self,
        query: str,
        vector: list[float],
        scopes: tuple[KnowledgeScopeRef, ...],
        limit: int,
        rrf_k: int,
    ) -> list[VectorHit]: ...


class MilvusVectorStore:
    def __init__(
        self,
        uri: str,
        collection: str,
        dimension: int,
        *,
        embedding_binding: str = "legacy",
    ) -> None:
        self.uri = uri
        self.base_collection = collection
        self.dimension = dimension
        self.embedding_binding = embedding_binding
        binding_hash = sha256(embedding_binding.encode("utf-8")).hexdigest()[:12]
        self.collection_prefix = f"{collection}_v3_d"
        self.collection = f"{self.collection_prefix}{dimension}"
        namespace = sha256(uri.encode("utf-8")).hexdigest()[:12]
        self.identity = (
            f"milvus-v3:{namespace}:{self.collection}:dense-{dimension}:bm25:{binding_hash}"
        )

    def _client(self) -> MilvusClient:
        return MilvusClient(uri=self.uri)

    def _ensure_collection(self, client: MilvusClient) -> None:
        if client.has_collection(self.collection):
            return
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=36)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=36)
        schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=self.dimension)
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
        index = MilvusClient.prepare_index_params()
        index.add_index("dense_vector", index_type="AUTOINDEX", metric_type="COSINE")
        index.add_index("sparse_vector", index_type="AUTOINDEX", metric_type="BM25")
        try:
            client.create_collection(self.collection, schema=schema, index_params=index)
        except Exception:
            if not client.has_collection(self.collection):
                raise

    def upsert_document(
        self,
        document_id: UUID,
        scope: str,
        scope_id: str,
        version: int,
        chunks: list[ChunkDraft],
        vectors: list[list[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("Chunk and vector counts do not match")
        client = self._client()
        try:
            self._ensure_collection(client)
            self._delete_from_collection(
                client,
                self.collection,
                document_id,
                embedding_binding=self.embedding_binding,
            )
            if chunks:
                client.upsert(
                    self.collection,
                    data=[
                        {
                            "id": str(uuid5(document_id, str(chunk.id))),
                            "chunk_id": str(chunk.id),
                            "dense_vector": vector,
                            "text": chunk.content,
                            "scope": scope,
                            "scope_id": scope_id,
                            "document_id": str(document_id),
                            "version": version,
                            "embedding_binding": self.embedding_binding,
                        }
                        for chunk, vector in zip(chunks, vectors, strict=True)
                    ],
                )
        finally:
            client.close()

    def delete_document(self, document_id: UUID) -> None:
        client = self._client()
        try:
            list_collections = getattr(client, "list_collections", None)
            collections = (
                [
                    name
                    for name in list_collections()
                    if str(name) == self.base_collection
                    or str(name).startswith(self.collection_prefix)
                ]
                if callable(list_collections)
                else [self.collection]
            )
            for collection in collections:
                self._delete_from_collection(client, str(collection), document_id)
        finally:
            client.close()

    @staticmethod
    def _delete_from_collection(
        client: MilvusClient,
        collection: str,
        document_id: UUID,
        *,
        embedding_binding: str | None = None,
    ) -> None:
        if not client.has_collection(collection):
            return
        expression = f"document_id == {json.dumps(str(document_id))}"
        if embedding_binding is not None:
            expression += f" and embedding_binding == {json.dumps(embedding_binding)}"
        client.delete(collection, filter=expression)

    def search(
        self,
        query: str,
        vector: list[float],
        scopes: tuple[KnowledgeScopeRef, ...],
        limit: int,
        rrf_k: int,
    ) -> list[VectorHit]:
        if not scopes:
            return []
        client = self._client()
        try:
            if not client.has_collection(self.collection):
                return []
            embedding_binding = json.dumps(self.embedding_binding)
            scope_expression = " or ".join(
                "(scope == "
                f"{json.dumps(scope.scope.value)} and scope_id == {json.dumps(scope.scope_id)})"
                for scope in scopes
            )
            expression = (
                f"embedding_binding == {embedding_binding} and "
                f"({scope_expression})"
            )
            dense_request = AnnSearchRequest(
                data=[vector],
                anns_field="dense_vector",
                param={"metric_type": "COSINE", "params": {}},
                limit=limit,
                expr=expression,
            )
            sparse_request = AnnSearchRequest(
                data=[query],
                anns_field="sparse_vector",
                param={"metric_type": "BM25", "params": {}},
                limit=limit,
                expr=expression,
            )
            rows = client.hybrid_search(
                self.collection,
                reqs=[dense_request, sparse_request],
                ranker=RRFRanker(k=rrf_k),
                limit=limit,
                output_fields=["chunk_id", "document_id"],
            )
            return [
                VectorHit(
                    chunk_id=UUID(str(row["entity"]["chunk_id"])),
                    score=float(row["distance"]),
                )
                for row in rows[0]
            ]
        finally:
            client.close()


@lru_cache
def get_vector_store() -> MilvusVectorStore:
    return get_vector_store_for(DataClassification.ORDINARY)


@lru_cache
def get_vector_store_for(classification: DataClassification) -> MilvusVectorStore:
    if classification is DataClassification.UNCLASSIFIED:
        raise ValueError("Unclassified data has no vector index")
    settings = get_settings()
    embedding = get_embedding_provider_for(classification)
    collection = (
        settings.restricted_milvus_collection
        if classification is DataClassification.RESTRICTED
        else settings.milvus_collection
    )
    return MilvusVectorStore(
        uri=settings.milvus_uri,
        collection=collection,
        dimension=embedding.dimension,
        embedding_binding=embedding_binding_fingerprint(embedding),
    )
