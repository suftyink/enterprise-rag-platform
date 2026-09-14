from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_assistant.rag.types import DocumentStatus, KnowledgeScope, SearchDecision


class KnowledgeDocumentResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    document_id: UUID = Field(alias="documentId")
    document_key: str = Field(alias="documentKey")
    version: int
    scope: KnowledgeScope
    status: DocumentStatus
    duplicate: bool = False
    title: str
    page_count: int | None = Field(default=None, alias="pageCount")
    phase: str | None = None
    error_code: str | None = Field(default=None, alias="errorCode")
    error_detail: str | None = Field(default=None, alias="errorDetail")


class KnowledgeRetryRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tenant_id: str = Field(alias="tenantId", min_length=1, max_length=64)
    operator_id: str = Field(alias="operatorId", min_length=1, max_length=64)


class KnowledgeSearchRequest(KnowledgeRetryRequest):
    query: str = Field(min_length=1, max_length=2_000)
    top_k: int = Field(default=5, alias="topK", ge=1, le=20)


class KnowledgeSearchResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    chunk_id: UUID = Field(alias="chunkId")
    document_id: UUID = Field(alias="documentId")
    document_key: str = Field(alias="documentKey")
    title: str
    scope: KnowledgeScope
    version: int
    page_start: int = Field(alias="pageStart")
    page_end: int = Field(alias="pageEnd")
    section_title: str | None = Field(default=None, alias="sectionTitle")
    content: str
    score: float
    retrieval_rank: int = Field(alias="retrievalRank")


class KnowledgeSearchPipeline(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    retrieval: str
    embedding_model: str = Field(alias="embeddingModel")
    rerank_model: str = Field(alias="rerankModel")
    threshold: float


class KnowledgeSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    trace_id: UUID = Field(alias="traceId")
    query: str
    decision: SearchDecision
    pipeline: KnowledgeSearchPipeline
    results: list[KnowledgeSearchResult]
