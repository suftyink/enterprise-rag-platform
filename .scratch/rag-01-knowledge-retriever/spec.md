# Spec 01：收敛 KnowledgeRetriever 检索模块

Type: spec
Status: resolved
Blocked by: none
Primary test seam: `KnowledgeRetriever.retrieve()`

## Problem Statement

当前混合检索能力已经存在，但 FastAPI 路由直接了解数据库、向量索引、Embedding、Rerank、阈值和响应组装，离线评测又通过 HTTP 复制调用约定。检索实现缺少一个稳定、可复用的领域入口，后续接入真实模型、引用回答和评测调优时容易出现线上与离线行为漂移。

## Solution

建立单一的 `KnowledgeRetriever` 深模块，把查询归一化、候选召回、RRF、PostgreSQL 二次过滤、Rerank、阈值门禁和运行元数据隐藏在 `retrieve()` 后。现有搜索 API 只负责 HTTP 与领域对象映射，并保持对外响应兼容。

## User Stories

1. As an internal employee, I want retrieval to return only evidence I am allowed to use, so that another tenant's knowledge is never exposed.
2. As an internal employee, I want low-confidence searches to return an explicit insufficient-knowledge decision, so that uncertain evidence is not presented as reliable.
3. As an API consumer, I want the existing search response to remain compatible, so that current clients do not break during the refactor.
4. As an answer-module developer, I want one typed retrieval interface, so that grounded answering does not depend on HTTP or storage details.
5. As an evaluation developer, I want production and offline evaluation to call the same retrieval implementation, so that measured quality matches runtime behavior.
6. As an operator, I want each retrieval run to expose a trace identifier and pipeline/configuration identity, so that regressions can be reproduced.
7. As a maintainer, I want dependency failures distinguished from insufficient evidence, so that outages are not misreported as valid refusals.
8. As a maintainer, I want retrieval results to be stable for identical inputs and index versions, so that tests and evaluations are repeatable.
9. As a security reviewer, I want authorization and publication filtering enforced inside the module, so that callers cannot accidentally bypass it.

## Implementation Decisions

- The only public use-case interface is `KnowledgeRetriever.retrieve()`; it accepts a typed retrieval request and returns a typed retrieval outcome.
- The request contains query text, trusted actor context and evidence limit. HTTP-provided identity fields remain temporarily supported only by the adapter until the identity spec replaces them.
- The outcome contains the decision, ordered evidence and retrieval-run metadata. ORM models, Milvus hits and provider responses never cross the interface.
- Existing Dense + BM25 + RRF behavior, candidate limits, PostgreSQL second filtering, Fake Rerank and threshold semantics are preserved in this spec.
- Only active, processing-ready records remain searchable until publication state is introduced by Spec 04.
- Empty permitted candidates and candidates below the threshold produce `INSUFFICIENT_KNOWLEDGE`; infrastructure or provider failures produce explicit errors.
- Final ordering is deterministic, including ties.
- FastAPI maps the outcome to the current API schema and no longer assembles the retrieval pipeline itself.
- This spec does not change models, chunking, identity source, publication workflow or retrieval quality targets.

## Testing Decisions

- The sole primary testing Seam is `KnowledgeRetriever.retrieve()`.
- Behavioral tests cover tenant/global visibility, active/ready filtering, Dense/BM25 fusion, stable ranking, rerank threshold, insufficient knowledge and dependency errors through that Seam.
- Existing API tests remain as thin compatibility checks, not a second behavioral test suite.
- Storage, vector and model dependencies may use deterministic adapters, but tests assert only retrieval outcomes and observable dependency errors.
- Existing search API and retrieval tests provide prior art; tests that directly assert helper implementation details should not be added.

## Out of Scope

- Real Embedding, Rerank or LLM providers.
- OIDC authentication and role authorization.
- Publication review states.
- Multi-format parsing, answer generation, citations and Web UI.
- Retrieval parameter tuning or quality target claims.

## Further Notes

This is the first implementation spec because every later answer, evaluation and quality decision depends on a single production retrieval path. The refactor must preserve current behavior before quality changes begin.

## Answer

Implemented `KnowledgeRetriever.retrieve()` as the single typed retrieval use case. FastAPI now maps HTTP requests and outcomes without assembling retrieval internals; tenant/global and active/READY eligibility, dependency errors, query normalization, deterministic ordering, invalid Rerank response handling and reproducible configuration identity are enforced behind the Seam. Retriever-focused tests and API compatibility tests pass.
