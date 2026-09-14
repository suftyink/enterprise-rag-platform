import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol
from uuid import UUID

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)

from erp_assistant.core.config import get_settings
from erp_assistant.orchestration.provider_factory import get_embedding_provider
from erp_assistant.rag.chunker import ChunkDraft


@dataclass(frozen=True)
class VectorHit:
    chunk_id: UUID
    score: float


class VectorStore(Protocol):
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
        tenant_id: str,
        limit: int,
        rrf_k: int,
    ) -> list[VectorHit]: ...


class MilvusVectorStore:
    def __init__(self, uri: str, collection: str, dimension: int) -> None:
        self.uri = uri
        self.collection = collection
        self.dimension = dimension

    def _client(self) -> MilvusClient:
        return MilvusClient(uri=self.uri)

    def _ensure_collection(self, client: MilvusClient) -> None:
        if client.has_collection(self.collection):
            return
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=36)
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
            self.delete_document(document_id)
            if chunks:
                client.upsert(
                    self.collection,
                    data=[
                        {
                            "id": str(chunk.id),
                            "dense_vector": vector,
                            "text": chunk.content,
                            "scope": scope,
                            "scope_id": scope_id,
                            "document_id": str(document_id),
                            "version": version,
                        }
                        for chunk, vector in zip(chunks, vectors, strict=True)
                    ],
                )
        finally:
            client.close()

    def delete_document(self, document_id: UUID) -> None:
        client = self._client()
        try:
            if client.has_collection(self.collection):
                value = json.dumps(str(document_id))
                client.delete(self.collection, filter=f"document_id == {value}")
        finally:
            client.close()

    def search(
        self,
        query: str,
        vector: list[float],
        tenant_id: str,
        limit: int,
        rrf_k: int,
    ) -> list[VectorHit]:
        client = self._client()
        try:
            if not client.has_collection(self.collection):
                return []
            global_scope = json.dumps("GLOBAL")
            tenant_scope = json.dumps("TENANT")
            tenant = json.dumps(tenant_id)
            expression = (
                f'(scope == {global_scope}) or '
                f'(scope == {tenant_scope} and scope_id == {tenant})'
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
                output_fields=["document_id"],
            )
            return [
                VectorHit(chunk_id=UUID(str(row["id"])), score=float(row["distance"]))
                for row in rows[0]
            ]
        finally:
            client.close()


@lru_cache
def get_vector_store() -> MilvusVectorStore:
    settings = get_settings()
    embedding = get_embedding_provider()
    return MilvusVectorStore(
        uri=settings.milvus_uri,
        collection=settings.milvus_collection,
        dimension=embedding.dimension,
    )
