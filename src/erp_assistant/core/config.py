from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    rag_enabled: bool = False
    milvus_uri: str = "http://localhost:19530"
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "erp-knowledge"
    milvus_collection: str = "knowledge_chunks_hybrid_v1"
    rag_rrf_k: int = 60
    rag_candidate_multiplier: int = 4
    rag_max_candidates: int = 80
    rag_rerank_threshold: float = 0.10
    knowledge_max_file_size: int = 20 * 1024 * 1024
    knowledge_max_pages: int = 200
    knowledge_chunk_target: int = 600
    knowledge_chunk_max: int = 900
    knowledge_chunk_overlap: int = 100
    knowledge_min_text_per_page: int = 20
    allow_global_knowledge_upload: bool = False
    global_knowledge_operator_ids: str = ""

    @property
    def global_knowledge_operators(self) -> set[str]:
        return {
            value.strip()
            for value in self.global_knowledge_operator_ids.split(",")
            if value.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
