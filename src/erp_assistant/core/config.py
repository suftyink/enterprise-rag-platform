from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EmbeddingDeploymentSettings(BaseModel):
    base_url: NonEmptyStr
    api_key: SecretStr = SecretStr("")
    deployment: NonEmptyStr
    model: NonEmptyStr
    version: NonEmptyStr
    dimension: int = Field(gt=0)


class RerankDeploymentSettings(BaseModel):
    base_url: NonEmptyStr
    api_key: SecretStr = SecretStr("")
    deployment: NonEmptyStr
    model: NonEmptyStr
    version: NonEmptyStr


class VectorIndexDeploymentSettings(BaseModel):
    uri: NonEmptyStr
    collection: NonEmptyStr


class RetrievalRuntimeDeploymentSettings(BaseModel):
    data_classification: Literal["ORDINARY", "RESTRICTED"]
    provider_boundary: Literal["CLOUD", "INTERNAL"]
    embedding: EmbeddingDeploymentSettings
    reranker: RerankDeploymentSettings
    vector_index: VectorIndexDeploymentSettings
    rrf_k: int = Field(default=60, gt=0)
    candidate_multiplier: int = Field(default=4, gt=0)
    max_candidates: int = Field(default=80, ge=20)
    rerank_threshold: float = Field(default=0.10, ge=0, le=1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Cross-border ERP AI Assistant"
    app_env: str = "development"
    api_prefix: str = "/api/assistant/v1"
    database_url: str = (
        "postgresql+asyncpg://erp_assistant:erp_assistant@localhost:5432/erp_assistant"
    )
    redis_url: str = "redis://localhost:6379/0"
    model_provider: str = "fake"
    embedding_provider: str = "fake"
    rerank_provider: str = "fake"
    ordinary_embedding_base_url: str = ""
    ordinary_embedding_api_key: str = ""
    ordinary_embedding_deployment: str = ""
    ordinary_embedding_model: str = ""
    ordinary_embedding_version: str = ""
    ordinary_embedding_dimension: int = 0
    ordinary_rerank_base_url: str = ""
    ordinary_rerank_api_key: str = ""
    ordinary_rerank_deployment: str = ""
    ordinary_rerank_model: str = ""
    ordinary_rerank_version: str = ""
    restricted_embedding_base_url: str = ""
    restricted_embedding_api_key: str = ""
    restricted_embedding_deployment: str = ""
    restricted_embedding_model: str = ""
    restricted_embedding_version: str = ""
    restricted_embedding_dimension: int = 0
    restricted_rerank_base_url: str = ""
    restricted_rerank_api_key: str = ""
    restricted_rerank_deployment: str = ""
    restricted_rerank_model: str = ""
    restricted_rerank_version: str = ""
    retrieval_provider_timeout_seconds: float = 10.0
    retrieval_runtime_deployments: list[RetrievalRuntimeDeploymentSettings] = Field(
        default_factory=list
    )
    restricted_knowledge_document_keys: str = ""
    ordinary_knowledge_sha256: str = ""
    restricted_knowledge_sha256: str = ""
    restricted_provider_allowed_hosts: str = ""
    rag_enabled: bool = False
    milvus_uri: str = "http://localhost:19530"
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "erp-knowledge"
    milvus_collection: str = "knowledge_chunks_hybrid_v2"
    restricted_milvus_collection: str = "knowledge_chunks_restricted_v1"
    rag_rrf_k: int = 60
    rag_candidate_multiplier: int = 4
    rag_max_candidates: int = 80
    rag_rerank_threshold: float = 0.10
    knowledge_max_file_size: int = 20 * 1024 * 1024
    knowledge_max_pages: int = 200
    knowledge_max_blocks: int = 5_000
    knowledge_max_uncompressed_size: int = 64 * 1024 * 1024
    knowledge_max_compression_ratio: int = 100
    knowledge_min_text_characters: int = 40
    knowledge_chunk_target: int = 600
    knowledge_chunk_max: int = 900
    knowledge_chunk_overlap: int = 100
    knowledge_min_text_per_page: int = 20
    knowledge_embedding_batch_size: int = 64
    knowledge_index_lease_seconds: int = 900
    allow_global_knowledge_upload: bool = False
    global_knowledge_operator_ids: str = ""

    @property
    def global_knowledge_operators(self) -> set[str]:
        return {
            value.strip()
            for value in self.global_knowledge_operator_ids.split(",")
            if value.strip()
        }

    @property
    def restricted_document_keys(self) -> set[str]:
        return {
            value.strip().casefold()
            for value in self.restricted_knowledge_document_keys.split(",")
            if value.strip()
        }

    @property
    def ordinary_document_hashes(self) -> set[str]:
        return {
            value.strip().casefold()
            for value in self.ordinary_knowledge_sha256.split(",")
            if value.strip()
        }

    @property
    def restricted_document_hashes(self) -> set[str]:
        return {
            value.strip().casefold()
            for value in self.restricted_knowledge_sha256.split(",")
            if value.strip()
        }

    @property
    def restricted_provider_hosts(self) -> set[str]:
        return {
            value.strip().casefold()
            for value in self.restricted_provider_allowed_hosts.split(",")
            if value.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
