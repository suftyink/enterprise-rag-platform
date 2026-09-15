from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_assistant.orchestration.providers import ProviderBoundary
from erp_assistant.rag.types import (
    DataClassification,
    DocumentStatus,
    IndexJobStatus,
    KnowledgeScope,
    PublicationStatus,
    RetentionStatus,
    SearchDecision,
)


class KnowledgeDocumentResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    document_id: UUID = Field(alias="documentId")
    document_key: str = Field(alias="documentKey")
    version: int
    scope: KnowledgeScope
    status: DocumentStatus
    publication_status: PublicationStatus = Field(alias="publicationStatus")
    retention_status: RetentionStatus = Field(alias="retentionStatus")
    governance_revision: int = Field(alias="governanceRevision")
    retrieval_eligible: bool = Field(alias="retrievalEligible")
    duplicate: bool = False
    title: str
    media_type: str = Field(alias="mediaType")
    data_classification: DataClassification = Field(alias="dataClassification")
    page_count: int | None = Field(default=None, alias="pageCount")
    block_count: int | None = Field(default=None, alias="blockCount")
    phase: str | None = None
    job_status: IndexJobStatus | None = Field(default=None, alias="jobStatus")
    attempt: int | None = None
    chunk_count: int | None = Field(default=None, alias="chunkCount")
    warnings: list[str] = Field(default_factory=list)
    pipeline_revision: str | None = Field(default=None, alias="pipelineRevision")
    parser_version: str | None = Field(default=None, alias="parserVersion")
    chunking_version: str | None = Field(default=None, alias="chunkingVersion")
    quality_signals: dict[str, int | float | str] = Field(
        default_factory=dict, alias="qualitySignals"
    )
    cleanup_required: bool = Field(default=False, alias="cleanupRequired")
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
    page_start: int | None = Field(alias="pageStart")
    page_end: int | None = Field(alias="pageEnd")
    section_title: str | None = Field(default=None, alias="sectionTitle")
    heading_path: list[str] = Field(default_factory=list, alias="headingPath")
    anchor: str | None = None
    block_kind: str = Field(alias="blockKind")
    source_locator: dict[str, object] = Field(default_factory=dict, alias="sourceLocator")
    content: str
    score: float
    retrieval_rank: int = Field(alias="retrievalRank")


class KnowledgeSearchPipeline(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    retrieval: str
    embedding_model: str = Field(alias="embeddingModel")
    rerank_model: str = Field(alias="rerankModel")
    threshold: float
    config_fingerprint: str = Field(alias="configFingerprint")
    embedding_deployment: str = Field(alias="embeddingDeployment")
    embedding_dimension: int = Field(alias="embeddingDimension")
    rerank_deployment: str = Field(alias="rerankDeployment")
    provider_boundary: ProviderBoundary = Field(alias="providerBoundary")
    production_ready: bool = Field(alias="productionReady")
    request_count: int = Field(alias="requestCount")
    input_tokens: int = Field(alias="inputTokens")


class KnowledgeSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    trace_id: UUID = Field(alias="traceId")
    query: str
    decision: SearchDecision
    elapsed_ms: float = Field(alias="elapsedMs")
    pipeline: KnowledgeSearchPipeline
    results: list[KnowledgeSearchResult]
