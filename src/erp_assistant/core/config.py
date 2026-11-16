from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EmbeddingDeploymentSettings(BaseModel):
    base_url: AnyHttpUrl
    api_key: SecretStr = SecretStr("")
    deployment: NonEmptyStr
    model: NonEmptyStr
    version: NonEmptyStr
    dimension: int = Field(gt=0)


class RerankDeploymentSettings(BaseModel):
    base_url: AnyHttpUrl
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
    parser_version: NonEmptyStr
    chunk_version: NonEmptyStr
    embedding: EmbeddingDeploymentSettings
    reranker: RerankDeploymentSettings
    vector_index: VectorIndexDeploymentSettings
    rrf_k: int = Field(default=60, gt=0)
    candidate_multiplier: int = Field(default=4, gt=0)
    max_candidates: int = Field(default=80, ge=20)
    rerank_threshold: float = Field(default=0.10, ge=0, le=1)
    chunk_target: int | None = Field(default=None, gt=0)
    chunk_max: int | None = Field(default=None, gt=0)
    chunk_overlap: int | None = Field(default=None, ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Enterprise RAG Knowledge Platform"
    app_env: str = "development"
    api_prefix: str = "/api/assistant/v1"
    database_url: str = (
        "postgresql+asyncpg://erp_assistant:erp_assistant@localhost:5432/erp_assistant"
    )
    redis_url: str = "redis://localhost:6379/0"
    model_provider: str = "fake"
    chat_provider: str = "fake"
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
    ordinary_chat_base_url: str = ""
    ordinary_chat_api_key: str = ""
    ordinary_chat_deployment: str = ""
    ordinary_chat_model: str = ""
    ordinary_chat_version: str = ""
    restricted_chat_base_url: str = ""
    restricted_chat_api_key: str = ""
    restricted_chat_deployment: str = ""
    restricted_chat_model: str = ""
    restricted_chat_version: str = ""
    retrieval_provider_timeout_seconds: float = Field(default=10.0, gt=0)
    chat_provider_timeout_seconds: float = Field(default=30.0, gt=0)
    answer_context_token_budget: int = Field(default=4_096, ge=64)
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
    identity_provider: Literal["oidc", "test"] = "test"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_subject_claim: str = "sub"
    oidc_jwks_cache_seconds: int = Field(default=300, gt=0, le=86_400)
    oidc_jwks_max_keys: int = Field(default=32, gt=0, le=128)
    oidc_authorization_url: str = ""
    oidc_token_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_scopes: str = "openid profile"
    identity_memberships_json: str = "{}"
    internal_web_base_url: str = "http://localhost:8000"
    internal_web_session_ttl_seconds: int = Field(default=28_800, ge=300, le=86_400)
    internal_web_auth_state_ttl_seconds: int = Field(default=600, ge=60, le=1_800)
    internal_web_test_subject: str = ""
    evaluation_bearer_token: SecretStr = SecretStr("")
    pilot_release_fingerprint: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")

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
