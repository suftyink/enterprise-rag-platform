# Spec 06：接入真实检索模型并达到质量基线

Type: spec
Status: ready-for-agent
Blocked by: rag-02-retrieval-evaluation, rag-05-multiformat-ingestion
Primary test seam: `RetrievalEvaluator.run()`

## Problem Statement

当前 Embedding 和 Rerank 都是确定性的词项型 Fake，实现只能验证流程，不能证明真实语义召回质量；固定候选数、RRF 参数和 `0.10` 阈值也没有真实业务依据。规划还要求普通与敏感知识分别路由到合规云端或内网/本地模型。

## Solution

接入至少一套合规云端和一套内网/本地 Embedding、Rerank 运行路径，持久化模型与索引绑定，并使用 Spec 02 的同一评测 Seam 对模型、Chunk、候选数、融合参数和阈值做受控实验，只有达到质量与隔离门禁的配置才能成为试点 profile。

## User Stories

1. As an employee, I want semantically relevant evidence returned even when wording differs, so that search works beyond exact token overlap.
2. As a product specialist, I want exact SKU and abbreviation matching preserved, so that semantic retrieval does not weaken identifier searches.
3. As a security owner, I want restricted knowledge sent only to approved local/internal models, so that sensitive content never leaves its allowed boundary.
4. As an operator, I want each index tied to a specific embedding model and dimension, so that incompatible queries cannot corrupt retrieval.
5. As a retrieval engineer, I want configurations compared on the same versioned dataset, so that tuning decisions are evidence-based.
6. As a release owner, I want only profiles meeting Recall, MRR, refusal and leakage gates activated, so that regressions are blocked.
7. As a maintainer, I want provider errors normalized and time-bounded, so that failures are predictable across vendors.
8. As an operator, I want model identity, latency and token/request usage recorded without content leakage, so that runtime cost and reliability are visible.
9. As a maintainer, I want model upgrades to build a new index version before switching, so that rollback remains possible.
10. As a developer, I want Fake providers retained for deterministic tests, so that local development does not require production credentials.

## Implementation Decisions

- Production Embedding and Rerank adapters implement the existing narrow provider contracts; vendor response types do not enter domain modules.
- Model selection is based on capability, data classification and a versioned runtime profile, never an arbitrary provider name supplied by the request.
- `RESTRICTED` content and queries are fail-closed to approved internal/local adapters. Ordinary content may use an approved cloud profile.
- Index metadata records embedding deployment/model version, vector dimension, parser version, Chunk version and retrieval profile.
- Query embedding must match the active index binding; incompatible combinations fail explicitly.
- Model and parameter experiments are evaluated through `RetrievalEvaluator.run()` against the same approved dataset and index snapshot.
- Candidate count, RRF constant, rerank threshold and optional query normalization are configuration, but only a gate-passing profile may be activated.
- Parent-child Chunking or query expansion is added only if the category metrics demonstrate material benefit without violating latency/refusal/leakage gates.
- Model upgrades create a staging index, evaluate it, then switch atomically; previous passing bindings remain rollback targets.
- Chat generation routing is completed in Spec 07, reusing the same classification policy.

## Testing Decisions

- The sole primary testing Seam is `RetrievalEvaluator.run()`.
- All acceptance claims—including semantic recall, exact identifier behavior, restricted routing, configuration fingerprints, latency, refusal and leakage—are observed through versioned evaluation runs.
- Recording provider adapters may verify which deployment received classified requests, but they are assertions supporting the evaluator scenario, not a second behavioral suite.
- Live-provider smoke runs use cleared evaluation content and are separated from deterministic CI; secrets are never stored in fixtures or reports.
- A candidate profile passes only when the complete required metric set meets the configured thresholds.

## Out of Scope

- Selecting more than the minimum approved cloud and internal/local provider set.
- LLM answer generation, prompt design and citation validation.
- GraphRAG, ReAct, knowledge graphs and long-term memory.
- Automatic online experimentation on employee traffic.
- Relaxing data-classification policy when a model route is unavailable.

## Further Notes

This spec is where recall is actually improved. Tuning before the approved dataset and multi-format corpus exist is explicitly disallowed because it would optimize synthetic behavior.
