# Spec 03：收敛 DocumentIndexer 文档处理模块

Type: spec
Status: ready-for-agent
Blocked by: rag-01-knowledge-retriever
Primary test seam: `DocumentIndexer.index()`

## Problem Statement

当前文档处理集中在一个服务函数内，函数自行创建数据库会话、对象存储、向量存储和 Embedding 依赖；Celery 任务无法准确表达处理结果，并可能把失败、OCR_REQUIRED 或跳过统一报告为 processed。这使幂等、重试、错误分类和跨存储一致性难以从模块边界验证。

## Solution

建立 `DocumentIndexer` 深模块，以 `index(document_id)` 作为唯一处理入口，注入运行依赖并返回结构化处理结果。Celery 只负责调用该入口及根据错误类别重试，不再理解解析、Chunk、Embedding 或状态流转。

## User Stories

1. As a knowledge administrator, I want document processing status to reflect the real outcome, so that I know whether intervention is required.
2. As an operator, I want retryable failures distinguished from permanent parsing failures, so that Celery retries only useful work.
3. As an operator, I want OCR-required documents reported explicitly, so that they are not mistaken for successful indexing.
4. As a maintainer, I want repeated processing of the same document to be idempotent, so that retries do not duplicate chunks or vectors.
5. As a maintainer, I want dependencies supplied at composition time, so that tests do not monkeypatch global factories.
6. As a retrieval user, I want the previously active version preserved when a new index attempt fails, so that working knowledge remains available.
7. As a developer, I want a typed outcome containing chunk count, warnings and pipeline identity, so that adapters do not infer state from logs.
8. As an operator, I want document-stage errors persisted before the task acknowledges completion, so that failures remain diagnosable.
9. As a security reviewer, I want processing logs to avoid document content, so that sensitive knowledge is not copied into operational logs.

## Implementation Decisions

- `DocumentIndexer.index()` is the only public processing use case and accepts a document identifier.
- Dependencies are constructed in the composition root and injected into the module: session factory, blob storage, parser, embedding provider and vector writer.
- The outcome distinguishes indexed-for-current-policy, OCR required, skipped/idempotent and permanently failed results; transient failures use typed retryable errors.
- The module is idempotent for document identity plus source hash plus pipeline revision.
- This spec preserves the current PDF parser, Chunk strategy, Fake Embedding and current activation behavior; publication is separated in Spec 04.
- Chunk/database/vector mutations are ordered so a failed new attempt never removes the last usable version.
- Processing phase, error code, warning list, attempt information and chunk count are persisted in structured fields available to status APIs.
- The Celery adapter returns the real outcome and retries only typed retryable errors.
- Reindexing reuses the same module behavior instead of maintaining a second indexing algorithm.

## Testing Decisions

- The sole primary testing Seam is `DocumentIndexer.index()`.
- Tests cover success, OCR required, invalid/encrypted PDF, retryable provider/storage/index failure, idempotent repeat, partial-write recovery and old-version preservation through that Seam.
- Deterministic database, storage, parser, embedding and vector adapters may be used, but assertions target the outcome and durable externally visible state.
- Celery retains only a thin adapter smoke test; worker return values must match the module outcome.
- Existing knowledge-processing and PDF pipeline tests provide prior art; direct tests of orchestration branches should be migrated to the module boundary.

## Out of Scope

- Review, publish, withdraw, rollback and deletion workflows.
- DOCX, Markdown, HTML and OCR implementation.
- Real model routing.
- OIDC/RBAC and user-facing Web status pages.
- Full distributed transaction guarantees across PostgreSQL, MinIO and Milvus.

## Further Notes

This spec creates the stable processing boundary required by governance and multi-format ingestion. It must not introduce a generic repository layer or expose provider-specific objects.
